//! Pipeline state machine, structs, and merge-queue dispatch extracted from `app/mod.rs` (#745).
//!
//! Covers the pipeline-workflow state structs (ArmedAutoReview, PendingRework, etc.),
//! pipeline layout / badge helpers, and the full `impl CoordApp` pipeline-panel cluster:
//! stage helpers, lifecycle grouping, merge-queue panel, ranked plan, headless-merge
//! dispatch, pipeline sidebar rebuild, state-machine resolvers (stage_status_for, etc.),
//! artifact helpers, test-plan lifecycle, and remote-session polling.
//!
//! **Import pattern:** `use super::*` is intentional — these methods live on `CoordApp`
//! and need the full parent namespace. See `sessions.rs` for the full rationale.
#[allow(unused_imports)]
use super::*;

// ─── Data loading ─────────────────────────────────────────────────────────────

// ── #487: live tmux session discovery ────────────────────────────────────────

/// Leg 2 (#517): an interactive Work/Plan/Fix session the TUI launched this
/// run, armed to auto-advance to **Test** once the board shows it finished
/// (Test precedes Review — the smoke test runs before the PR/review).  The
/// struct keeps its `AutoReview` name for continuity, but the stage it now
/// offers is Test.
///
/// The trigger is strictly **board-driven**, never terminal-scraped (ToS
/// §3.7 / #437): the embedded shell does NOT exit when the interactive
/// `claude` session ends — it returns to the prompt and
/// `finalize_interactive_exit` records the work assignment as `done` + a
/// branch.  So we watch the board for a NEW done-with-branch work assignment
/// (one whose id was not already done at arm time) and then prompt.
pub(crate) struct ArmedAutoReview {
    /// Coordinator-local repo name (matches `Assignment.repo`).
    pub(crate) coord_repo: String,
    /// GitHub repo slug (the `pipeline_issues` key half), for row selection.
    pub(crate) repo_slug: String,
    /// GitHub issue number.
    pub(crate) issue_num: u64,
    /// Work assignment ids already `done` + branch when we armed.  The prompt
    /// fires only when a work aid appears that is NOT in this set — i.e. the
    /// freshly-launched interactive work, not a pre-existing completion.
    pub(crate) prior_done_ids: std::collections::HashSet<String>,
}

/// Leg 2 (#517): the issue whose smoke test just passed/skipped, awaiting the
/// operator's one-key confirm to launch the human-attended review (Test
/// precedes Review).  Raised by `detect_test_verdict`.
pub(crate) struct PendingAutoReview {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
}

/// Which interactive stage a [`PendingStageLaunch`] offer starts.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum StageLaunchKind {
    /// request-changes review whose findings are already in the DB → `--fix-of`.
    Fix,
    /// completed work/fix → `--smoke-of` interactive testing (Test precedes
    /// Review; on pass it then advances to review).
    Test,
}

/// A one-key offer to launch the next interactive stage for an issue.  Unlike
/// the #587 rework dialog this carries NO findings input — it's used only when
/// the next step needs no operator typing: a request-changes review whose
/// findings the agent already self-reported (kind=Fix, raised by
/// `detect_review_verdict`), or a freshly-completed work/fix advancing to the
/// smoke test (kind=Test, raised by `detect_completed_interactive_work`).
pub(crate) struct PendingStageLaunch {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    pub(crate) kind: StageLaunchKind,
}

/// #685: action performed when the test-mode choice is confirmed.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum TestModeChoiceAction {
    /// Dispatch a headless Work assignment (`start-skip-plan`) after setting
    /// the label.
    DispatchWork,
    /// Just update the label without dispatching (right-click "Set test mode").
    SetOnly,
}

/// #685: pending dialog asking the operator to choose the test-mode policy
/// before a headless Work session starts (or to flip it via right-click).
///
/// `[1] Pause for smoke test` (default) → `test-mode:smoke`.
/// `[2] Fully automated` → `test-mode:auto`.
///
/// On confirm the TUI queues `coord set-test-mode` then `coord assign`
/// (for DispatchWork) or only `coord set-test-mode` (for SetOnly).
#[derive(Clone)]
pub(crate) struct PendingTestModeChoice {
    pub(crate) coord_repo: String,
    pub(crate) issue_num: u64,
    /// What to do after the mode is chosen.
    pub(crate) action: TestModeChoiceAction,
    /// Currently set test-mode (from `all_labels`), used to pre-select the default.
    pub(crate) current_mode: Option<String>,
    /// `coord assign` machine name (used for DispatchWork).
    pub(crate) machine_name: Option<String>,
    /// `coord assign` model override (used for DispatchWork).
    pub(crate) model_override: Option<String>,
}

/// Leg 3 (#517): an interactive review the TUI launched this run, armed to
/// route on its verdict (board-driven, via `coord report-result`).  Fires once
/// when a NEW verdict appears (a review id not in `prior_verdicted_ids`):
/// request-changes → rework prompt; approve → smoke/merge notice.
pub(crate) struct ArmedVerdict {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    /// Review assignment ids that already carried a verdict when we armed —
    /// so only a freshly-reported verdict triggers routing.
    pub(crate) prior_verdicted_ids: std::collections::HashSet<String>,
}

/// Leg 3 (#517): a request-changes verdict awaiting the operator's one-key
/// confirm to launch the human-attended `--fix-of` session.  The exact review
/// id is re-resolved at confirm time (`selected_request_changes_review_aid`)
/// against the selected row, so only the issue identity is held here.
///
/// `findings` is populated as the operator types in the rework dialog (#587).
/// Written to the DB via `coord set-review-findings` on confirm, so the fix
/// worker's `_load_review_findings` DB cache hit gives it concrete feedback
/// instead of the "(No structured findings were captured)" fallback.
pub(crate) struct PendingRework {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    /// Reviewer findings typed by the operator.  Required before the fix
    /// can be dispatched — the rework dialog blocks confirm when empty.
    pub(crate) findings: String,
}

/// Leg 3c / A3 (#517, #581): an interactive testing session the TUI launched,
/// armed to route on its verdict (board-driven, via `coord test --passed|--fail`
/// recorded on the WORK row).  Fires once when the work row's `test_state`
/// changes to a terminal value: `failed` → fail→fix prompt; `passed`/`skipped`
/// → pass→merge prompt.
pub(crate) struct ArmedTestVerdict {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    /// The WORK assignment id under test — its `test_state` carries the verdict.
    pub(crate) work_aid: String,
    /// The work row's `test_state` when we armed, so only a NEW verdict fires.
    pub(crate) prior_test_state: Option<String>,
}

/// Leg 3c (#517, #581): a failed manual test awaiting the operator's one-key
/// confirm to launch the human-attended `--fix-of` fix on the existing branch.
/// `work_aid` is the failed WORK id (the backend's #581 test-fail fix front
/// door accepts it directly).
pub(crate) struct PendingTestFix {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
}

/// Leg 3c (#517, #306): a passed test awaiting the operator's one-key confirm
/// to launch the human-attended `--merge-of` merge agent (proactive rebase +
/// conflict resolution) on the approved branch.
pub(crate) struct PendingMerge {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
}

/// #2402: confirm-armed by the Pipeline row's "Re-verify (revalidate)"
/// context-menu action (offered only when the Merge stage is BLOCKED on a
/// `format::merge_revalidate_eligible` reason — #2397's surfaced
/// `checks_stale`/`smoke_required` case with the automatic-rerun budget
/// spent, #1769/#1851). Confirming runs `coord merge --revalidate --only
/// <assignment_id>` — a real local build+test (or CI re-run) on the daemon
/// host, so it gets the same brief confirm-before-spawn shape as
/// `pending_force_merge`, not a bare click-and-go.
#[derive(Clone)]
pub(crate) struct PendingMergeRevalidate {
    pub(crate) assignment_id: String,
    pub(crate) issue_num: u64,
}

/// #863: an in-flight headless preflight (`coord assign --interactive
/// --fix-of <aid> [--force] <machine> <repo> <issue> --dry-run`) checking
/// whether `pipeline.max_review_iterations` blocks a Fix dispatch, BEFORE the
/// human-attended terminal opens.  `_dispatch_fix_of`
/// (coord/commands/dispatch_workers.py) runs the same cap check and returns
/// cleanly on `--dry-run` without ever touching a TTY, so this preflight is a
/// safe, side-effect-free probe dispatched via `CommandRunner` — completely
/// separate from the real (embedded-PTY) Fix launch it gates.
///
/// Matched against the completed `CommandResult` by `work_aid` in
/// `run_periodic_work`.  On a clean exit the real launch proceeds (forced or
/// not, per `force`); on a cap refusal (`max_review_iterations` in stderr)
/// `pending_fix_force_confirm` is raised instead.
#[derive(Clone)]
pub(crate) struct PendingFixCapPreflight {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    pub(crate) machine: String,
    pub(crate) work_aid: String,
    /// Whether THIS preflight already carries `--force` (the operator's
    /// confirmed retry) — echoed back to the real launch on success so the
    /// embedded terminal's command line also gets `--force`.
    pub(crate) force: bool,
}

/// #863 review fix: the exact target a Fix launch resolves to when it's the
/// follow-through from a completed cap preflight, carried straight from the
/// [`PendingFixCapPreflight`] that just cleared — NOT re-derived from
/// whatever is currently selected in the UI.
///
/// Without this, an operator who changes the Pipeline selection (or clicks a
/// different issue's Fix action) while the preflight subprocess is still
/// running gets the follow-through launch silently misdirected to the
/// NEW selection instead of the issue that was actually preflighted — and on
/// the confirmed `--force` path, that could apply `--force` to a different
/// issue's fix-eligible work without ever showing that issue's own cap
/// confirm. Passed to `launch_interactive_session_on_machine_inner` as
/// `Some(..)` only on the follow-through call; every other caller passes
/// `None` and resolves from the live selection as before.
pub(crate) struct FixPreflightTarget {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    pub(crate) work_aid: String,
}

// ── #1084: oracle-loop pre-req pipeline (Gate A / Acceptance-Authoring) ──
//
// Both mock-author (Gate A, milestone-level) and test-author (per-issue JIT
// authoring) already ride the same generic Work→Test→Review→Merge gate
// machinery as a normal `type="work"` assignment — they just aren't named
// "work", so none of the generic stage-status helpers below (which key off
// `assignment_type == "work"` for the Test/Merge special cases) pick them
// up. `prereq_pipeline_status_from` and its two callers are a small
// parallel track that resolves the same 4-stage shape for these two
// assignment types, so the TUI can render "not-started / running /
// stuck-on-gate-X / done" for the pre-req pipeline instead of it staying
// invisible (docs/ORACLE_LOOP.md, claude-coordinator#947's
// 19-hour-invisible-stall friction log).

/// One stage's resolved status in a [`PrereqPipelineStatus`], plus the
/// timestamp its *current* status began — `Some` while Active (anchored to
/// `dispatched_at`) or while Pending directly behind a settled predecessor
/// (anchored to the predecessor's completion time), so the renderer can
/// show elapsed time for both "running" and "stuck waiting" without a
/// separate lookup. `None` for Done/Failed/Skipped stages, and for a
/// Pending stage whose predecessor hasn't settled either.
///
/// #1572: this `Pending`+`since` pairing is exactly what
/// `crate::app::prereq_stage_label` keys off to tell "never reached" apart
/// from "reached and stalled" — `since: None` renders as "not started" (no
/// clock), `since: Some(_)` as "pending" with a mounting "waiting Xm Ys" —
/// so a running clock always means the stage should be moving.
#[derive(Clone, Debug)]
pub(crate) struct PrereqStage {
    pub(crate) status: StageStatus,
    pub(crate) since: Option<f64>,
    /// #2397: the merge-queue's own computed block reason
    /// (`coord.merge_queue.plan()`'s per-entry `reason`, joined against the
    /// board's `merge_plan`) plus the human/in-flight/needs-a-person
    /// affordance (`format::fmt_merge_block_reason`) — set only on the Merge stage
    /// while it's `Pending` behind a settled Review, and only when a plan
    /// entry actually exists (a PR/branch is queued). `None` for every
    /// other stage, and for Merge before a plan entry exists — callers keep
    /// falling back to the bare "waiting Xm Ys" clock in that case.
    pub(crate) block_reason: Option<String>,
}

/// Resolved Author→Test→Review→Merge status for one oracle-loop pre-req
/// track. `author_id` is the short id of the resolved mock-author /
/// test-author assignment (the "which assignment" half of "running with
/// elapsed time + assignment id"); `None` when the track has never been
/// dispatched (every stage Pending, `since: None`).
#[derive(Clone)]
pub(crate) struct PrereqPipelineStatus {
    pub(crate) author_id: Option<String>,
    /// [Author, Test, Review, Merge], in that order.
    pub(crate) stages: [PrereqStage; 4],
}

/// #1199: collapse a 4-stage [`PrereqPipelineStatus`] (Author→Test→Review→
/// Merge) into the single overall status a milestone gate lane box shows —
/// used to render Gate A's box from the existing #1084 `gate_a_prereq_status`
/// machinery without recomputing anything. Mirrors
/// `prereq_pipeline_status_from`'s own settling order (each stage only
/// advances once its predecessor is Done, so Merge Done implies every prior
/// stage is Done too): Failed wins if any sub-stage failed, Done only when
/// every sub-stage is Done, Active if any sub-stage is running, else Pending
/// (covers "never dispatched", where every sub-stage is already Pending).
pub(crate) fn collapse_prereq_pipeline_status(status: &PrereqPipelineStatus) -> StageStatus {
    if status.stages.iter().any(|s| s.status == StageStatus::Failed) {
        StageStatus::Failed
    } else if status.stages.iter().all(|s| s.status == StageStatus::Done) {
        StageStatus::Done
    } else if status.stages.iter().any(|s| s.status == StageStatus::Active) {
        StageStatus::Active
    } else {
        StageStatus::Pending
    }
}

/// #863: the iteration cap was hit — awaiting the operator's one-key confirm
/// to re-dispatch the SAME Fix with `--force` (#862's override).  Raised by
/// the `PendingFixCapPreflight` completion handler; consumed by
/// `confirm_fix_force_past_cap`.
pub(crate) struct PendingFixForceConfirm {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    pub(crate) machine: String,
    pub(crate) work_aid: String,
    /// The configured cap (`pipeline.max_review_iterations`), parsed from the
    /// preflight's stderr, for the confirm prompt text.  `None` if the
    /// stderr format ever drifts — the prompt falls back to generic wording.
    pub(crate) max_iterations: Option<u32>,
}

/// #1197: one resolved child row ready to nest beneath its epic's Pipeline
/// row — issue number, title, and dispatch state (Done/InFlight/Ready,
/// reusing `milestone_dag::build_dag_nodes` rather than re-deriving it).
#[derive(Clone)]
pub(crate) struct EpicChildRow {
    pub(crate) number: u64,
    pub(crate) title: String,
    pub(crate) state: NodeState,
}

/// #1197: per-bucket epic-nesting projection, computed once per leaf-row
/// loop (`issue_idxs`/`mil_issue_idxs`) in `rebuild_pipeline_sidebar`.
///
/// `by_epic` maps an epic's `pipeline_issues` index to its resolved child
/// rows. `nested` is the flat set of every child issue keyed by
/// `(epic_repo, child_number)` — nested under some epic in THIS bucket —
/// callers skip those when emitting ordinary top-level rows, fixing the bug
/// #1197 describes: an epic and its children previously rendered as flat
/// siblings under the same milestone/repo bucket (both matched the same
/// milestone/lifecycle grouping). Keyed by repo (not a bare `u64`) for the
/// same reason `pipeline_globally_nested_children` is: GitHub issue numbers
/// are only unique within a repo, so a bare-number set risks suppressing an
/// unrelated same-numbered issue from a different repo in a multi-repo
/// bucket like Done (#1300).
#[derive(Default)]
pub(crate) struct EpicNesting {
    pub(crate) by_epic: std::collections::HashMap<usize, Vec<EpicChildRow>>,
    pub(crate) nested: std::collections::HashSet<(String, u64)>,
}

/// #1198: gold/amber marker colour for the epic-row badge — distinct from
/// every other colour already in play on a leaf row (repo-tag purple
/// `rgb(180, 140, 240)`, queue-depth amber `rgb(220, 160, 40)`, stage
/// badges, live-stream green) so an epic reads as visually different at a
/// glance rather than blending into one of those existing signals.
pub(crate) const EPIC_BADGE_COLOR: Color = Color::rgb(240, 200, 70);

/// #1198: build the marker span for an epic row, or `None` for a regular
/// issue. Spliced into a leaf row's `spans` right after the `#N` span and
/// before the title, at every Pipeline row-building site, so the epic
/// label — checked case-insensitively via `labels_carry_epic_label`,
/// unifying the split this issue calls out — is visible on the row itself,
/// independent of whether #1197's child-nesting happens to apply (a
/// childless epic still gets the badge).
///
/// Placement matters: `quadraui`'s TUI tree rasteriser clips `spans` at the
/// row's right edge and then draws any `TreeRow::badge` right-aligned OVER
/// whatever text landed there (`draw_tree` in `quadraui/src/tui/tree.rs`).
/// A marker appended at the *end* of `spans` (after the title, live-stream,
/// work-order indicators) would be clipped by a long title, or silently
/// overwritten by the row's own repo-tag/stage badge. Right after `#N` is
/// the one position guaranteed visible regardless of title length or badge
/// content — and it doesn't disturb `#N`'s own column, which several
/// existing TuiDriver tests key off of (chevron hit-testing via
/// `epic_pos.0 - 1.0`, indent comparisons against nested children).
pub(crate) fn epic_badge_span(issue: &PipelineIssue) -> Option<StyledSpan> {
    epic_badge_span_for_labels(&issue.all_labels)
}

/// #1270: label-driven core of `epic_badge_span`, factored out so the
/// Board panel's `IssueGroup` (which carries `labels: Vec<String>` copied
/// from `open_issues`, not a `PipelineIssue`) can render the identical
/// "◆EPIC" marker without depending on the Pipeline-only type.
pub(crate) fn epic_badge_span_for_labels(labels: &[String]) -> Option<StyledSpan> {
    if labels_carry_epic_label(labels) {
        Some(StyledSpan::with_fg("◆EPIC ", EPIC_BADGE_COLOR))
    } else {
        None
    }
}

/// #1500: GitHub label an operator sets (via the TUI's "Mark ready" /
/// "Unmark ready" Pipeline row actions, or `coord queue`/`coord unqueue`
/// directly) to stage a New issue into In-progress:`ready` with no
/// dispatch — the "next up" marker.  Deliberately a *new* label rather
/// than reusing `status:ready`: that label is already applied
/// automatically by `coord track` / the refinement finalize step for
/// *every* issue sent to the Pipeline, so it carries no "an operator
/// specifically staged this" signal.
pub(crate) const QUEUED_LABEL: &str = "status:queued";

/// Case-insensitive check for [`QUEUED_LABEL`] against an arbitrary label
/// list, mirroring `labels_carry_epic_label`'s convention.
pub(crate) fn labels_carry_queued_label(labels: &[String]) -> bool {
    labels.iter().any(|l| l.eq_ignore_ascii_case(QUEUED_LABEL))
}

/// #1174 (docs/ORACLE_LOOP.md, #1173): build the local launcher line for
/// the human-attended test-author session — `coord acceptance author
/// --interactive`'s Pipeline-row-menu counterpart to
/// `build_interactive_launch_cmd`'s Work/Review/Test/Merge lines (see
/// `sessions.rs`).
///
/// Unlike those, `coord acceptance author` takes `REPO TRACKING_ISSUE`
/// positionals plus `--issue <N>` (the JIT per-issue slice, #1171) rather
/// than `MACHINE REPO ISSUE` — there is no work_aid and, unlike the
/// generic flavours, no explicit machine argument here: the CLI
/// auto-picks a qualified machine, exactly like the headless
/// `dispatch_acceptance_author_for_selected_pipeline_row` path already
/// does (#1060). `for_path` threads `--for-path` through exactly like the
/// headless dispatch (#1151) — omitting it on a routed repo hits the
/// CLI's routing-gate rejection instead of a clean dispatch. As with
/// `build_interactive_launch_cmd`, the trailing `\r` auto-submits the
/// line so the launcher starts immediately, and `repo`/`for_path` are
/// shell-quoted via `shell_quote_arg` so values containing spaces or
/// shell metacharacters can't break the line.
pub(crate) fn build_acceptance_author_interactive_launch_cmd(
    config_path: Option<&str>,
    repo: &str,
    tracking_issue: u64,
    issue_num: u64,
    for_path: Option<&str>,
) -> String {
    // `--config` is registered on the leaf `acceptance author` command
    // (coord/commands/acceptance.py), not on the root `coord` click.group —
    // unlike `coord assign` in sessions.rs::build_interactive_launch_cmd,
    // where `--config` IS a root-group option. Placing it before the
    // `acceptance author` subcommand makes Click's root group reject it
    // with "No such option '--config'" before the subcommand ever sees it
    // (#1174 smoke-test failure). So `cfg` goes AFTER "acceptance author",
    // alongside the other options.
    let cfg = match config_path {
        Some(p) if !p.is_empty() => format!("--config {} ", shell_quote_arg(p)),
        _ => String::new(),
    };
    let for_path_flag = match for_path {
        Some(p) if !p.is_empty() => format!(" --for-path {}", shell_quote_arg(p)),
        _ => String::new(),
    };
    format!(
        "coord acceptance author {}{} {} --issue {}{} --interactive\r",
        cfg,
        shell_quote_arg(repo),
        tracking_issue,
        issue_num,
        for_path_flag,
    )
}

/// #1201: parse the glob(s) an issue body's own `## Files` section declares
/// as allowed, out of the convention (not a schema — see #1201's writeup)
/// most issues in this repo already follow:
/// ```text
/// ## Files
/// - **Allowed:** `tui/src/app/**` (render/dialogs as needed), `tests/**`.
/// - **Forbidden:** ...
/// ```
/// Scans every line containing the literal marker `**Allowed:**` and
/// extracts each backtick-quoted span on that line as a candidate glob —
/// tolerating comma-separated lists and trailing parenthetical notes
/// (which live outside the backticks and are simply not captured).
/// Non-path backtick spans that sneak in (e.g. `` `#[cfg(test)]` `` next to
/// a real glob) are harmless: `globs_overlap` just won't match them against
/// any real route. Returns an empty vec when the body has no `## Files`
/// section, or no `**Allowed:**` line — the caller's ambiguous-route
/// fallback (#1151) is unchanged for those issues.
pub(crate) fn parse_allowed_globs_from_issue_body(body: &str) -> Vec<String> {
    const MARKER: &str = "**Allowed:**";
    let mut globs = Vec::new();
    for line in body.lines() {
        let Some(after) = line.find(MARKER).map(|i| &line[i + MARKER.len()..]) else {
            continue;
        };
        let mut in_span = false;
        for span in after.split('`') {
            if in_span && !span.trim().is_empty() {
                globs.push(span.trim().to_string());
            }
            in_span = !in_span;
        }
    }
    globs
}

/// #1201: does an `## Files`-declared allowed glob (`allowed`) plausibly
/// belong to a routed acceptance driver's candidate subtree (`route`)?
///
/// Neither side is a concrete path — both are glob *patterns* (`allowed` is
/// e.g. `tui/src/app/**`, `route` is e.g. `tui/**`) — so this can't use
/// `fnmatch(path, pattern)` the way the CLI's own route resolution does
/// (`AcceptanceConfig.driver_for`). Instead it compares leading `/`-split
/// path segments pairwise, treating `*`/`**` on *either* side as a wildcard
/// that matches any segment, and considers the two glob patterns
/// "overlapping" (same subtree) if every segment pair up to the shorter
/// pattern's length matches. This correctly recognises `tui/src/app/**`
/// and `tui/**` as the same subtree (first segment `tui` == `tui`, second
/// pair `src`/`**` matches via wildcard, comparison stops at the shorter
/// pattern's 2 segments) while correctly rejecting `tui/src/app/**` against
/// `coord/**` (first segment `tui` != `coord`).
pub(crate) fn globs_overlap(allowed: &str, route: &str) -> bool {
    let a: Vec<&str> = allowed.split('/').filter(|s| !s.is_empty()).collect();
    let b: Vec<&str> = route.split('/').filter(|s| !s.is_empty()).collect();
    if a.is_empty() || b.is_empty() {
        return false;
    }
    let len = a.len().min(b.len());
    (0..len).all(|i| {
        let sa = a[i];
        let sb = b[i];
        sa == sb || sa == "*" || sa == "**" || sb == "*" || sb == "**"
    })
}

/// #1598: why [`CoordApp::pipeline_jump_target`] couldn't resolve a Board
/// issue to a real, currently-rendered Pipeline row.
///
/// Exists because the Board's "View in Pipeline" affordance used to gate
/// purely on `pipeline_issues` membership — broader than what the Pipeline
/// sidebar actually renders (#815 built a disabled state for the *untracked*
/// case, but a *tracked* issue can still render nowhere: dismissed, or —
/// the case that motivated this fix — classified "done" via a stale
/// `merge_queue` row (e.g. closed then reopened, no fresh assignment yet),
/// which #2405 removed from the sidebar altogether). The menu predicate and
/// the jump handler both call `pipeline_jump_target` so they can't drift on
/// the answer again.
///
/// #2449: a hit hidden only by the currently-typed Pipeline search filter is
/// deliberately NOT one of these reasons any more — "View in Pipeline" now
/// opens/activates a Pipeline document tab, which doesn't require the
/// issue's sidebar row to survive the active filter. The other three
/// reasons still describe a genuine absence of anything to jump to.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum PipelineNotVisible {
    /// Not a `pipeline_issues` member at all — no tracked (`coord`) label.
    Untracked,
    /// A member, but dismissed from the Pipeline sidebar this session.
    Dismissed,
    /// #2405: a member classified into the "done" bucket. The Pipeline
    /// sidebar no longer has a Done section at all — completed issues live
    /// in the `Completed` tab's grid — so there is no sidebar row to jump to.
    Completed,
}

impl PipelineNotVisible {
    /// Short, menu-column-width reason shown as the disabled "View in
    /// Pipeline" item's right-aligned hint.
    pub(crate) fn menu_hint(self) -> &'static str {
        match self {
            Self::Untracked => "no coord label",
            Self::Dismissed => "dismissed",
            Self::Completed => "see Completed tab",
        }
    }

    /// Full-sentence explanation for the `jump_board_to_pipeline` toast.
    pub(crate) fn toast_body(self, issue_number: u64) -> String {
        match self {
            Self::Untracked => format!(
                "#{issue_number} has no coord tracking label — add one to dispatch it."
            ),
            Self::Dismissed => {
                format!("#{issue_number} was dismissed from the Pipeline this session.")
            }
            Self::Completed => format!(
                "#{issue_number} is completed — open the Pipeline's Completed tab to find it."
            ),
        }
    }
}

/// Status badge text + colour for the Pipeline sidebar row.
///
/// Colour mapping (semantic → `quadraui::Theme` field, overridden per palette):
/// - `"work"`   → `theme.link_fg`              (blue: active work item)
/// - `"review"` → `theme.badge_warning` (amber: awaiting review)
/// - `"smoke"`  → `theme.diagnostic_hint`       (violet: smoke-test gate)
/// - `"merge"`  → `theme.accent_fg`             (blue: merge stage)
/// - `"done"`   → `theme.badge_passed`          (green: completed)
/// - other      → `theme.muted_fg`              (gray: unknown/other)
pub(crate) fn stage_badge(stage: &str, theme: &quadraui::Theme) -> (String, Color) {
    match stage {
        "work" => ("work".into(), theme.link_fg),
        "review" => ("review".into(), theme.badge_warning),
        "smoke" => ("smoke".into(), theme.diagnostic_hint),
        "merge" => ("merge".into(), theme.accent_fg),
        "done" => ("done".into(), theme.badge_passed),
        other => (other.to_string(), theme.muted_fg),
    }
}


/// #550: map a lowercase status string from the server-computed
/// `issue_stage_projection` (`coord.stage_projection`, see
/// `PENDING`/`ACTIVE`/`DONE`/`FAILED`/`STALE`/`SKIPPED`) onto
/// `quadraui::StageStatus`.  Kept as a plain string on the wire (rather than
/// deserializing directly into the enum) so this crate isn't coupled to
/// quadraui's serde representation. Unknown/missing values map to `Pending`
/// — the same "never ran yet" default the local computation uses.
pub(crate) fn parse_stage_status(s: &str) -> StageStatus {
    match s {
        "active" => StageStatus::Active,
        "done" => StageStatus::Done,
        "failed" => StageStatus::Failed,
        "stale" => StageStatus::Stale,
        "skipped" => StageStatus::Skipped,
        _ => StageStatus::Pending,
    }
}

/// #2500: pending "What needs to change?" text input, raised before
/// `coord gate-a --changes` fires for the selected epic row. Same
/// single-buffer shape as `PendingDriveQueueAfter` (Enter submits, Esc
/// cancels) — the whole payload is one free-text `--note`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PendingGateAChangesNote {
    /// Coord-local repo of the row the verdict is recorded against.
    pub(crate) repo_name: String,
    pub(crate) issue_number: u64,
    pub(crate) buf: String,
}

/// #2501: outcome of [`pull_default_branch_ff_only`] — gates "View Gate A
/// mock (local)". Every non-`Ok` variant means the working tree was left
/// completely untouched: never force, never touch a dirty tree, never
/// switch branches on the operator's behalf — the same posture this repo
/// already takes everywhere else around destructive/ambiguous git
/// operations. The caller turns each variant into a toast telling the
/// operator to sort out the checkout themselves.
enum LocalPullOutcome {
    /// Already up to date, or fast-forwarded cleanly onto the remote
    /// default branch.
    Ok,
    /// `git status --porcelain` reported uncommitted changes.
    Dirty,
    /// The checked-out branch isn't the remote's default branch. Carries
    /// the branch name actually checked out, for the toast.
    WrongBranch(String),
    /// The default branch has diverged from `origin/<default>` — a
    /// fast-forward merge isn't possible.
    Diverged,
    /// Some other git failure (no `origin` remote, detached HEAD, spawn
    /// failure, ...). Carries a short reason for the toast.
    Failed(String),
}

/// #2501 review: how long [`pull_default_branch_ff_only`]'s `git fetch`
/// step is allowed to run before it's killed and reported as a `Failed`
/// outcome. A local fetch against these repos is sub-second, so this is
/// generous slack for a slow-but-honest remote, not a target latency — it
/// exists purely to cap the worst case (a remote peer that accepts the TCP
/// connection and then silently drops packets) well short of the
/// OS-level connect/read timeouts, which run into minutes.
const FETCH_TIMEOUT: Duration = Duration::from_secs(20);

/// Run `program <args>` in `current_dir`, killing it and returning an error
/// if it hasn't exited within `timeout`. Generic over `program` (rather than
/// hardcoding `git`) purely so the timeout/kill machinery itself is
/// unit-testable against a synthetic long-running command without needing a
/// real git remote that can be made to hang on demand.
///
/// Polls [`std::process::Child::try_wait`] rather than blocking on `.wait()`
/// or `.output()` — those have no built-in deadline, which is the whole
/// problem this exists to solve. stdin is nulled (matching every other
/// subprocess call in this module) so nothing here can block on a prompt.
fn run_command_with_timeout(
    program: &str,
    current_dir: &std::path::Path,
    args: &[&str],
    timeout: Duration,
) -> Result<std::process::Output, String> {
    use std::process::{Command, Stdio};

    let mut child = Command::new(program)
        .current_dir(current_dir)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("could not launch {program}: {e}"))?;

    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => break,
            Ok(None) => {
                if Instant::now() >= deadline {
                    // Best-effort: reap after kill so we don't leak a zombie,
                    // but don't let a stuck kill/wait itself hang this call —
                    // both are already non-blocking-ish (kill just signals;
                    // wait on an already-killed child returns promptly).
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!("timed out after {timeout:?}"));
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(e) => return Err(format!("wait failed: {e}")),
        }
    }
    child
        .wait_with_output()
        .map_err(|e| format!("could not read output: {e}"))
}

/// #2501: fetch `origin` and fast-forward the local checkout at `repo_dir`
/// onto the remote's default branch.
///
/// Synchronous/blocking by design: an async job queue (mirroring
/// `spawn_test_build`/`test_build_jobs`) would need a new field on `App`
/// itself (`app/mod.rs`), out of scope for this fix. This is a rare,
/// explicit user click — not a per-tick operation — and a local `git fetch`
/// against these repos is sub-second, so a brief block is an acceptable
/// trade against the scope this change stays within. The one step that
/// actually talks to the network (`fetch`) is bounded by
/// [`FETCH_TIMEOUT`] regardless: `stdin(Stdio::null())` already makes an
/// *auth* failure fail fast, but a remote that accepts the TCP connection
/// and then silently drops packets is not an auth failure — that class of
/// hang can otherwise block the whole TUI for OS-timeout-scale minutes, not
/// "briefly".
///
/// Sequence, each step aborting before the next on failure:
/// 1. `git status --porcelain` — any output at all means a dirty working
///    tree, and we never pull over one (`Dirty`).
/// 2. Resolve the currently checked-out branch (`Failed` on a detached HEAD
///    or any other resolution failure).
/// 3. `git fetch origin <branch>` — safe regardless of working-tree state;
///    updates `refs/remotes/origin/<branch>` via the default clone refspec.
///    Killed and reported as `Failed` if it runs past [`FETCH_TIMEOUT`].
/// 4. Resolve the remote's default branch via
///    `refs/remotes/origin/HEAD` (running `git remote set-head origin
///    --auto` once, if that ref isn't set yet, to populate it from what was
///    just fetched — a local bookkeeping ref, not a working-tree mutation).
/// 5. If the checked-out branch isn't that default branch, abort
///    (`WrongBranch`) rather than merging into the wrong branch or
///    switching branches ourselves.
/// 6. `git merge --ff-only origin/<default>` — `Diverged` when this isn't a
///    fast-forward, `Ok` otherwise (including "already up to date").
fn pull_default_branch_ff_only(repo_dir: &std::path::Path) -> LocalPullOutcome {
    use std::process::{Command, Stdio};

    fn run(repo_dir: &std::path::Path, args: &[&str]) -> Result<std::process::Output, String> {
        Command::new("git")
            .current_dir(repo_dir)
            .args(args)
            .stdin(Stdio::null())
            .output()
            .map_err(|e| format!("git {}: {}", args.join(" "), e))
    }

    fn first_reason(out: &std::process::Output, fallback: &str) -> String {
        String::from_utf8_lossy(&out.stderr)
            .lines()
            .map(|l| l.trim())
            .find(|l| !l.is_empty())
            .map(|l| l.to_string())
            .unwrap_or_else(|| fallback.to_string())
    }

    // 1. Dirty check first — before anything else touches this repo at all.
    let status = match run(repo_dir, &["status", "--porcelain"]) {
        Ok(out) => out,
        Err(e) => return LocalPullOutcome::Failed(e),
    };
    if !status.status.success() {
        return LocalPullOutcome::Failed(first_reason(&status, "git status failed"));
    }
    if !status.stdout.is_empty() {
        return LocalPullOutcome::Dirty;
    }

    // 2. Current branch.
    let branch_out = match run(repo_dir, &["rev-parse", "--abbrev-ref", "HEAD"]) {
        Ok(out) => out,
        Err(e) => return LocalPullOutcome::Failed(e),
    };
    if !branch_out.status.success() {
        return LocalPullOutcome::Failed(first_reason(&branch_out, "git rev-parse HEAD failed"));
    }
    let branch = String::from_utf8_lossy(&branch_out.stdout).trim().to_string();
    if branch.is_empty() || branch == "HEAD" {
        return LocalPullOutcome::Failed("checkout has no branch checked out (detached HEAD)".to_string());
    }

    // 3. Fetch — safe regardless of working-tree state. Bounded, unlike the
    //    other steps here: this is the one that talks to the network.
    let fetch = match run_command_with_timeout(
        "git",
        repo_dir,
        &["fetch", "origin", &branch],
        FETCH_TIMEOUT,
    ) {
        Ok(out) => out,
        Err(e) => return LocalPullOutcome::Failed(format!("git fetch origin {branch}: {e}")),
    };
    if !fetch.status.success() {
        return LocalPullOutcome::Failed(first_reason(&fetch, "git fetch origin failed"));
    }

    // 4. Resolve the remote's default branch, populating the ref if a
    //    non-`clone` checkout never set it.
    let default_branch = resolve_remote_default_branch(repo_dir).or_else(|| {
        let _ = run(repo_dir, &["remote", "set-head", "origin", "--auto"]);
        resolve_remote_default_branch(repo_dir)
    });
    let Some(default_branch) = default_branch else {
        return LocalPullOutcome::Failed(
            "cannot determine the remote's default branch (refs/remotes/origin/HEAD unset)"
                .to_string(),
        );
    };

    // 5. Refuse to merge into / switch off of the wrong branch.
    if branch != default_branch {
        return LocalPullOutcome::WrongBranch(branch);
    }

    // 6. Fast-forward only.
    let merge = match run(
        repo_dir,
        &["merge", "--ff-only", &format!("origin/{default_branch}")],
    ) {
        Ok(out) => out,
        Err(e) => return LocalPullOutcome::Failed(e),
    };
    if !merge.status.success() {
        return LocalPullOutcome::Diverged;
    }
    LocalPullOutcome::Ok
}

/// #2501: resolve the remote's default branch name (e.g. `"main"`) via
/// `refs/remotes/origin/HEAD`, stripping the `origin/` prefix.  `None` when
/// that ref isn't set (a checkout that was never `git clone`d, or had it
/// pruned) — the caller tries `git remote set-head origin --auto` once and
/// retries before giving up.
fn resolve_remote_default_branch(repo_dir: &std::path::Path) -> Option<String> {
    let out = std::process::Command::new("git")
        .current_dir(repo_dir)
        .args(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        .stdin(std::process::Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let short = String::from_utf8_lossy(&out.stdout).trim().to_string();
    short.strip_prefix("origin/").map(|s| s.to_string())
}

/// #2501: fire-and-forget open of a local filesystem path via a `file://`
/// URL handed to the OS's default opener for that URL — the `file://`
/// counterpart to `dispatch_open_pr_for_selected_pipeline_row`'s `gh pr view
/// --web`. `gh` handles cross-platform opening for the PR case; there's no
/// `gh` equivalent for a bare local path, so this shells out to the OS
/// opener directly. For an `.html` file that opener is the default browser
/// (the MIME association); for a bare directory (the pre-index.html
/// fallback — see the call site's comment) it's typically the file manager
/// instead, since directories aren't browser-associated. Same "don't care
/// about exit code" posture either way — the user sees the OS opener react,
/// not a coord-tui status.
fn open_local_path_in_browser(path: &std::path::Path) {
    let url = format!("file://{}", percent_encode_file_url_path(path));
    #[cfg(target_os = "macos")]
    let mut cmd = std::process::Command::new("open");
    #[cfg(target_os = "windows")]
    let mut cmd = {
        let mut c = std::process::Command::new("cmd");
        c.args(["/C", "start", ""]);
        c
    };
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let mut cmd = std::process::Command::new("xdg-open");
    let _ = cmd
        .arg(&url)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
}

/// #2501: minimal percent-encoding for a filesystem path used in a
/// `file://` URL — escapes everything outside the URL-safe unreserved set
/// (preserving `/` as the path separator and `:` for a Windows drive
/// letter) so a mocks directory containing spaces or other punctuation
/// still opens correctly.
fn percent_encode_file_url_path(path: &std::path::Path) -> String {
    percent_encode_file_url_path_str(&path.to_string_lossy())
}

/// Pure string transform backing [`percent_encode_file_url_path`]. Split out
/// so the Windows drive-letter handling below is testable with literal
/// `C:\...`-style strings regardless of which OS runs the test suite — a
/// `PathBuf` built from such a string on Linux keeps the backslashes as
/// ordinary (valid, if unusual) filename characters rather than parsing them
/// as separators the way `std::path` would on an actual Windows host, so
/// operating on the string form directly is what makes this reachable at
/// all outside CI's one Windows leg.
///
/// #2501 review: `open_local_path_in_browser` has a
/// `#[cfg(target_os = "windows")]` branch, so Windows support was clearly
/// intended, but without this a path like `C:\Users\john\mocks` produced
/// `file://C%3A%5CUsers%5Cjohn%5Cmocks` — the drive-letter colon and every
/// backslash percent-encoded rather than converted, which is not a file URL
/// any opener recognises. `file:///C:/Users/john/mocks` (forward slashes,
/// unescaped drive-letter colon, and a leading `/` before the drive letter —
/// the third slash after `file://`) is the form RFC 8089 / real browsers
/// expect.
fn percent_encode_file_url_path_str(s: &str) -> String {
    // A Windows absolute path always starts `<letter>:` followed by `\`,
    // `/`, or end-of-string (bare drive root, e.g. `C:`). Detected on the
    // string alone — not `Path::is_absolute()`, which is itself
    // Windows-vs-Unix ambiguous for exactly this string on the "wrong" host.
    let is_windows_drive_path = s.len() >= 2
        && s.as_bytes()[0].is_ascii_alphabetic()
        && s.as_bytes()[1] == b':'
        && matches!(s.as_bytes().get(2), None | Some(b'\\') | Some(b'/'));
    let normalized = if is_windows_drive_path {
        // Backslash -> forward slash ONLY on this identified-Windows path;
        // a bare Unix path keeps literal backslashes untouched (they're a
        // valid, if rare, filename character there and must not be
        // rewritten). Leading `/` supplies the third slash of
        // `file:///C:/...` once the caller prepends the `file://` scheme.
        format!("/{}", s.replace('\\', "/"))
    } else {
        s.to_string()
    };
    let mut out = String::with_capacity(normalized.len());
    for b in normalized.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'/' | b':' => {
                out.push(b as char);
            }
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

// ─── Pipeline impl CoordApp ─────────────────────────────────────────────────

impl CoordApp {

    // ── Pipeline panel ────────────────────────────────────────────────────

    /// Effective list of stages: a Plan stage (when `pipeline_require_plan`
    /// is set), then "work", then the configured `pipeline.default_gates`
    /// (deduplicated to handle accidental "work" / "plan" entries in the
    /// gate list).
    pub(crate) fn pipeline_stage_names(&self) -> Vec<String> {
        let mut stages: Vec<String> = Vec::with_capacity(4);
        if self.data.pipeline_require_plan {
            stages.push("plan".to_string());
        }
        stages.push("work".to_string());
        for g in &self.data.pipeline_default_gates {
            // #1429: "merge" is restored to the per-issue stage-name ordering
            // as an observation badge (#738 retired the per-issue *box*'s
            // Go/dispatch affordance — that reasoning was about the
            // affordance, not observation; `stage_status_for`/
            // `merge_stage_status_for` already compute its real
            // queue/CI/conflict-fix state). #2402 partially reverses the
            // affordance half too: `is_dispatchable_stage` now includes
            // "merge" so a BLOCKED (or not-yet-started) Merge box gets a
            // [Go] again — #738's "merge is initiated solely from the Merge
            // Queue panel" left a stuck entry with no per-issue recovery
            // path at all (#2397's checks_stale/smoke_required case).
            // "work"/"plan" stay excluded — they're prepended separately.
            if g != "work" && g != "plan" {
                stages.push(g.clone());
            }
        }
        stages
    }

    /// Per-issue stage list — prepends a "plan" stage when the issue has
    /// at least one `type="plan"` assignment, even if `pipeline_require_plan`
    /// is false globally.
    ///
    /// Motivation: the user can right-click → Start with Plan on a
    /// per-issue basis (#262).  Without this override, the plan-typed
    /// assignment would get folded into the Work stage (turning it
    /// green) and the user would see no evidence Plan ever ran.
    pub(crate) fn pipeline_stage_names_for_issue(&self, issue: &PipelineIssue) -> Vec<String> {
        let mut stages = self.pipeline_stage_names();
        let already_has_plan = stages.first().map(|s| s == "plan").unwrap_or(false);
        if !already_has_plan && self.issue_has_plan_assignment(issue) {
            stages.insert(0, "plan".to_string());
        }
        stages
    }

    /// True iff at least one assignment with `type="plan"` exists for
    /// *issue* (matching by issue number and, when set, coord_repo).
    pub(crate) fn issue_has_plan_assignment(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.assignment_type.as_deref() == Some("plan")
                && a.issue_number == issue.number
                && issue
                    .coord_repo
                    .as_deref()
                    .map(|r| r == a.repo)
                    .unwrap_or(true)
        })
    }

    /// Compute the lifecycle section key for a pipeline issue.
    ///
    /// Priority (highest wins):
    /// 1. `is_closed`            → "done"
    /// 2. Epic tracking issue (`epic_children_for` is `Some`) → aggregate
    ///    child state via `epic_lifecycle_section` (#1253)
    /// 3. Direct merge_queue "merged"                → "done"
    /// 4. Has any assignment     → "in-progress"
    /// 5. Has label `status:ready` (no assignments) → "pending"
    /// 6. Has label `status:refining`               → "refining"
    /// 7. Otherwise                                 → "new"
    pub(crate) fn pipeline_lifecycle_section(&self, issue: &PipelineIssue) -> &'static str {
        if issue.is_closed {
            return "done";
        }
        // #1253: an epic's actual work happens on its *children*, not on the
        // epic issue itself, so it almost never carries a direct assignment
        // — the `has_work_assignment` check below is essentially always
        // false for one, and it would sit in "New" for its entire life
        // (observed: #1200, all 7 children closed, stuck in New until
        // manually closed). Classify by aggregate child state instead.
        //
        // #1314: this MUST run before the direct `merge_stage_status_for`
        // check below. A `type="work"` dispatch landed (incorrectly, but
        // possible in practice — see #1314) directly against a tracking
        // issue's own number reads as a merged/"done" direct assignment on
        // that issue, which would otherwise short-circuit an epic straight
        // to "done" while its real children are still open/unstarted
        // (observed: epic #1120, 3 of 4 children untouched, sorted into
        // Done and effectively unfindable). An epic must always defer to
        // the aggregate child-state classifier, regardless of what its own
        // direct merge stage shows.
        if let Some(children) = self.epic_children_for(issue) {
            return self.epic_lifecycle_section(issue, children);
        }
        // An open issue whose merge_queue entry is "merged" is logically
        // completed — the PR closed the issue via `fixes #N` even before the
        // brain has synced the GitHub close.  Mirror the Board classifier
        // (IssueGroup::lifecycle_section) which already treats merged as done.
        if self.merge_stage_status_for(issue) == StageStatus::Done {
            return "done";
        }
        // Only *workable* assignments make an issue in-progress.  Scoping
        // conversations — `refinement`, `new-issue-chat`, `test-chat`, and the
        // #628 "Chat about issue" (`chat`) — are NOT pipeline execution, so a
        // chat-/refinement-only issue belongs in New, not Active.  Use the same
        // `is_workable_type` predicate as the Board classifier; the old inline
        // list omitted `chat`, which pinned chat-only issues (e.g. #258) to
        // In-progress and made "Drop to backlog" appear to do nothing.
        //
        // #1553: matched on `effective_issue_number`, not the raw column. An
        // oracle-loop child's acceptance slice (and its review/fix/smoke
        // chain) is booked with the milestone's TRACKING issue in
        // `issue_number`, so the raw match left a child with six live
        // sessions classified as Pending — the operator's literal report was
        // "I don't think #1124 is running, the TUI shows no activity". The
        // epic itself is unaffected: it returned above via
        // `epic_lifecycle_section`, which aggregates child state rather than
        // reading its own assignments.
        let has_work_assignment = self.data.assignments.iter().any(|a| {
            a.effective_issue_number() == issue.number
                && issue
                    .coord_repo
                    .as_deref()
                    .map(|r| r == a.repo)
                    .unwrap_or(true)
                && a.assignment_type
                    .as_deref()
                    .map(is_workable_type)
                    .unwrap_or(true)
        });
        if has_work_assignment {
            return "in-progress";
        }
        // #1500: an operator-staged issue (`status:queued`, set via the
        // "Mark ready" row action / `coord queue`) with no work dispatched
        // yet is the "next up" intent marker — render it under In-progress
        // with the `ready` badge (never `in-flight`: `active_issue_in_flight`
        // is false with no live session). Checked AFTER the real-work branch
        // above so actual dispatch always outranks the marker — once a
        // worker lands, the row flips to `in-flight` on its own with no
        // extra bookkeeping needed here.
        if labels_carry_queued_label(&issue.all_labels) {
            return "in-progress";
        }
        // #628: a coord-tracked, not-yet-started issue is just "new". Neither
        // `status:ready` (Pending) nor `status:refining` (Refining) splits a
        // separate bucket anymore — they gate no dispatch, so the New/Pending and
        // Refining distinctions were display-only. The empty pending/refining
        // sections auto-hide; the Pipeline is New → In-progress → Done.
        "new"
    }

    /// #1253: classify an epic tracking issue's lifecycle section by
    /// aggregate *child* state rather than the epic's own (near-always-
    /// absent) direct assignment. Reuses `build_dag_nodes` — the same
    /// Done/InFlight/Ready computation `compute_epic_nesting` uses for the
    /// nested child rows — so the section bucket and the nested badges never
    /// disagree.
    ///
    /// - Any child `Done` or `InFlight` → "in-progress". This deliberately
    ///   covers *both* "some children still running" *and* "all children
    ///   done, epic still open" — the epic is just waiting on a manual close
    ///   at that point, which is not "new" (the #1253 fix-direction note
    ///   flagged the latter as arguably still in-progress rather than a new
    ///   bucket, to avoid growing the lifecycle-section enum for one case).
    /// - No child started yet (all `Ready`/`Blocked`, or the child list
    ///   resolved to zero DAG nodes) → "new" stands, UNLESS #1500's
    ///   `status:queued` staging marker is present on the epic itself or
    ///   any child (checked below) — an epic almost never carries a direct
    ///   assignment of its own, so its "Mark ready" action stages the
    ///   marker somewhere in the epic+children set rather than relying on
    ///   the plain-issue branch in [`Self::pipeline_lifecycle_section`].
    fn epic_lifecycle_section(&self, issue: &PipelineIssue, children: &[EpicChild]) -> &'static str {
        let repo_name = issue.coord_repo.clone().unwrap_or_default();
        let work_order: Vec<WorkOrderNode> = children
            .iter()
            .map(|c| WorkOrderNode {
                issue_number: c.number,
                group: None,
                after: Vec::new(),
            })
            .collect();
        let nodes = build_dag_nodes(&work_order, &repo_name, &self.data.open_issues, &self.data.assignments);
        let any_progress = nodes
            .iter()
            .any(|n| matches!(n.state, NodeState::Done | NodeState::InFlight));
        if any_progress {
            return "in-progress";
        }
        // #1500: staged-but-not-started — the epic itself carries
        // `status:queued`, or a child does (e.g. only one child was staged
        // individually before the epic itself was marked ready).
        if labels_carry_queued_label(&issue.all_labels)
            || self.children_carry_queued_label(&repo_name, children)
        {
            return "in-progress";
        }
        "new"
    }

    /// #1598: resolve a Board issue's real, currently-rendered Pipeline
    /// row, if it has one.
    ///
    /// Single source of truth for "is this issue visible in the Pipeline
    /// right now" — called by BOTH the Board right-click menu's "View in
    /// Pipeline" enablement check (`context_menu_items_for_board_row`) and
    /// `jump_board_to_pipeline`, so they can't independently drift on the
    /// answer again. Delegates the actual gating to the exact functions the
    /// sidebar rebuild uses (`pipeline_active_issues`,
    /// `pipeline_repos_for_state`) rather than re-deriving their exclusion
    /// rules, so a future change to either stays in sync here for free.
    /// See [`PipelineNotVisible`] for what "not visible" can mean.
    pub(crate) fn pipeline_jump_target(
        &self,
        repo: &str,
        number: u64,
    ) -> Result<usize, PipelineNotVisible> {
        let idx = self
            .pipeline_issues
            .iter()
            .position(|pi| {
                pi.number == number && pi.coord_repo.as_deref().unwrap_or(&pi.repo_slug) == repo
            })
            .ok_or(PipelineNotVisible::Untracked)?;
        let issue = &self.pipeline_issues[idx];
        // Attribute *why* below before checking `visible` — dismissal is
        // also applied inside the per-bucket functions themselves, so
        // checking it here doesn't change the gating decision; it only lets
        // us report the specific cause instead of a generic "not visible".
        if self
            .pipeline_dismissed
            .contains(&(issue.repo_slug.clone(), issue.number))
        {
            return Err(PipelineNotVisible::Dismissed);
        }
        // #2405: "done" has no sidebar section any more — completed issues
        // are reachable only through the Completed tab's grid — so it can
        // never resolve to a sidebar row, whatever its timestamps say.
        let lc = self.pipeline_lifecycle_section(issue);
        if lc == "done" {
            return Err(PipelineNotVisible::Completed);
        }
        // #2449: visibility is checked with the Pipeline search filter
        // IGNORED (`apply_search = false`) — "View in Pipeline" opens or
        // activates a document tab, which doesn't need the issue's row to
        // survive the currently-typed filter the way a sidebar selection
        // does. The tree itself is unaffected; only this jump/enablement
        // check is filter-blind.
        let visible = match lc {
            "in-progress" => self.pipeline_active_issues_impl(false).contains(&idx),
            lc => self
                .pipeline_repos_for_state_impl(lc, false)
                .iter()
                .any(|(_, idxs)| idxs.contains(&idx)),
        };
        if visible {
            Ok(idx)
        } else {
            Err(PipelineNotVisible::Completed)
        }
    }

    /// #1500: numbers of `children` (an epic's `## Sub-issues` list) that
    /// currently carry [`QUEUED_LABEL`], resolved against the
    /// `open_issues` label cache (children aren't necessarily
    /// `coord`-labelled themselves, so they may not appear in
    /// `self.pipeline_issues`). Shared by [`Self::epic_lifecycle_section`]
    /// (membership test) and `unmark_selected_ready`'s epic fan-out (which
    /// child numbers to strip the label from).
    pub(crate) fn queued_child_numbers(&self, repo_name: &str, children: &[EpicChild]) -> Vec<u64> {
        children
            .iter()
            .filter(|c| {
                self.data
                    .open_issues
                    .iter()
                    .find(|oi| oi.repo_name == repo_name && oi.number == c.number)
                    .map(|oi| labels_carry_queued_label(&oi.labels))
                    .unwrap_or(false)
            })
            .map(|c| c.number)
            .collect()
    }

    /// #1500: true when any of `children` currently carries [`QUEUED_LABEL`].
    fn children_carry_queued_label(&self, repo_name: &str, children: &[EpicChild]) -> bool {
        !self.queued_child_numbers(repo_name, children).is_empty()
    }

    /// #1500: does `issue` carry the `status:queued` staging marker —
    /// directly, or (for an epic tracking issue) via the epic itself or any
    /// child? This is the same test [`Self::pipeline_lifecycle_section`] /
    /// [`Self::epic_lifecycle_section`] use to promote a not-yet-dispatched
    /// row to In-progress, exposed here so the context menu can gate
    /// "Unmark ready" to rows an actual `status:queued` put there (rather
    /// than a row that reached In-progress via real work, which "Unmark
    /// ready" would be a confusing no-op for).
    pub(crate) fn pipeline_issue_is_queued(&self, issue: &PipelineIssue) -> bool {
        if labels_carry_queued_label(&issue.all_labels) {
            return true;
        }
        match (issue.coord_repo.as_deref(), self.epic_children_for(issue)) {
            (Some(repo), Some(children)) => self.children_carry_queued_label(repo, children),
            _ => false,
        }
    }

    /// Return true when any assignment in the DB touches this issue.
    ///
    /// Used to distinguish "closed via coord pipeline" (has assignment rows) from
    /// "closed externally / implemented in-session" (zero assignment rows ever
    /// created).  Matches by issue number and coord repo (or repo_slug when no
    /// local mapping exists).
    pub(crate) fn issue_has_any_assignment(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && if let Some(local) = &issue.coord_repo {
                    a.repo == *local
                } else {
                    true
                }
        })
    }

    /// #546: Sum ``cost_usd`` across ALL assignments for an issue (rollup).
    ///
    /// Returns ``None`` when no assignment has a captured cost yet (e.g. all
    /// workers are still running or all rows predate #208).  Interactive / Max
    /// subscription sessions have ``cost_usd = NULL`` and are excluded from
    /// the sum (they are shown as "Max" in the per-assignment rows instead).
    ///
    /// The rollup shows the coordinator the TOTAL spend for an issue across all
    /// stage iterations: plan + work + fix-1 + fix-2 + review + smoke.
    ///
    /// #1553: keyed on [`Assignment::effective_issue_number`], so an
    /// oracle-loop acceptance slice's spend lands on the CHILD issue it was
    /// authored for rather than on the milestone's tracking issue (which
    /// otherwise absorbed every child's authoring cost — $7.90 of #1124's
    /// work booked to epic #1120).
    pub(crate) fn issue_total_cost(&self, issue: &PipelineIssue) -> Option<f64> {
        let local_repo = issue.coord_repo.as_deref();
        let costs: Vec<f64> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.effective_issue_number() == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == *r,
                None => true,
            })
            .filter_map(|a| a.cost_usd)
            .filter(|&c| c > 0.0)
            .collect();
        if costs.is_empty() {
            None
        } else {
            Some(costs.iter().sum())
        }
    }

    /// #546: Total token count (input + output) for an issue across all
    /// assignments.  Returns 0 when no tokens have been persisted yet.
    ///
    /// #1553: same effective-issue attribution as [`Self::issue_total_cost`].
    pub(crate) fn issue_total_tokens(&self, issue: &PipelineIssue) -> i64 {
        let local_repo = issue.coord_repo.as_deref();
        self.data
            .assignments
            .iter()
            .filter(|a| a.effective_issue_number() == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == *r,
                None => true,
            })
            .map(|a| a.input_tokens + a.output_tokens)
            .sum()
    }

    /// Return the coord_repo name for an issue, falling back to the repo_slug.
    pub(crate) fn pipeline_repo_key(issue: &PipelineIssue) -> &str {
        issue.coord_repo.as_deref().unwrap_or(&issue.repo_slug)
    }

    /// Group pipeline issues under a repo into non-empty lifecycle sections.
    ///
    /// #194: the Pipeline panel displays all **five lifecycle sections** —
    /// **New** (no `status:*` label), **Refining** (`status:refining`),
    /// **Pending** (`status:ready`, no work assignments), **In-progress**
    /// (active work assignment), and **Done** (closed / merged).  The
    /// classifier keys returned by `pipeline_lifecycle_section` map 1:1
    /// to the display labels — the Pipeline sidebar surfaces the full
    /// lifecycle so the TUI can drive every stage.
    ///
    /// Returns `(lifecycle_key, Vec<pipeline_issues index>)` in display
    /// order (New → Refining → Pending → In-progress → Done), skipping
    /// empty sections.  Issues that the user has dismissed via 'D' are
    /// excluded.
    ///
    /// Note: used in tests to verify per-repo lifecycle grouping; the
    /// production sidebar uses `pipeline_active_issues` and
    /// `pipeline_repos_for_state` directly.
    #[cfg(test)]
    pub(crate) fn pipeline_groups_for_repo(&self, repo_key: &str) -> Vec<(&'static str, Vec<usize>)> {
        // #194: full five-section lifecycle — New / Refining / Pending /
        // In-progress / Done — surfaced in display order.
        const VISIBLE_LIFECYCLE: [&str; 5] =
            ["new", "refining", "pending", "in-progress", "done"];

        // #290: deduplicate by (repo_slug, issue_number) — keep the *last*
        // occurrence so that a newer closed entry (from the gh --state=closed
        // query) beats a stale open entry (from --state=open) when the two
        // queries race around an issue that just closed.  Without this, the
        // same issue can appear in both "in-progress" (open copy) and "done"
        // (closed copy) simultaneously during the transition window.
        let mut dedup: std::collections::HashMap<(String, u64), usize> =
            std::collections::HashMap::new();
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if Self::pipeline_repo_key(issue) == repo_key
                && !self
                    .pipeline_dismissed
                    .contains(&(issue.repo_slug.clone(), issue.number))
            {
                // Last write wins — later index = more recent entry from fetch.
                dedup.insert((issue.repo_slug.clone(), issue.number), i);
            }
        }

        let mut result: Vec<(&'static str, Vec<usize>)> = Vec::new();
        for &lc in &VISIBLE_LIFECYCLE {
            let mut idxs: Vec<usize> = dedup
                .values()
                .copied()
                .filter(|&i| {
                    let issue = &self.pipeline_issues[i];
                    self.pipeline_lifecycle_section(issue) == lc
                        // FILTER box: drop non-matching issues so empty
                        // lifecycle groups (and, in the rebuild, empty repo
                        // sections) fall away — matching Board behavior.
                        && self.pipeline_search.matches(issue.number, &issue.title)
                })
                .collect();
            // Sort to restore stable ordering by original position in pipeline_issues.
            idxs.sort_unstable();
            if !idxs.is_empty() {
                result.push((lc, idxs));
            }
        }
        result
    }

    /// Capture the (repo_slug, issue_number) of the currently-selected
    /// pipeline issue.  Callers that are about to replace
    /// `self.pipeline_issues` MUST call this first, then pass the result
    /// into `rebuild_pipeline_sidebar` — otherwise the rebuild's own
    /// internal lookup reads the stale `pipeline_sel` index against the
    /// fresh `pipeline_issues` list and gets either the wrong issue or
    /// `None`, defaulting the selection to issue #0.  That's the bug
    /// behind "the 15-second refresh keeps jumping me back to the top".
    ///
    /// #1199 fix (selection snaps to the epic on refresh): derive the id
    /// from the sidebar's *selected tree path* rather than from
    /// `pipeline_sel`.  A nested epic-child row that carries none of the
    /// tracked labels (so it's absent from `pipeline_issues`) resolves to
    /// `pipeline_sel = None` — the old capture then returned `None`, the
    /// rebuild found nothing to restore, and the default-select snapped the
    /// selection to the epic parent.  Reading the path lets us capture the
    /// nested child's own `(epic_repo_slug, child_number)` even when it isn't
    /// independently tracked, so `rebuild_pipeline_sidebar` can restore the
    /// nested row (see `locate_pipeline_selection`).  Falls back to the
    /// `pipeline_sel` mapping when the path can't be decoded.
    pub(crate) fn capture_pipeline_selection_id(&self) -> Option<(String, u64)> {
        // `pipeline_sel` is authoritative and stays in sync with the tree path
        // in production (both derive from `selected_pipeline_index`). Use it
        // first — this preserves the established behaviour for every ordinary
        // (top-level, or tracked-nested) selection, and — critically — takes
        // priority over any active Pipeline document tab. A doc tab merely
        // being open elsewhere must never outrank an actual, deliberate tree
        // selection (see #1197/#1199: selecting an untracked nested
        // epic-child row is a legitimate choice that leaves `pipeline_sel`
        // `None` on purpose; a doc-tab-first check here would silently
        // redirect the next rebuild's restore — and therefore every
        // detail-pane render and per-issue action keyed off `pipeline_sel`
        // — onto the tab's issue instead of the row actually highlighted).
        if let Some(id) = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|i| (i.repo_slug.clone(), i.number))
        {
            return Some(id);
        }
        // Fallback (#1199): `pipeline_sel` is `None` even though a row IS
        // selected — the only production cause is a nested epic-child row that
        // carries none of the tracked labels, so it's absent from
        // `pipeline_issues` and `resolve_nested_child_index` returned `None`.
        // Recover its own `(epic_repo_slug, child_number)` from the tree path
        // so `rebuild_pipeline_sidebar` can keep the child selected on the
        // next refresh instead of snapping to the epic parent.
        if let Some(id) = self.selected_nested_child_id() {
            return Some(id);
        }
        // #2452: only once the tree genuinely has NO selection of its own
        // (neither `pipeline_sel` nor a nested untracked child resolved)
        // does an active Pipeline document tab become the fallback "durable
        // selection intent". `rebuild_pipeline_sidebar` leaves the tree in
        // exactly this state the moment the active tab's row is hidden by
        // `pipeline_search` (an explicit no-selection, not a stale one — see
        // its target-restore block). Without this fallback, the very next
        // `rebuild_pipeline_sidebar(None)` call — fired by something as
        // unrelated as the next filter keystroke, a collapse toggle, or the
        // periodic refresh — would treat that as "no target was ever
        // requested" and let the default-select-first-row block snap onto
        // some other, unrelated issue (see the multi-keystroke acceptance
        // coverage: each further keystroke must keep re-attempting to
        // locate THIS SAME target, not silently drop it after the first
        // keystroke that hides it). This mirrors Board's own
        // `board_detail_issue_group`, which likewise only falls back to the
        // active tab once the tree-cursor lookup comes up empty.
        self.pipeline_doc_active_key().cloned()
    }

    /// Resolve the sidebar's current selection to a nested epic-child's own
    /// `(epic_repo_slug, child_number)` — **only** when the selected row is a
    /// nested child (its path is one segment deeper than the state's base
    /// depth). Returns `None` for a plain top-level row, so it's a safe,
    /// narrowly-scoped fallback for [`Self::capture_pipeline_selection_id`]
    /// (a top-level row is always covered by `pipeline_sel`).
    ///
    /// Mirrors [`Self::selected_pipeline_index`]'s path decoding but yields
    /// the child's issue *number* (from `epic_children`) rather than a
    /// `pipeline_issues` index, so it works even for a child absent from
    /// `pipeline_issues`.
    pub(crate) fn selected_nested_child_id(&self) -> Option<(String, u64)> {
        let search_offset = 1usize;
        let section = self.pipeline_sidebar.active_section()?;
        if section < search_offset {
            return None;
        }
        let state_idx = section - search_offset;
        let &state_key = self.pipeline_state_section_names.get(state_idx)?;
        let path = self.pipeline_sidebar.selected_path(section)?;

        // Resolve the base issue index (the epic at the state's base depth)
        // plus require the trailing nested-child segment. #1487: `in_progress`
        // marks the leaves whose nested children were filtered + reordered
        // (In-progress only — see `resolve_nested_child_index`'s doc comment).
        let (base_depth, parent_idx, in_progress) = match state_key {
            "new" => {
                if path.len() < 3 {
                    return None;
                }
                let repo_groups = self.pipeline_repos_for_state("new");
                let (_, repo_issue_idxs) = repo_groups.get(path[0] as usize)?;
                let milestones = self.pipeline_milestones_for_issues(repo_issue_idxs);
                let (_, _, mil) = milestones.get(path[1] as usize)?;
                (3usize, mil.get(path[2] as usize).copied()?, false)
            }
            "in-progress" => {
                if path.len() < 3 {
                    return None;
                }
                let groups = self.pipeline_active_by_repo();
                let (_, issue_idxs) = groups.get(path[0] as usize)?;
                let milestones = self.pipeline_milestones_for_issues(issue_idxs);
                let (_, _, mil) = milestones.get(path[1] as usize)?;
                (3usize, mil.get(path[2] as usize).copied()?, true)
            }
            "refining" | "pending" => {
                if path.len() < 2 {
                    return None;
                }
                let groups = self.pipeline_repos_for_state(state_key);
                let (_, issue_idxs) = groups.get(path[0] as usize)?;
                (2usize, issue_idxs.get(path[1] as usize).copied()?, false)
            }
            _ => return None,
        };

        // Only a nested child (path deeper than the base) is our concern.
        if path.len() <= base_depth {
            return None;
        }
        let parent = self.pipeline_issues.get(parent_idx)?;
        let child_pos = path[base_depth] as usize;
        let child_number = if in_progress {
            // #1281 / #1487: the parent rendered its In-progress-FILTERED,
            // in-flight-first-SORTED child list — `child_pos` indexes into
            // that subsequence, not the raw `epic_children_for` order.
            // Resolve the rows from `epic_child_rows_for` and run them
            // through the same `in_progress_child_rows` the renderer +
            // `resolve_nested_child_index` both use so the three sites agree
            // on what child sits at position `child_pos`.
            let repo = parent.coord_repo.as_deref()?;
            let rows = self.epic_child_rows_for(repo, parent.number);
            if rows.is_empty() {
                return None;
            }
            Self::in_progress_child_rows(&rows).get(child_pos)?.number
        } else {
            self.epic_children_for(parent)?.get(child_pos)?.number
        };
        Some((parent.repo_slug.clone(), child_number))
    }

    /// Compute a short uppercase repo tag for display in the Active section.
    ///
    /// If `repo_name`'s first character is unique (case-insensitive) among
    /// `all_repos`, returns that single character uppercased.  On collision,
    /// extends to the shortest prefix of `repo_name` that is not shared by
    /// any other entry in `all_repos`; the first character of the prefix is
    /// uppercased and the rest are kept lowercase.
    ///
    /// Examples:
    /// - `["quadraui"]` → `"Q"`
    /// - `["claude-coordinator", "coord-something"]` → `"Cl"` / `"Co"`
    pub(crate) fn repo_tag(repo_name: &str, all_repos: &[String]) -> String {
        let Some(first_char) = repo_name.chars().next() else {
            return "?".to_string();
        };
        let first_lower = first_char.to_ascii_lowercase();

        // Check if any OTHER repo starts with the same letter.
        let has_conflict = all_repos.iter().any(|r| {
            r.as_str() != repo_name
                && r.chars().next().map(|c| c.to_ascii_lowercase()) == Some(first_lower)
        });

        if !has_conflict {
            // No collision — single uppercase char.
            return first_char.to_uppercase().collect();
        }

        // Find the shortest prefix of repo_name that is unique across all_repos.
        for len in 2..=repo_name.len() {
            if !repo_name.is_char_boundary(len) {
                continue;
            }
            let prefix_lower = repo_name[..len].to_lowercase();
            let collision = all_repos
                .iter()
                .any(|r| r.as_str() != repo_name && r.to_lowercase().starts_with(&prefix_lower));
            if !collision {
                let mut chars = repo_name[..len].chars();
                let tag: String = chars
                    .next()
                    .map(|c| {
                        let upper: String = c.to_uppercase().collect();
                        upper
                    })
                    .unwrap_or_default()
                    + chars.as_str();
                return tag;
            }
        }

        // Fallback: full name with first char uppercased.
        let mut chars = repo_name.chars();
        chars
            .next()
            .map(|c| {
                let upper: String = c.to_uppercase().collect();
                upper + chars.as_str()
            })
            .unwrap_or_default()
    }

    /// Return a sorted list of `pipeline_issues` indices for all in-progress
    /// issues across all repos.  Applies dedup (last-write-wins by
    /// `(repo_slug, issue_number)`), search filter, and dismissal filter.
    pub(crate) fn pipeline_active_issues(&self) -> Vec<usize> {
        self.pipeline_active_issues_impl(true)
    }

    /// Shared implementation behind [`Self::pipeline_active_issues`] and
    /// [`Self::pipeline_jump_target`]. `apply_search` controls whether the
    /// currently-typed Pipeline search filter is applied.
    ///
    /// #2449: `pipeline_jump_target` calls this with `apply_search = false`
    /// so "View in Pipeline" can open/activate a document tab for an issue
    /// the search filter is currently hiding from the sidebar tree — a tab
    /// doesn't need the issue's row to survive the filter the way a sidebar
    /// selection does. Every other caller keeps `apply_search = true`.
    fn pipeline_active_issues_impl(&self, apply_search: bool) -> Vec<usize> {
        let mut dedup: std::collections::HashMap<(String, u64), usize> =
            std::collections::HashMap::new();
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if !self
                .pipeline_dismissed
                .contains(&(issue.repo_slug.clone(), issue.number))
            {
                dedup.insert((issue.repo_slug.clone(), issue.number), i);
            }
        }
        let mut idxs: Vec<usize> = dedup
            .values()
            .copied()
            .filter(|&i| {
                let issue = &self.pipeline_issues[i];
                self.pipeline_lifecycle_section(issue) == "in-progress"
                    && (!apply_search || self.pipeline_search.matches(issue.number, &issue.title))
            })
            .collect();
        idxs.sort_unstable();
        idxs
    }

    /// Whether a live claude session currently exists for `issue` — either an
    /// interactive `coord-<id>` tmux session (local OR remote, via
    /// `live_tmux_sessions`) or a headless `claude -p` worker whose DB
    /// assignment still shows `status="running"` (#897).
    ///
    /// The tmux check is matched precisely by coord-local repo + issue number
    /// so a same-number session in another repo doesn't false-positive (#480).
    /// A session counts as live whether or not a human is attached.
    ///
    /// The running-assignment check covers ALL **headless** assignment types
    /// (work / review / smoke / conflict-fix) — any active headless worker
    /// makes the issue Live. Interactive/chat assignments are excluded
    /// (#1097): unlike headless `claude -p` workers, nothing in the daemon
    /// proactively re-checks whether an interactive tmux session died
    /// uncleanly, so a stale `status="running"` row would otherwise pin the
    /// issue "Live" forever. Interactive liveness is decided solely by
    /// `has_tmux_session`, which self-corrects once the real tmux discovery
    /// sweep stops finding the session.
    pub(crate) fn issue_session_is_live(&self, issue: &PipelineIssue) -> bool {
        self.session_is_live(
            issue.coord_repo.as_deref(),
            Self::pipeline_repo_key(issue),
            issue.number,
        )
    }

    /// #1269: whether an epic *child* (an `EpicChild`/`EpicChildRow`, which
    /// carries only an issue number — no full `PipelineIssue`) currently has
    /// a live claude session. A child always shares its parent epic's
    /// `coord_repo` (`compute_epic_nesting` derives the same `repo_name` for
    /// every child it resolves), so the epic's `coord_repo` doubles as both
    /// the assignment-repo gate and the tmux repo-key match — the two
    /// separate parameters `session_is_live` takes for a full `PipelineIssue`
    /// (whose `repo_slug` fallback doesn't apply here) collapse into one.
    ///
    /// #1487: used by `active_issue_in_flight` to decide whether an epic's
    /// top-level In-progress row reads `in-flight` or `ready` — an epic
    /// almost never carries a session of its own, so its status comes from
    /// its children.  (Before #1487 this drove the Live/Idle *grouping*.)
    pub(crate) fn child_session_is_live(&self, epic_coord_repo: &str, child_number: u64) -> bool {
        self.session_is_live(Some(epic_coord_repo), epic_coord_repo, child_number)
    }

    /// Shared core of `issue_session_is_live` / `child_session_is_live`
    /// (#1269 extraction — see `issue_session_is_live`'s original doc
    /// comment, reproduced here, for the full rationale):
    ///
    /// Interactive path: a coord-<id> tmux session exists.
    ///
    /// #935 Part A: a "pending-" optimistic entry only counts as live while
    /// either (a) it is within the sweep budget (newly launched, session not
    /// yet discoverable) OR (b) a running assignment independently confirms
    /// the work is active.  A phantom pending- that exhausted its budget
    /// without a real session appearing no longer makes the card "Live".
    ///
    /// `coord_repo` gates the assignment-status match (`None` matches any
    /// repo — mirrors `issue_session_is_live`'s old `unwrap_or(true)`
    /// fallback for an issue with no local repo mapping; a child always
    /// passes `Some`). `repo_key` is what the tmux session list is matched
    /// against (`pipeline_repo_key`'s coord_repo-or-repo_slug fallback for a
    /// full issue; identical to `coord_repo` for a child).
    fn session_is_live(&self, coord_repo: Option<&str>, repo_key: &str, issue_number: u64) -> bool {
        // #1553: `effective_issue_number` so the `in-flight` badge lights up
        // on the CHILD issue whose acceptance slice is actually running,
        // rather than on the milestone's tracking issue that the slice's
        // `issue_number` carries.
        let repo_matches = |a: &&Assignment| {
            a.effective_issue_number() == issue_number
                && coord_repo.map(|r| r == a.repo).unwrap_or(true)
                && a.status == "running"
        };
        // Unrestricted: any running assignment (interactive or headless) that
        // matches this issue+repo. Used only to confirm a freshly-launched
        // "pending-" tmux entry is real (#935) — an interactive assignment is
        // exactly what backs a pending- entry (see `sessions.rs`'s
        // `build_interactive_launch_cmd`), so gating this on `!is_interactive`
        // would make the pending-budget escape hatch dead code for its only
        // real-world caller (#1097 review finding #1).
        let has_any_running_assignment = self
            .data
            .assignments
            .iter()
            .any(|a| repo_matches(&a));
        // Headless-only: excludes interactive/chat assignments (#1097).
        // Unlike headless `claude -p` workers, nothing in the daemon
        // proactively re-checks whether an interactive tmux session died
        // uncleanly, so a stale `status="running"` row on an interactive
        // assignment must not, by itself, pin the issue "Live" forever.
        let has_running_headless_assignment = self
            .data
            .assignments
            .iter()
            .any(|a| repo_matches(&a) && !a.is_interactive);

        let has_tmux_session = self.live_tmux_sessions.iter().any(|s| {
            if s.issue_number != Some(issue_number) || s.repo_name.as_deref() != Some(repo_key) {
                return false;
            }
            if s.assignment_id.starts_with("pending-") {
                // Pending entry: live only while under budget OR assignment
                // confirms it (the headless-path guard from #897). Any
                // running assignment (including interactive) confirms it
                // here, since pending- entries are always interactive.
                s.pending_sweep_count <= Self::PENDING_SESSION_SWEEP_BUDGET
                    || has_any_running_assignment
            } else {
                true // real session from discovery always counts
            }
        });
        if has_tmux_session {
            return true;
        }
        // Headless path (#897): a `claude -p` worker (is_interactive=false)
        // has no tmux session but is actively running. Any non-interactive
        // assignment with status="running" marks the issue as Live;
        // interactive assignments rely solely on `has_tmux_session` above
        // (#1097).
        has_running_headless_assignment
    }

    /// #1487: whether a top-level In-progress row should read `in-flight`
    /// (rather than `ready`).
    ///
    /// A plain (non-epic) issue is keyed off its own session
    /// (`issue_session_is_live` — tmux session or running headless worker).
    ///
    /// An **epic** tracking issue almost never carries a session of its own
    /// (the work happens on its children), so an epic is `in-flight` when
    /// *any* of its children is live — the same child-liveness classification
    /// #1269 used to build the old Live/Idle split, now applied per row
    /// instead of as a grouping axis.  An epic that does happen to carry its
    /// own session still counts.
    pub(crate) fn active_issue_in_flight(&self, idx: usize) -> bool {
        let Some(issue) = self.pipeline_issues.get(idx) else {
            return false;
        };
        if let Some(children) = self.epic_children_for(issue) {
            let repo = issue.coord_repo.as_deref().unwrap_or_default();
            if children
                .iter()
                .any(|child| self.child_session_is_live(repo, child.number))
            {
                return true;
            }
        }
        self.issue_session_is_live(issue)
    }

    /// #1487: group the in-progress ("Active") issues by **repo** — the same
    /// grouping the New / Refining / Pending sections use — in the same
    /// insertion-order-of-`pipeline_issues` repo order
    /// (`pipeline_repos_for_state`).  Only non-empty groups are returned,
    /// mirroring `pipeline_repos_for_state`'s `(group_key, issue_indices)`
    /// shape so the nested `[group, …]` path handling is identical for every
    /// Pipeline section.
    ///
    /// This replaces the pre-#1487 `pipeline_active_by_liveness` Live/Idle
    /// split.  Liveness is a *per-row attribute*, not a grouping axis: with
    /// the old split an epic with one live and three idle children rendered
    /// **twice**, once under each group, each instance showing only its half
    /// of the children and its own duplicate `N/M done` badge.  Grouping by
    /// repo renders every epic exactly once with all of its non-Done children
    /// beneath it; the liveness signal survives as the per-row
    /// `in-flight`/`ready` badge (`active_issue_in_flight`).
    ///
    /// Within a repo group the issues are ordered **in-flight first, then
    /// ready**, ties broken by `pipeline_issues` index so the order is
    /// stable.  `pipeline_milestones_for_issues` preserves the order of the
    /// slice it is given, so each milestone bucket inherits the same sort —
    /// and so do both selection decoders, which resolve through the very
    /// same two functions.
    pub(crate) fn pipeline_active_by_repo(&self) -> Vec<(String, Vec<usize>)> {
        // Repo order from pipeline_issues (stable insertion order) — matches
        // `pipeline_repos_for_state`, so In-progress lists repos in the same
        // order as every other section.
        let mut repos: Vec<String> = Vec::new();
        for issue in &self.pipeline_issues {
            let key = Self::pipeline_repo_key(issue).to_string();
            if !repos.contains(&key) {
                repos.push(key);
            }
        }
        let active = self.pipeline_active_issues();
        let mut result: Vec<(String, Vec<usize>)> = Vec::new();
        for repo_key in repos {
            let mut ranked: Vec<(u8, usize)> = active
                .iter()
                .copied()
                .filter(|&i| Self::pipeline_repo_key(&self.pipeline_issues[i]) == repo_key.as_str())
                .map(|i| (if self.active_issue_in_flight(i) { 0 } else { 1 }, i))
                .collect();
            // (rank, idx) sorts in-flight above ready and keeps the original
            // issue order within a rank.
            ranked.sort_unstable();
            if !ranked.is_empty() {
                result.push((repo_key, ranked.into_iter().map(|(_, i)| i).collect()));
            }
        }
        result
    }

    // ── #728: Done-section windowing helpers ─────────────────────────────────

    /// Compute the "done-at" timestamp for an issue: the moment it actually
    /// *entered* Done, for the Done section's recency window + sort (#913).
    ///
    /// Preferred source: a `state == "merged"` `merge_queue` entry's
    /// `last_attempt` — `coord/merge_queue.py` sets that field immediately
    /// before the merge attempt and never touches it again on success, so
    /// for a merged entry it IS the merge time. A `passed`/`skipped` smoke
    /// test can sit for hours or days before review + merge actually lands
    /// it, so keying off work-completion time hides freshly-merged items
    /// behind a stale-looking timestamp — see #913.
    ///
    /// Falls back to the **max** `finished_at` across all assignments for
    /// the issue (by issue number + coord-repo) when no merged queue entry
    /// exists — e.g. the merge_queue row was pruned after reconcile flipped
    /// the assignment to `status == "merged"`, or the issue was closed on
    /// GitHub with no coord-driven merge at all.
    ///
    /// Returns `None` when neither source has a timestamp (e.g. rows predate
    /// the relevant column). #2405: such issues have no END time, so the
    /// completed-issues grid — which windows on exactly this value — cannot
    /// place them in any time range and drops them.
    pub(crate) fn issue_done_at(&self, issue: &PipelineIssue) -> Option<f64> {
        if let Some(merged_at) = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug)
            .filter(|m| m.state == "merged")
            .and_then(|m| m.last_attempt)
        {
            return Some(merged_at);
        }
        let local_repo = issue.coord_repo.as_deref();
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == *r,
                None => true,
            })
            .filter_map(|a| a.finished_at)
            .reduce(f64::max)
    }

    /// #2405: earliest moment any worker was dispatched for `issue` — the
    /// grid's START column.
    ///
    /// The mirror image of [`Self::issue_done_at`]: that one reduces with
    /// `max` over `finished_at` (when did it *stop*), this one with `min` over
    /// `dispatched_at` (when did it *first start*). Scoped by coord-local repo
    /// the same way, so a same-number issue in another repo can't contribute.
    pub(crate) fn issue_started_at(&self, issue: &PipelineIssue) -> Option<f64> {
        let local_repo = issue.coord_repo.as_deref();
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == *r,
                None => true,
            })
            .filter_map(|a| a.dispatched_at)
            .reduce(f64::min)
    }

    // ── #2405: the completed-issues grid ─────────────────────────────────
    //
    // #728's Done section listed whatever had exited the pipeline inside one
    // fixed window, with no columns, no sort, and no time range beyond the
    // `→`-cycled label. This replaces it with the shape the Reports panel
    // (#1741/#1911/#1762/#1853) already settled on for exactly this problem:
    // a time range + repo filter over a sortable `DataTable`.
    //
    // **Why this is Pipeline-local rather than a `coord/reports.py` entry.**
    // The catalogue route was investigated first (it is that module's stated
    // extension point, and `reports.rs`'s header makes "adding report #2
    // requires zero `tui/**` changes" load-bearing). It cannot carry this
    // feature: the acceptance requirement is that a **row click opens the
    // Pipeline's own Overview detail** for that issue, and the Reports panel
    // has no per-report click behaviour by construction — wiring one in would
    // mean a `match` on a report id inside `reports.rs`, which is precisely
    // the property that module exists to protect. So the grid borrows the
    // *widgets and the control vocabulary* (`SINCE_PRESETS`, free-form
    // durations, empty-`until`-means-now, empty-repo-means-all — all copied
    // from `ISSUE_ACTIVITY` in `coord/reports.py`) without the round trip.
    // A `completed-issues` catalogue entry remains a perfectly good separate
    // addition for the Reports panel; it just isn't this.

    /// Time-range presets, byte-identical to `coord/reports.py`'s
    /// `SINCE_PRESETS` so the two controls can't drift apart. Any free-form
    /// duration (`90m`, `13h`, …) is accepted too — see
    /// [`Self::parse_duration_secs`].
    pub(crate) const COMPLETED_SINCE_PRESETS: [&'static str; 5] =
        ["1h", "6h", "24h", "3d", "7d"];

    /// Column order of the completed grid. The index positions are the sort
    /// keys `CompletedGrid::sort` stores, so they are part of that state's
    /// meaning — do not reorder without migrating it.
    pub(crate) const COMPLETED_COLUMNS: [&'static str; 4] =
        ["ISSUE", "TITLE", "STARTED", "ENDED"];

    /// `"13h"` → `46800.0`. Units: s, m, h, d, w. `None` when the string is
    /// not a duration at all. Mirrors `coord/reports.py`'s `parse_duration`.
    pub(crate) fn parse_duration_secs(raw: &str) -> Option<f64> {
        let raw = raw.trim();
        let (digits, unit) = raw.split_at(raw.len().checked_sub(1)?);
        let n: f64 = digits.parse().ok()?;
        if n < 0.0 {
            return None;
        }
        let mult = match unit.to_ascii_lowercase().as_str() {
            "s" => 1.0,
            "m" => 60.0,
            "h" => 3600.0,
            "d" => 86_400.0,
            "w" => 604_800.0,
            _ => return None,
        };
        Some(n * mult)
    }

    /// Epoch seconds or an ISO-8601 `YYYY-MM-DD[THH:MM[:SS]][Z]` → epoch
    /// seconds. Mirrors `coord/reports.py`'s `parse_timestamp` for the shapes
    /// an operator actually types; anything else is `None`, which the grid
    /// surfaces as an inline error rather than silently treating as "now".
    ///
    /// Hand-rolled rather than pulled from `chrono` because coord-tui has no
    /// date dependency and this is the only place one would be needed. UTC
    /// only — the `Z` is optional but never means "local".
    pub(crate) fn parse_until_epoch(raw: &str) -> Option<f64> {
        let raw = raw.trim();
        if raw.is_empty() {
            return None;
        }
        if let Ok(secs) = raw.parse::<f64>() {
            return Some(secs);
        }
        let s = raw.trim_end_matches('Z').trim_end_matches('z');
        let (date, time) = match s.split_once(['T', ' ']) {
            Some((d, t)) => (d, t),
            None => (s, "00:00:00"),
        };
        let mut dparts = date.split('-');
        let y: i64 = dparts.next()?.parse().ok()?;
        let mo: i64 = dparts.next()?.parse().ok()?;
        let d: i64 = dparts.next()?.parse().ok()?;
        if dparts.next().is_some() || !(1..=12).contains(&mo) || !(1..=31).contains(&d) {
            return None;
        }
        let mut tparts = time.split(':');
        let hh: i64 = tparts.next()?.parse().ok()?;
        let mm: i64 = tparts.next().unwrap_or("0").parse().ok()?;
        let ss: f64 = tparts.next().unwrap_or("0").parse().ok()?;
        if tparts.next().is_some() || hh > 23 || mm > 59 || !(0.0..60.0).contains(&ss) {
            return None;
        }
        // Howard Hinnant's `days_from_civil` — the standard branch-free
        // civil-date → epoch-day conversion, valid for any proleptic
        // Gregorian date.
        let y = if mo <= 2 { y - 1 } else { y };
        let era = if y >= 0 { y } else { y - 399 } / 400;
        let yoe = y - era * 400;
        let doy = (153 * (if mo > 2 { mo - 3 } else { mo + 9 }) + 2) / 5 + d - 1;
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        let days = era * 146_097 + doe - 719_468;
        Some(days as f64 * 86_400.0 + (hh * 3600 + mm * 60) as f64 + ss)
    }

    /// The grid's resolved `[start, end]` window in epoch seconds, or an
    /// error string naming the control that failed to parse.
    ///
    /// Empty `until` means now (same rule as `ISSUE_ACTIVITY`), so the window
    /// always ends at a real instant — a row with no END timestamp at all can
    /// therefore never land in any range, and is dropped.
    pub(crate) fn completed_window(&self) -> Result<(f64, f64), String> {
        let end = if self.completed_grid.until.trim().is_empty() {
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64()
        } else {
            Self::parse_until_epoch(&self.completed_grid.until).ok_or_else(|| {
                format!(
                    "Window end {:?} is not epoch seconds or an ISO-8601 timestamp \
                     (e.g. 2026-08-03T09:16:00Z). Leave it empty for now.",
                    self.completed_grid.until.trim()
                )
            })?
        };
        let span = Self::parse_duration_secs(&self.completed_grid.since).ok_or_else(|| {
            format!(
                "Time range {:?} is not a duration — use one of {}, or e.g. 13h \
                 (units: s, m, h, d, w).",
                self.completed_grid.since.trim(),
                Self::COMPLETED_SINCE_PRESETS.join(", ")
            )
        })?;
        Ok((end - span, end))
    }

    /// Options for the repo selector: `""` (all repos) followed by every
    /// distinct repo that has at least one *completed* issue, in stable
    /// first-seen order.
    ///
    /// Derived from the completed set rather than from every repo on the
    /// board so the selector can never offer a filter that blanks the grid.
    /// A repo currently named in `completed_grid.repo` but absent from that
    /// set is still appended, so a stale selection stays visible (and
    /// steppable back out of) instead of silently reading as "all repos".
    pub(crate) fn completed_repo_choices(&self) -> Vec<String> {
        let mut out: Vec<String> = vec![String::new()];
        for issue in &self.pipeline_issues {
            if self.pipeline_lifecycle_section(issue) != "done" {
                continue;
            }
            let key = Self::pipeline_repo_key(issue).to_string();
            if !out.contains(&key) {
                out.push(key);
            }
        }
        let sel = self.completed_grid.repo.trim();
        if !sel.is_empty() && !out.iter().any(|r| r == sel) {
            out.push(sel.to_string());
        }
        out
    }

    /// Every completed issue inside the current window and repo filter, as
    /// grid rows in the currently-selected sort order.
    ///
    /// Shares the Done section's exclusion rules verbatim — dedup by
    /// `(repo_slug, number)` last-write-wins, the sidebar search filter, and
    /// the `D`-key dismissal set — so an issue hidden from the Pipeline stays
    /// hidden here. Returns the window error (never a partial list) when a
    /// control doesn't parse.
    pub(crate) fn completed_rows(&self) -> Result<Vec<CompletedRow>, String> {
        let (start, end) = self.completed_window()?;
        let repo_filter = self.completed_grid.repo.trim();

        let mut dedup: std::collections::HashMap<(String, u64), usize> =
            std::collections::HashMap::new();
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if !self
                .pipeline_dismissed
                .contains(&(issue.repo_slug.clone(), issue.number))
                && self.pipeline_search.matches(issue.number, &issue.title)
            {
                dedup.insert((issue.repo_slug.clone(), issue.number), i);
            }
        }

        // `repo_tag` is context-dependent (it shortens against the set it is
        // given), so feed it the repos actually on screen — every repo in the
        // *unfiltered* completed set — rather than the post-filter subset.
        // Otherwise picking a repo would silently re-shorten every ref.
        let all_repos: Vec<String> = self
            .completed_repo_choices()
            .into_iter()
            .filter(|r| !r.is_empty())
            .collect();

        let mut rows: Vec<CompletedRow> = dedup
            .values()
            .copied()
            .filter(|&i| self.pipeline_lifecycle_section(&self.pipeline_issues[i]) == "done")
            .filter(|&i| {
                repo_filter.is_empty()
                    || Self::pipeline_repo_key(&self.pipeline_issues[i]) == repo_filter
            })
            .filter_map(|i| {
                let issue = &self.pipeline_issues[i];
                let finished_at = self.issue_done_at(issue)?;
                if finished_at < start || finished_at > end {
                    return None;
                }
                let tag = Self::repo_tag(Self::pipeline_repo_key(issue), &all_repos);
                Some(CompletedRow {
                    idx: i,
                    id: (issue.repo_slug.clone(), issue.number),
                    issue_ref: format!("{}#{}", tag, issue.number),
                    title: issue.title.clone(),
                    started_at: self.issue_started_at(issue),
                    finished_at: Some(finished_at),
                })
            })
            .collect();

        Self::sort_completed_rows(&mut rows, self.completed_grid.sort);
        Ok(rows)
    }

    /// Apply `sort` to `rows` in place. `None` is the default order —
    /// newest-finished first, which is what the old Done section showed.
    ///
    /// A secondary key of `issue_ref` keeps the order total, so a repaint
    /// can never reshuffle rows that tie on the sort column (two issues that
    /// merged in the same second, or a whole column of `—`).
    pub(crate) fn sort_completed_rows(rows: &mut [CompletedRow], sort: Option<(usize, bool)>) {
        use std::cmp::Ordering;
        // `None` sorts last in *both* directions: a missing timestamp is an
        // absence, not a small value, and burying it under an ascending sort
        // is how #1300-style "where did my row go" reports start.
        fn opt_cmp(a: Option<f64>, b: Option<f64>) -> Ordering {
            match (a, b) {
                (Some(x), Some(y)) => x.partial_cmp(&y).unwrap_or(Ordering::Equal),
                (Some(_), None) => Ordering::Less,
                (None, Some(_)) => Ordering::Greater,
                (None, None) => Ordering::Equal,
            }
        }
        let Some((col, ascending)) = sort else {
            rows.sort_by(|a, b| {
                opt_cmp(b.finished_at, a.finished_at).then_with(|| a.issue_ref.cmp(&b.issue_ref))
            });
            return;
        };
        rows.sort_by(|a, b| {
            let primary = match col {
                0 => a.issue_ref.cmp(&b.issue_ref),
                1 => a.title.to_lowercase().cmp(&b.title.to_lowercase()),
                2 => opt_cmp(a.started_at, b.started_at),
                _ => opt_cmp(a.finished_at, b.finished_at),
            };
            let primary = if ascending { primary } else { primary.reverse() };
            primary.then_with(|| a.issue_ref.cmp(&b.issue_ref))
        });
    }

    // ── #2405: completed grid — controls ─────────────────────────────────

    /// Widget id for control `field` (`"since"` / `"until"` / `"repo"`).
    pub(crate) fn completed_field_id(field: &str) -> WidgetId {
        WidgetId::new(format!("completed:{field}"))
    }

    /// Inverse of [`Self::completed_field_id`]: the field index for a widget
    /// id, or `None` when the id belongs to something else.
    pub(crate) fn completed_field_index(id: &str) -> Option<usize> {
        match id.strip_prefix("completed:")? {
            "since" => Some(0),
            "until" => Some(1),
            "repo" => Some(2),
            _ => None,
        }
    }

    /// The time-range segments: the shared presets, plus the live value when
    /// it is a free-form duration, so a `13h` window is still visible (and
    /// still selected) instead of silently reading as `1h`.
    pub(crate) fn completed_since_options(&self) -> Vec<String> {
        let mut out: Vec<String> = Self::COMPLETED_SINCE_PRESETS
            .iter()
            .map(|s| s.to_string())
            .collect();
        let cur = self.completed_grid.since.trim();
        if !cur.is_empty() && !out.iter().any(|o| o == cur) {
            out.push(cur.to_string());
        }
        out
    }

    /// Choice params with at most this many options render as a segmented
    /// control (every option one click away); more and the row would not
    /// fit, so they fall back to a dropdown. Same rule and same reason as
    /// `reports.rs`'s `REPORTS_SEGMENTED_MAX`.
    const COMPLETED_SEGMENTED_MAX: usize = 6;

    /// The control row: time range / window end / repo, in that order.
    ///
    /// The field order **is** the `Tab` focus order and `field_sel` indexes
    /// into it, exactly like `reports_param_form`.
    pub(crate) fn completed_form(&self) -> Form {
        let since_options = self.completed_since_options();
        let since_idx = since_options
            .iter()
            .position(|o| o == self.completed_grid.since.trim())
            .unwrap_or(0);
        let repo_options = self.completed_repo_choices();
        let repo_idx = repo_options
            .iter()
            .position(|o| o == self.completed_grid.repo.trim())
            .unwrap_or(0);
        // `""` is a real, selectable option (all repos) but an empty segment
        // is invisible, so it is *displayed* as `(all)` while the stored
        // value stays empty.
        let repo_labels: Vec<String> = repo_options
            .iter()
            .map(|r| {
                if r.is_empty() {
                    "(all)".to_string()
                } else {
                    r.clone()
                }
            })
            .collect();
        let repo_kind = if repo_labels.len() <= Self::COMPLETED_SEGMENTED_MAX {
            FieldKind::SegmentedControl {
                options: repo_labels,
                selected_idx: repo_idx,
            }
        } else {
            FieldKind::Dropdown {
                options: repo_labels
                    .iter()
                    .map(|c| StyledText::plain(c.as_str()))
                    .collect(),
                selected_idx: repo_idx,
            }
        };
        let fields = vec![
            FormField {
                id: Self::completed_field_id("since"),
                label: StyledText::plain("Time range"),
                kind: FieldKind::SegmentedControl {
                    options: since_options,
                    selected_idx: since_idx,
                },
                hint: StyledText::plain(""),
                disabled: false,
                validation: None,
            },
            FormField {
                id: Self::completed_field_id("until"),
                label: StyledText::plain("Window end"),
                kind: FieldKind::TextInput {
                    value: self.completed_grid.until.clone(),
                    placeholder: "epoch seconds or ISO-8601 — empty means now".to_string(),
                    cursor: None,
                    selection_anchor: None,
                },
                hint: StyledText::plain(""),
                disabled: false,
                validation: None,
            },
            FormField {
                id: Self::completed_field_id("repo"),
                label: StyledText::plain("Repo"),
                kind: repo_kind,
                hint: StyledText::plain(""),
                disabled: false,
                validation: None,
            },
        ];
        let focused_field = fields
            .get(self.completed_grid.field_sel)
            .map(|f| f.id.clone());
        Form {
            id: WidgetId::new("pipeline-completed-form"),
            fields,
            focused_field,
            scroll_offset: 0,
            has_focus: true,
        }
    }

    /// Number of control fields — the modulus for `Tab` cycling.
    pub(crate) const COMPLETED_FIELD_COUNT: usize = 3;

    /// Step the focused choice control to the next/previous option (`←`/`→`).
    /// A no-op on the free-text `Window end` field. Returns `true` when the
    /// value changed.
    pub(crate) fn completed_step_choice(&mut self, forward: bool) -> bool {
        let (options, current) = match self.completed_grid.field_sel {
            0 => (
                self.completed_since_options(),
                self.completed_grid.since.trim().to_string(),
            ),
            2 => (
                self.completed_repo_choices(),
                self.completed_grid.repo.trim().to_string(),
            ),
            _ => return false,
        };
        if options.len() < 2 {
            return false;
        }
        let idx = options.iter().position(|o| *o == current).unwrap_or(0);
        let n = options.len();
        let next = if forward {
            (idx + 1) % n
        } else {
            (idx + n - 1) % n
        };
        self.completed_set_field(self.completed_grid.field_sel, options[next].clone());
        true
    }

    /// Write a control's value and reset everything downstream of it.
    ///
    /// Scroll and the open row detail both reset on *any* control change:
    /// the row set is about to change underneath them, and a scroll offset or
    /// a detail view left pointing into the old set is the "stale result"
    /// failure `render_reports_result` calls out. The **sort** deliberately
    /// survives — it is a preference about the columns, not about the rows.
    pub(crate) fn completed_set_field(&mut self, field: usize, value: String) {
        match field {
            0 => self.completed_grid.since = value,
            1 => self.completed_grid.until = value,
            2 => self.completed_grid.repo = value,
            _ => return,
        }
        self.completed_grid.scroll = 0;
        self.completed_grid.detail = None;
    }

    /// Read a control's value.
    pub(crate) fn completed_field_value(&self, field: usize) -> String {
        match field {
            0 => self.completed_grid.since.clone(),
            1 => self.completed_grid.until.clone(),
            2 => self.completed_grid.repo.clone(),
            _ => String::new(),
        }
    }

    /// `true` when the focused control is free text — the only state in which
    /// typing edits a value instead of firing the panel's single-key
    /// bindings. Same contract as `reports_focus_is_text`.
    pub(crate) fn completed_focus_is_text(&self) -> bool {
        self.completed_grid.field_sel == 1
    }

    /// Append a typed character to the focused free-text control.
    pub(crate) fn completed_text_insert(&mut self, ch: char) -> bool {
        if !self.completed_focus_is_text() {
            return false;
        }
        let mut v = self.completed_field_value(self.completed_grid.field_sel);
        v.push(ch);
        self.completed_set_field(self.completed_grid.field_sel, v);
        true
    }

    /// Delete the last character of the focused free-text control.
    pub(crate) fn completed_text_backspace(&mut self) -> bool {
        if !self.completed_focus_is_text() {
            return false;
        }
        let mut v = self.completed_field_value(self.completed_grid.field_sel);
        if v.pop().is_none() {
            return false;
        }
        self.completed_set_field(self.completed_grid.field_sel, v);
        true
    }

    /// Toggle the sort on `col`: first click sorts descending (the most
    /// useful direction for both timestamp columns, and the order the grid
    /// already opens in), a second click flips to ascending, a third clears
    /// back to the default newest-finished-first order.
    ///
    /// Same three-state cycle as `reports_sort_by_column`, so a header click
    /// means the same thing in both grids.
    pub(crate) fn completed_sort_by_column(&mut self, col: usize) -> bool {
        if col >= Self::COMPLETED_COLUMNS.len() {
            return false;
        }
        self.completed_grid.sort = match self.completed_grid.sort {
            Some((c, false)) if c == col => Some((col, true)),
            Some((c, true)) if c == col => None,
            _ => Some((col, false)),
        };
        self.completed_grid.scroll = 0;
        true
    }

    /// Open the detail view for the `n`-th row of the current grid.
    /// Returns `false` when there is no such row.
    pub(crate) fn completed_open_row(&mut self, n: usize) -> bool {
        let Ok(rows) = self.completed_rows() else {
            return false;
        };
        let Some(row) = rows.get(n) else {
            return false;
        };
        let idx = row.idx;
        self.completed_grid.detail = Some(row.id.clone());
        // The Overview body only renders its stage-content block when a stage
        // is focused, so pick this issue's own default the same way selecting
        // a sidebar row would — otherwise the detail opens on meta alone.
        self.pipeline_focused_stage = self.default_focused_stage_for(idx);
        self.pipeline_stage_content_scroll = 0;
        true
    }

    /// Close the row detail and return to the grid. `false` when no detail
    /// was open (so callers can fall through to their next Esc binding).
    pub(crate) fn completed_close_detail(&mut self) -> bool {
        if self.completed_grid.detail.is_none() {
            return false;
        }
        self.completed_grid.detail = None;
        self.pipeline_stage_content_scroll = 0;
        true
    }

    /// The `pipeline_issues` index the open row detail refers to, or `None`
    /// when nothing is open (or the issue has since left the board).
    pub(crate) fn completed_detail_index(&self) -> Option<usize> {
        let (repo, number) = self.completed_grid.detail.as_ref()?;
        self.pipeline_issues
            .iter()
            .position(|pi| pi.repo_slug == *repo && pi.number == *number)
    }

    // ── #2405: completed grid — render + hit-test ────────────────────────

    /// `1755\u{2026}` → `2026-08-18 14:03`, or `—` when there is no
    /// timestamp. Local-clock-free (UTC), matching how `coord/reports.py`
    /// stringifies a `timestamp` column.
    pub(crate) fn completed_time_cell(ts: Option<f64>) -> String {
        let Some(ts) = ts else {
            return "—".to_string();
        };
        // Inverse of `parse_until_epoch`'s `days_from_civil`.
        let days = (ts / 86_400.0).floor() as i64;
        let secs_of_day = (ts - days as f64 * 86_400.0).max(0.0) as i64;
        let z = days + 719_468;
        let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
        let doe = z - era * 146_097;
        let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = doy - (153 * mp + 2) / 5 + 1;
        let m = if mp < 10 { mp + 3 } else { mp - 9 };
        let y = if m <= 2 { y + 1 } else { y };
        format!(
            "{y:04}-{m:02}-{d:02} {:02}:{:02}",
            secs_of_day / 3600,
            (secs_of_day % 3600) / 60
        )
    }

    /// The grid's columns. `TITLE` flexes; the other three are content-sized
    /// with clamps, so a wide title can never squeeze the timestamps into
    /// illegibility (the failure #1762 fixed for the Reports table).
    pub(crate) fn completed_columns() -> Vec<Column> {
        vec![
            Column {
                title: Self::COMPLETED_COLUMNS[0].to_string(),
                width: ColumnWidth::Content { min: 8.0, max: 14.0 },
                align: ColumnAlign::Left,
            },
            Column {
                title: Self::COMPLETED_COLUMNS[1].to_string(),
                width: ColumnWidth::Flex(1.0),
                align: ColumnAlign::Left,
            },
            Column {
                title: Self::COMPLETED_COLUMNS[2].to_string(),
                width: ColumnWidth::Content { min: 16.0, max: 18.0 },
                align: ColumnAlign::Left,
            },
            Column {
                title: Self::COMPLETED_COLUMNS[3].to_string(),
                width: ColumnWidth::Content { min: 16.0, max: 18.0 },
                align: ColumnAlign::Left,
            },
        ]
    }

    /// Rows to `DataRow`s, in the order `completed_rows` produced.
    pub(crate) fn completed_data_rows(rows: &[CompletedRow]) -> Vec<DataRow> {
        rows.iter()
            .map(|r| DataRow {
                cells: vec![
                    StyledText {
                        spans: vec![StyledSpan::with_fg(
                            r.issue_ref.clone(),
                            Color::rgb(150, 150, 240),
                        )],
                    },
                    StyledText::plain(r.title.as_str()),
                    StyledText {
                        spans: vec![StyledSpan::with_fg(
                            Self::completed_time_cell(r.started_at),
                            Color::rgb(150, 150, 170),
                        )],
                    },
                    StyledText {
                        spans: vec![StyledSpan::with_fg(
                            Self::completed_time_cell(r.finished_at),
                            Color::rgb(140, 200, 140),
                        )],
                    },
                ],
                decoration: Decoration::Normal,
            })
            .collect()
    }

    /// Height of the control row, in surface units.
    fn completed_controls_height(lh: f32) -> f32 {
        Self::COMPLETED_FIELD_COUNT as f32 * lh
    }

    /// The leading text of the detail view's single "back" row. The open
    /// issue's ref and title are appended after it, so the detail says which
    /// issue it is about — the Overview body itself never names one.
    pub(crate) const COMPLETED_DETAIL_BACK_HINT: &'static str =
        "  ← Back to Completed  (Esc)";

    /// A one-line, **unbordered** caption list. `plain_list` draws a border,
    /// which on a single-row rect paints the frame's top edge and nothing
    /// else — so the detail's back row uses this instead.
    fn completed_caption_list(id: &str, text: &str) -> ListView {
        ListView {
            id: WidgetId::new(id),
            title: None,
            items: vec![ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(text, Color::rgb(170, 170, 200))],
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
        }
    }

    /// Render the `Completed` tab: either the open row's detail, or the
    /// control row above the result grid.
    pub(crate) fn render_pipeline_completed(
        &self,
        backend: &mut dyn Backend,
        rect: Rect,
        lh: f32,
    ) {
        // Drop last frame's geometry up front, exactly like
        // `render_reports_result`: every path below that paints a table
        // re-sets it, and every path that doesn't must leave a click with
        // nothing to hit rather than a table that is no longer on screen.
        *self.completed_table_layout.borrow_mut() = None;
        *self.completed_form_layout.borrow_mut() = None;

        if self.completed_grid.detail.is_some() {
            self.render_completed_detail(backend, rect, lh);
            return;
        }

        let controls_h = Self::completed_controls_height(lh).min(rect.height);
        let controls_rect = Rect::new(rect.x, rect.y, rect.width, controls_h);
        // Paint and hit-test from ONE layout, derived by the backend itself —
        // the `msv.rs` contract. Re-deriving geometry at click time is the
        // cached-layout-vs-paint drift #1094 had to fix for the Audit table.
        let form = self.completed_form();
        let form_layout = backend.form_layout(controls_rect, &form);
        backend.draw_form(controls_rect, &form);
        *self.completed_form_layout.borrow_mut() = Some((controls_rect, form_layout));

        let body_rect = Rect::new(
            rect.x,
            rect.y + controls_h,
            rect.width,
            (rect.height - controls_h).max(0.0),
        );
        if body_rect.height <= 0.0 {
            return;
        }

        let rows = match self.completed_rows() {
            Ok(rows) => rows,
            Err(reason) => {
                // A bad control value is stated, not swallowed: rendering an
                // empty grid for an unparseable `until` reads identically to
                // "nothing completed", which is the wrong answer.
                backend.draw_list(
                    body_rect,
                    &plain_list("completed-error", &format!("  {reason}"), 0),
                );
                return;
            }
        };
        if rows.is_empty() {
            backend.draw_list(
                body_rect,
                &plain_list(
                    "completed-empty",
                    &format!(
                        "  No issues completed in this window ({}{}).",
                        self.completed_grid.since.trim(),
                        if self.completed_grid.repo.trim().is_empty() {
                            String::new()
                        } else {
                            format!(", repo {}", self.completed_grid.repo.trim())
                        }
                    ),
                    0,
                ),
            );
            return;
        }

        let table = DataTable {
            id: WidgetId::new("pipeline-completed"),
            columns: Self::completed_columns(),
            rows: Self::completed_data_rows(&rows),
            selected_idx: None,
            scroll_offset: self.completed_grid.scroll.min(rows.len().saturating_sub(1)),
            sort: self.completed_grid.sort.map(|(c, asc)| {
                (
                    c,
                    if asc {
                        SortDirection::Ascending
                    } else {
                        SortDirection::Descending
                    },
                )
            }),
            has_focus: false,
            show_scrollbar: true,
            min_total_width: None,
            // Held at 0.0 for the same reason `render_reports_result` holds
            // it there: `DataTableLayout::hit_test` has no concept of
            // `h_scroll` while the renderer subtracts it, so a non-zero value
            // would shift the painted headers out from under the hit-test and
            // route sort clicks to the wrong column.
            h_scroll: 0.0,
            column_overrides: Vec::new(),
            footer: None,
        };
        let layout = backend.draw_data_table(body_rect, &table, None);
        *self.completed_table_layout.borrow_mut() = Some((body_rect, layout));
    }

    /// The open row's detail: the Overview tab's content for that issue —
    /// repo/labels/gates meta plus focused-stage content — **without** the
    /// Work/Test/Review/Merge stage-box strip and without the action bar.
    ///
    /// Both omissions are deliberate and are the point of the view: this is a
    /// historical record of an issue that already left the pipeline, so a
    /// strip inviting a click-to-focus and a `[Go]` bar inviting a dispatch
    /// would both be offering actions on something that has finished.
    fn render_completed_detail(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        let back_h = lh.min(rect.height);
        let caption = match self.completed_detail_index() {
            Some(idx) => {
                let issue = &self.pipeline_issues[idx];
                let tag = Self::repo_tag(
                    Self::pipeline_repo_key(issue),
                    &self
                        .completed_repo_choices()
                        .into_iter()
                        .filter(|r| !r.is_empty())
                        .collect::<Vec<_>>(),
                );
                format!(
                    "{}   ·   {}#{}  {}",
                    Self::COMPLETED_DETAIL_BACK_HINT,
                    tag,
                    issue.number,
                    issue.title
                )
            }
            None => Self::COMPLETED_DETAIL_BACK_HINT.to_string(),
        };
        backend.draw_list(
            Rect::new(rect.x, rect.y, rect.width, back_h),
            &Self::completed_caption_list("completed-detail-back", &caption),
        );
        let body_rect = Rect::new(
            rect.x,
            rect.y + back_h,
            rect.width,
            (rect.height - back_h).max(0.0),
        );
        match self.completed_detail_index() {
            Some(idx) => {
                backend.draw_list(body_rect, &self.pipeline_tab_body_list_for(Some(idx)))
            }
            None => backend.draw_list(
                body_rect,
                &plain_list(
                    "completed-detail-missing",
                    "  That issue is no longer on the board.",
                    0,
                ),
            ),
        }
    }

    /// Hit-test a click against the last-painted grid, or `None` when no grid
    /// is on screen. Same render-then-hit-test pattern (and same cached-rect
    /// reason) as `reports_table_hit`.
    pub(crate) fn completed_table_hit(&self, pos: Point) -> Option<DataTableHit> {
        let n = self.completed_rows().ok()?.len();
        if n == 0 {
            return None;
        }
        let cache = self.completed_table_layout.borrow();
        let (rect, layout) = cache.as_ref()?;
        Some(layout.hit_test(
            pos.x - rect.x,
            pos.y - rect.y,
            self.completed_grid.scroll,
            n,
        ))
    }

    /// How many body rows the painted grid can show — the clamp the wheel
    /// handler needs. `None` when no grid is on screen.
    pub(crate) fn completed_visible_rows(&self) -> Option<usize> {
        let cache = self.completed_table_layout.borrow();
        let (_, layout) = cache.as_ref()?;
        Some(layout.visible_rows.max(1))
    }

    /// Scroll the grid by `delta` rows, clamped to the row count. Returns
    /// `true` when the offset actually moved.
    pub(crate) fn completed_scroll_by(&mut self, delta: isize) -> bool {
        let Ok(rows) = self.completed_rows() else {
            return false;
        };
        let visible = self.completed_visible_rows().unwrap_or(1);
        let max = rows.len().saturating_sub(visible);
        let next = (self.completed_grid.scroll as isize + delta).clamp(0, max as isize) as usize;
        if next == self.completed_grid.scroll {
            return false;
        }
        self.completed_grid.scroll = next;
        true
    }

    /// Route a click that landed inside the `Completed` tab's content rect.
    /// Returns `true` when it changed something (the caller's redraw signal).
    pub(crate) fn completed_main_click(
        &mut self,
        pos: Point,
        content_rect: Rect,
        lh: f32,
    ) -> bool {
        if self.completed_grid.detail.is_some() {
            // The single "back" row at the top; anything else in the detail
            // is inert text.
            if pos.y < content_rect.y + lh {
                return self.completed_close_detail();
            }
            return false;
        }
        let controls_h = Self::completed_controls_height(lh).min(content_rect.height);
        if pos.y < content_rect.y + controls_h {
            return self.completed_control_click(pos);
        }
        match self.completed_table_hit(pos) {
            Some(DataTableHit::Header { col }) => self.completed_sort_by_column(col),
            Some(DataTableHit::Row { idx }) => self.completed_open_row(idx),
            _ => false,
        }
    }

    /// Route a click that landed on the control row against the last-painted
    /// `FormLayout`. Keyboard focus follows the mouse, same as the Settings
    /// panel.
    pub(crate) fn completed_control_click(&mut self, pos: Point) -> bool {
        let hit = {
            let cache = self.completed_form_layout.borrow();
            let Some((rect, layout)) = cache.as_ref() else {
                return false;
            };
            layout.hit_test(pos.x - rect.x, pos.y - rect.y)
        };
        let FormHit::Field(id) = hit else {
            return false;
        };
        // A `SegmentedControl` reports a synthetic per-option id, which is
        // what makes "click the option you want" work without the click path
        // knowing what the options mean.
        let (base, seg_idx) = split_segment_id(id.as_str());
        let Some(field) = Self::completed_field_index(base) else {
            return false;
        };
        self.completed_grid.field_sel = field;
        let Some(seg) = seg_idx else {
            // A text control (or the body of a choice control with no
            // per-option region): focusing it is the whole effect.
            return true;
        };
        // Look the value up in the same option list the form was built from —
        // never parse it back out of the label, which for the repo selector
        // reads `(all)` rather than the empty string it means.
        let options = match field {
            0 => self.completed_since_options(),
            2 => self.completed_repo_choices(),
            _ => return true,
        };
        let Some(choice) = options.get(seg).cloned() else {
            return true;
        };
        self.completed_set_field(field, choice);
        true
    }

    /// Return issues for a lifecycle state, grouped by repo, in stable repo
    /// order (same order as `pipeline_repo_names`).  Only intended for
    /// `"new"`, `"refining"`, `"pending"`, and `"done"` — Active issues are
    /// handled by `pipeline_active_issues` / `pipeline_active_by_repo` instead
    /// (same repo grouping, plus the #1487 in-flight-first ordering).
    ///
    /// Applies dedup, search filter, and dismissal filter.  Empty repos are
    /// omitted.
    pub(crate) fn pipeline_repos_for_state(&self, lc_key: &'static str) -> Vec<(String, Vec<usize>)> {
        self.pipeline_repos_for_state_impl(lc_key, true)
    }

    /// Shared implementation behind [`Self::pipeline_repos_for_state`] and
    /// [`Self::pipeline_jump_target`] — see
    /// [`Self::pipeline_active_issues_impl`]'s doc comment for why
    /// `apply_search` exists (#2449).
    fn pipeline_repos_for_state_impl(
        &self,
        lc_key: &'static str,
        apply_search: bool,
    ) -> Vec<(String, Vec<usize>)> {
        let mut dedup: std::collections::HashMap<(String, u64), usize> =
            std::collections::HashMap::new();
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if !self
                .pipeline_dismissed
                .contains(&(issue.repo_slug.clone(), issue.number))
                && (!apply_search || self.pipeline_search.matches(issue.number, &issue.title))
            {
                dedup.insert((issue.repo_slug.clone(), issue.number), i);
            }
        }
        // Build repo order from pipeline_issues (stable insertion order).
        let mut repos: Vec<String> = Vec::new();
        for issue in &self.pipeline_issues {
            let key = Self::pipeline_repo_key(issue).to_string();
            if !repos.contains(&key) {
                repos.push(key);
            }
        }
        let mut result: Vec<(String, Vec<usize>)> = Vec::new();
        for repo_key in repos {
            let mut idxs: Vec<usize> = dedup
                .values()
                .copied()
                .filter(|&i| {
                    let issue = &self.pipeline_issues[i];
                    Self::pipeline_repo_key(issue) == repo_key.as_str()
                        && self.pipeline_lifecycle_section(issue) == lc_key
                })
                .collect();
            idxs.sort_unstable();
            if !idxs.is_empty() {
                result.push((repo_key, idxs));
            }
        }
        result
    }

    /// Resolve the milestone `(number, title)` for a single pipeline issue by
    /// looking it up in `self.data.open_issues` (matched by `coord_repo` +
    /// `number`).  Returns `None` when the issue has no `coord_repo`, isn't
    /// found in `open_issues`, or has no milestone assigned — callers map
    /// that to the `"no-milestone"` bucket.  Shared by
    /// `pipeline_milestones_for_issues` (group rendering) and #869's
    /// jump-to-pipeline reveal logic (which needs the bucket for a single
    /// issue, not a whole group).
    pub(crate) fn pipeline_issue_milestone(&self, issue: &PipelineIssue) -> Option<(i64, String)> {
        let oi = issue.coord_repo.as_ref().and_then(|repo_name| {
            self.data
                .open_issues
                .iter()
                .find(|oi| oi.repo_name == *repo_name && oi.number == issue.number)
        })?;
        let num = oi.milestone_number?;
        Some((num, oi.milestone_title.clone().unwrap_or_default()))
    }

    /// #795 Phase 3b: look up the server-computed work-order node for
    /// `(repo_name, issue_number)` from `self.data.milestone_work_orders`.
    ///
    /// Returns `None` when the daemon predates #795 (empty list) or this issue
    /// isn't part of any tracked work order.  Callers augment Pipeline
    /// milestone card rows with the rank/next_up/blocked_on badges only when
    /// `Some(_)` is returned.
    pub(crate) fn work_order_node_for(
        &self,
        repo_name: &str,
        issue_number: u64,
    ) -> Option<&MilestoneWorkOrderNode> {
        self.data.milestone_work_orders.iter().find_map(|mwo| {
            if mwo.repo_name != repo_name {
                return None;
            }
            mwo.nodes.iter().find(|n| n.issue_number == issue_number)
        })
    }

    /// #1197: resolve the child-issue list for `issue` when it's an epic
    /// tracking issue with at least one `## Sub-issues` child (#1195's
    /// `data.epic_children` seam, published under the wire key `children`).
    /// Returns `None` for a non-epic or childless issue — callers render it
    /// as an ordinary flat leaf, unchanged from before #1197.
    pub(crate) fn epic_children_for(&self, issue: &PipelineIssue) -> Option<&[EpicChild]> {
        let repo_name = issue.coord_repo.as_deref()?;
        self.epic_children_for_repo_issue(repo_name, issue.number)
    }

    /// #1270: repo/number-keyed core of `epic_children_for`, factored out
    /// so the Board panel — whose `IssueGroup` rows carry a bare
    /// `(repo, issue_number)`, not a `PipelineIssue`/`coord_repo` — can
    /// resolve the same `data.epic_children` seam without depending on the
    /// Pipeline-only type. `epic_children_for` is now a thin wrapper over
    /// this.
    pub(crate) fn epic_children_for_repo_issue(
        &self,
        repo_name: &str,
        issue_number: u64,
    ) -> Option<&[EpicChild]> {
        self.data.epic_children.iter().find_map(|ec| {
            if ec.repo_name == repo_name
                && ec.tracking_issue == issue_number
                && !ec.children.is_empty()
            {
                Some(ec.children.as_slice())
            } else {
                None
            }
        })
    }

    /// #1197: compute the [`EpicNesting`] for one leaf-row bucket (a
    /// milestone's `mil_issue_idxs`, or a repo's `issue_idxs`). `after`/
    /// `group` are always empty for #1195
    /// API-sourced children (that shape carries no ordering annotation,
    /// unlike the `## Work order` convention `milestone_dag.rs` also
    /// parses) — so `build_dag_nodes` only ever resolves Done/InFlight/Ready
    /// for these, never Blocked. Reuses `build_dag_nodes` rather than
    /// re-deriving the state computation, per #1197's scope note; its
    /// "missing from `open_issues` ⇒ presumed Done" heuristic holds
    /// identically here since `open_issues` is the same cache regardless of
    /// whether child *membership* came from the `## Work order` block or
    /// the #1195 API-sourced `children` list.
    pub(crate) fn compute_epic_nesting(&self, issue_idxs: &[usize]) -> EpicNesting {
        let mut nesting = EpicNesting::default();
        for &idx in issue_idxs {
            let Some(issue) = self.pipeline_issues.get(idx) else {
                continue;
            };
            let Some(repo_name) = issue.coord_repo.as_deref() else {
                continue;
            };
            let rows = self.epic_child_rows_for(repo_name, issue.number);
            if rows.is_empty() {
                continue;
            }
            for row in &rows {
                nesting.nested.insert((repo_name.to_string(), row.number));
            }
            nesting.by_epic.insert(idx, rows);
        }
        nesting
    }

    /// #1270: DAG-computed child rows (Done/InFlight/Ready state, resolved
    /// titles) for the epic `issue_number` in `repo_name` — factored out of
    /// `compute_epic_nesting` so the Board panel (keyed directly by
    /// `(repo, issue_number)`, not a `pipeline_issues` index) builds on the
    /// exact same DAG-state computation instead of re-deriving it, per this
    /// issue's "Board and Pipeline don't drift" ask. Empty when `issue_number`
    /// isn't an epic or carries no `## Sub-issues` children.
    pub(crate) fn epic_child_rows_for(&self, repo_name: &str, issue_number: u64) -> Vec<EpicChildRow> {
        let Some(children) = self.epic_children_for_repo_issue(repo_name, issue_number) else {
            return Vec::new();
        };
        let work_order: Vec<WorkOrderNode> = children
            .iter()
            .map(|c| WorkOrderNode {
                issue_number: c.number,
                group: None,
                after: Vec::new(),
            })
            .collect();
        let nodes = build_dag_nodes(
            &work_order,
            repo_name,
            &self.data.open_issues,
            &self.data.assignments,
        );
        nodes
            .into_iter()
            .map(|n| EpicChildRow {
                number: n.issue_number,
                title: n.title,
                state: n.state,
            })
            .collect()
    }

    /// #1281 / #1487: shared "keep this child under its In-progress epic
    /// row" predicate — the child must not itself be [`NodeState::Done`].
    ///
    /// A Done child under an otherwise-in-progress epic is finished work and
    /// belongs to the Done section's flow, not under the epic's In-progress
    /// row.  The epic itself STAYS visible with its `M/M done` summary
    /// (`epic_progress_span`, keyed off the unfiltered nesting) — it's just
    /// the individual Done child rows that stop nesting there.
    ///
    /// Keyed off `EpicChildRow.state` — the [`NodeState`] the renderer
    /// already resolves via `build_dag_nodes` — rather than the coarse
    /// [`EpicChild.state`] parentage-checkbox string, so the merged-PR-but-
    /// GitHub-issue-not-yet-closed case (checkbox still "open",
    /// [`NodeState::Done`] via the assignment/state cache) agrees with the
    /// renderer's own display state.
    ///
    /// Callers go through [`Self::in_progress_child_rows`], which also
    /// applies the #1487 in-flight-first ordering — do not filter with this
    /// predicate alone, or the resulting subsequence won't match the one the
    /// renderer emitted.
    pub(crate) fn epic_child_kept_in_progress(row: &EpicChildRow) -> bool {
        !matches!(row.state, NodeState::Done)
    }

    /// #1487: sort rank for an epic child's [`NodeState`] — in-flight first,
    /// then ready, then blocked, then done (done never renders under
    /// In-progress, see [`Self::epic_child_kept_in_progress`]).
    fn node_state_rank(state: &NodeState) -> u8 {
        match state {
            NodeState::InFlight => 0,
            NodeState::Ready => 1,
            NodeState::Blocked(_) => 2,
            NodeState::Done => 3,
        }
    }

    /// #1281 / #1487: THE child-row subsequence the In-progress section
    /// renders beneath an epic — Done children dropped, the rest ordered
    /// in-flight → ready → blocked with ties keeping their original
    /// (work-order / issue-number) position.
    ///
    /// The renderer (via [`Self::filter_epic_nesting_for_in_progress`]) and
    /// both selection decoders — [`Self::resolve_nested_child_index`] and
    /// [`Self::selected_nested_child_id`] — all funnel through this one
    /// function so the three sites stay in exact agreement: `child_pos` in
    /// either decoder indexes into EXACTLY the sequence the renderer emitted
    /// rows for.  This is where an off-by-one would silently right-click the
    /// wrong issue.
    pub(crate) fn in_progress_child_rows(rows: &[EpicChildRow]) -> Vec<EpicChildRow> {
        let mut kept: Vec<(u8, usize, EpicChildRow)> = rows
            .iter()
            .filter(|row| Self::epic_child_kept_in_progress(row))
            .enumerate()
            .map(|(pos, row)| (Self::node_state_rank(&row.state), pos, (*row).clone()))
            .collect();
        // (rank, original position) — stable in-flight-first ordering.
        kept.sort_by_key(|(rank, pos, _)| (*rank, *pos));
        kept.into_iter().map(|(_, _, row)| row).collect()
    }

    /// #1281 / #1487: derive the In-progress projection of an
    /// already-computed [`EpicNesting`] — per epic, exactly the rows
    /// [`Self::in_progress_child_rows`] yields (Done children dropped,
    /// in-flight sorted above ready), dropping any epic whose resulting
    /// child list is empty from `by_epic`.
    ///
    /// The unfiltered `nesting` passed in still backs the "N/M done"
    /// progress badge (`epic_progress_span`), which intentionally reports
    /// against the WHOLE child set, and still backs `nested` (the flat-row
    /// suppression set) so a child dropped from the filtered list does not
    /// suddenly reappear as a duplicate flat sibling elsewhere.
    ///
    /// An all-children-done epic keeps its top-level row (the loop over
    /// `mil_issue_idxs` doesn't consult `by_epic` to decide whether to emit
    /// the epic — only to decide whether to expand children beneath it), so
    /// the row stays visible under In-progress with its `M/M done` summary
    /// until the epic issue itself closes.
    pub(crate) fn filter_epic_nesting_for_in_progress(nesting: &EpicNesting) -> EpicNesting {
        let mut filtered = EpicNesting {
            nested: nesting.nested.clone(),
            by_epic: std::collections::HashMap::new(),
        };
        for (&idx, rows) in &nesting.by_epic {
            let kept = Self::in_progress_child_rows(rows);
            if !kept.is_empty() {
                filtered.by_epic.insert(idx, kept);
            }
        }
        filtered
    }

    /// #1253: every child issue nested under *some* epic, across the WHOLE
    /// `pipeline_issues` list rather than one lifecycle-section bucket.
    ///
    /// `compute_epic_nesting`'s `nested` set only sees the epics present in
    /// whatever `issue_idxs` slice it was given — fine while an epic and its
    /// children always shared a bucket, but Bug 1's classifier fix (#1253)
    /// means they can now diverge: an epic moves to "in-progress" the
    /// instant any child has progress, while a not-yet-started sibling child
    /// stays in "new". Suppressing flat duplicates per-bucket then misses
    /// the case entirely — the child renders once nested under the epic in
    /// the epic's bucket, and once more as a flat top-level row in its own.
    /// Scanning every epic in `pipeline_issues` up front (independent of
    /// which section each one currently resolves to) and consulting this
    /// set wherever a row is about to render flat fixes that regardless of
    /// which bucket the epic and the child each land in.
    ///
    /// Keyed by `(repo, issue_number)` — `nested` alone (bare `u64`, as
    /// `EpicNesting` uses) risks a false-positive suppression if two repos
    /// happen to share a child issue number.
    ///
    /// Applies the same search + dismissal gate every sibling dedup function
    /// (`pipeline_repos_for_state`, `completed_rows`, …) already
    /// applies before an issue counts toward anything. Without it, an epic
    /// that is filtered out of every rendered bucket — because it doesn't
    /// match the search query, or because it was dismissed — would still
    /// contribute its children to this suppression set, hiding a child that
    /// *does* match the query (or that lost its only visible parent) with
    /// nothing left to render it nested. See review findings #1/#2 on #1253.
    pub(crate) fn pipeline_globally_nested_children(&self) -> std::collections::HashSet<(String, u64)> {
        let mut nested = std::collections::HashSet::new();
        for issue in &self.pipeline_issues {
            let Some(repo_name) = issue.coord_repo.as_ref() else {
                continue;
            };
            if self
                .pipeline_dismissed
                .contains(&(issue.repo_slug.clone(), issue.number))
            {
                continue;
            }
            if !self.pipeline_search.matches(issue.number, &issue.title) {
                continue;
            }
            let Some(children) = self.epic_children_for(issue) else {
                continue;
            };
            for child in children {
                nested.insert((repo_name.clone(), child.number));
            }
        }
        nested
    }

    /// #1253: true when `issue` is nested under some epic elsewhere in the
    /// pipeline (see `pipeline_globally_nested_children`) and should
    /// therefore be skipped when emitting a flat top-level row.
    fn is_globally_nested(
        issue: &PipelineIssue,
        globally_nested: &std::collections::HashSet<(String, u64)>,
    ) -> bool {
        issue
            .coord_repo
            .as_ref()
            .map(|r| globally_nested.contains(&(r.clone(), issue.number)))
            .unwrap_or(false)
    }

    /// #1197: resolve the `pipeline_epic_expanded` key + current expanded
    /// state for an epic row. Returns `None` when the issue has no
    /// `coord_repo` (an untracked row can't be keyed, and can't be an epic
    /// anyway — `epic_children_for` also requires the repo).
    ///
    /// Untouched keys default to **expanded**, UNLESS every child is already
    /// `Done` (#1253), in which case they default to **collapsed** — a
    /// finished epic's children are done work, not the reason to keep
    /// scanning the row, and #1197's always-expanded default meant a
    /// long-lived epic just accumulated an ever-growing wall of closed
    /// children (completed issues get an adjustable time range of their own
    /// in #2405's Completed grid; nested epic children have no equivalent). A
    /// manual toggle (a `pipeline_epic_expanded` entry) always wins over
    /// either default. This differs from the milestone header's #857
    /// collapsed-by-default (a milestone is a bucket; an epic is the work).
    pub(crate) fn epic_expand_state(
        &self,
        issue: &PipelineIssue,
        children: &[EpicChildRow],
    ) -> Option<((String, u64), bool)> {
        let key = (issue.coord_repo.clone()?, issue.number);
        let expanded = Self::epic_expand_state_in(&key, children, &self.pipeline_epic_expanded);
        Some((key, expanded))
    }

    /// #1270: map-agnostic core of `epic_expand_state` — resolves the
    /// collapse-when-all-done default against whichever
    /// `pipeline_epic_expanded`-shaped map the caller passes in, so the
    /// Board panel's own `board_epic_expanded` map gets the identical
    /// default without duplicating the "all children Done ⇒ start
    /// collapsed" rule.
    pub(crate) fn epic_expand_state_in(
        key: &(String, u64),
        children: &[EpicChildRow],
        expanded_map: &std::collections::HashMap<(String, u64), bool>,
    ) -> bool {
        let all_done = !children.is_empty()
            && children.iter().all(|c| matches!(c.state, NodeState::Done));
        let default_expanded = !all_done;
        expanded_map.get(key).copied().unwrap_or(default_expanded)
    }

    /// #1253: "N/M done" child-progress summary appended to an epic row's
    /// title spans. Rendered unconditionally (not just while collapsed) so
    /// the count is visible in every lifecycle bucket without requiring a
    /// click — a collapsed epic that hid its children with no summary was
    /// exactly the #1253 Bug 2 complaint ("hides everything once you fold it
    /// by hand"). Returns `None` for a childless resolution (shouldn't occur
    /// given callers only invoke this from `nesting.by_epic`, but a `0/0`
    /// badge would be noise if it ever did).
    pub(crate) fn epic_progress_span(children: &[EpicChildRow]) -> Option<StyledSpan> {
        if children.is_empty() {
            return None;
        }
        let done = children
            .iter()
            .filter(|c| matches!(c.state, NodeState::Done))
            .count();
        let total = children.len();
        let color = if done == total {
            Color::rgb(100, 180, 100) // matches the Done section's "✓ merged/closed" green
        } else {
            Color::rgb(140, 140, 160) // dim — mirrors the muted "new" state color
        };
        Some(StyledSpan::with_fg(format!("  {done}/{total}"), color))
    }

    /// #1197: resolve a nested-child row's own `pipeline_issues` index.
    /// `parent_idx` is the epic's index (already resolved from the
    /// selected path's leading segments); `child_pos` is the extra trailing
    /// path segment identifying which rendered child row was selected.
    /// Returns `None` when the child isn't independently tracked in
    /// `pipeline_issues` (e.g. it carries none of `pipeline_tracked_labels`)
    /// — such a row exists for nested display only, and callers correctly
    /// get "nothing selected" rather than silently falling back to acting
    /// on the parent epic instead.
    ///
    /// #1281 / #1487: `in_progress` must mirror whatever filter the row that
    /// was actually rendered used — `true` for a child nested beneath an
    /// epic's In-progress row (`child_pos` then indexes into the SAME
    /// filtered + in-flight-first-sorted subsequence
    /// [`Self::in_progress_child_rows`] produces), `false` everywhere else
    /// (New/Done/Refining/Pending nest the full, unfiltered child list in its
    /// original order, unchanged from #1197).
    ///
    /// The filtered path resolves the child list from [`Self::epic_child_rows_for`]
    /// (not [`Self::epic_children_for`]) so both branches walk the same
    /// [`NodeState`]-carrying rows the renderer emitted, and runs them
    /// through [`Self::in_progress_child_rows`] — the same function. Without
    /// this, a Done child suppressed by the renderer (#1281), or the #1487
    /// reordering, would shift the subsequence and `child_pos` would resolve
    /// to the wrong `pipeline_issues` index.
    pub(crate) fn resolve_nested_child_index(
        &self,
        parent_idx: usize,
        child_pos: usize,
        in_progress: bool,
    ) -> Option<usize> {
        let issue = self.pipeline_issues.get(parent_idx)?;
        let repo = issue.coord_repo.clone();
        let child_number = if in_progress {
            let repo_key = repo.as_deref()?;
            let rows = self.epic_child_rows_for(repo_key, issue.number);
            if rows.is_empty() {
                return None;
            }
            Self::in_progress_child_rows(&rows).get(child_pos)?.number
        } else {
            let children = self.epic_children_for(issue)?;
            children.get(child_pos)?.number
        };
        self.pipeline_issues
            .iter()
            .position(|pi| pi.number == child_number && pi.coord_repo == repo)
    }

    /// #1197: build one nested `TreeRow` for an epic child — `parent_path`
    /// is the epic's own row path (extended here with `child_idx`), `indent`
    /// is one greater than the epic row's own indent. Mirrors the plain
    /// issue-row spans (`#N  title`) plus a small state badge reusing
    /// `NodeState`, distinct from the milestone-work-order rank badges
    /// (`work_order_node_for`) — an epic's `## Sub-issues` children carry no
    /// rank/ordering, just Done/InFlight/Ready.
    pub(crate) fn epic_child_tree_row(
        &self,
        parent_path: &[u16],
        indent: u16,
        child_idx: usize,
        child: &EpicChildRow,
    ) -> TreeRow {
        let mut path: TreePath = parent_path.to_vec();
        path.push(child_idx as u16);
        let (state_text, state_color) = match child.state {
            NodeState::Done => ("done", self.active_theme.badge_passed),
            NodeState::InFlight => ("in-flight", self.active_theme.link_fg),
            NodeState::Ready => ("ready", self.active_theme.accent_fg),
            NodeState::Blocked(_) => ("blocked", self.active_theme.badge_warning),
        };
        // #1199 fix (duplicated-number label): a child that has aged out of
        // the `open_issues` cache (a closed/old sub-issue no longer synced —
        // e.g. #1031/#1032/#1033 under epic #1034) has no title anywhere:
        // the `## Sub-issues` checklist carries only `#N {group/after}`, no
        // human text, so `build_dag_nodes` falls back to `title = "#N"`.
        // Rendering that verbatim after the `#N` column produced the reported
        // "#1031 #1031". Detect the number-only fallback (or an empty title)
        // and show a muted "(details not cached)" placeholder instead of the
        // redundant number — the row still identifies itself by its `#N`
        // column and its state badge.
        let fallback_title = child.title.trim().is_empty()
            || child.title.trim() == format!("#{}", child.number);
        let (title_text, title_color) = if fallback_title {
            (
                "(details not cached)".to_string(),
                Color::rgb(120, 120, 120),
            )
        } else {
            (trunc(&child.title, 18).to_string(), Color::rgb(180, 180, 180))
        };
        TreeRow {
            path,
            indent,
            icon: None,
            text: StyledText {
                spans: vec![
                    StyledSpan::with_fg(
                        format!("#{:<5}", child.number),
                        Color::rgb(150, 150, 240),
                    ),
                    StyledSpan::with_fg(title_text, title_color),
                ],
            },
            badge: Some(Badge::colored(state_text, state_color)),
            is_expanded: None,
            decoration: Decoration::Normal,
            edit: None,
        }
    }

    /// #1487: the `in-flight` / `ready` status badge for a **top-level**
    /// In-progress row.  Before #1487 the only thing conveying this was the
    /// Live/Idle group node the row sat under; with the section grouped by
    /// repo instead, each row carries the signal itself — using the same
    /// badge vocabulary and colours `epic_child_tree_row` already renders on
    /// nested child rows.
    pub(crate) fn in_progress_status_badge(&self, in_flight: bool) -> Badge {
        if in_flight {
            Badge::colored("in-flight", self.active_theme.link_fg)
        } else {
            Badge::colored("ready", self.active_theme.accent_fg)
        }
    }

    /// #668: Group a slice of `pipeline_issues` indices by milestone.
    ///
    /// Looks up milestone data via `pipeline_issue_milestone`.  Issues
    /// without a resolvable milestone fall into the `"no-milestone"` bucket.
    ///
    /// Returns `Vec<(milestone_key, display_title, Vec<usize>)>` sorted:
    /// named milestones by number ASC, `"No milestone"` last.
    pub(crate) fn pipeline_milestones_for_issues(
        &self,
        issue_idxs: &[usize],
    ) -> Vec<(String, String, Vec<usize>)> {
        let mut milestone_map: std::collections::BTreeMap<
            (i64, String),
            (String, String, Vec<usize>),
        > = std::collections::BTreeMap::new();

        for &idx in issue_idxs {
            let issue = &self.pipeline_issues[idx];
            match self.pipeline_issue_milestone(issue) {
                Some((n, title)) => {
                    let key = n.to_string();
                    let sort_key = (n, title.clone());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| (key, title, Vec::new()))
                        .2
                        .push(idx);
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
                        .push(idx);
                }
            }
        }

        milestone_map
            .into_values()
            .filter(|(_, _, idxs)| !idxs.is_empty())
            .collect()
    }

    /// Returns a map from GitHub repo slug (`owner/name`) to the count of
    /// `state='pending'` rows in the in-memory merge-queue snapshot.
    ///
    /// Used by `rebuild_pipeline_sidebar` to badge repo sub-header rows with
    /// a queue-depth indicator (amber when the depth exceeds 5).  Pure read of
    /// `self.data.merge_queue` — no network I/O, safe to call on every render.
    pub(crate) fn pending_merge_queue_depth_by_slug(&self) -> std::collections::HashMap<String, usize> {
        let mut map: std::collections::HashMap<String, usize> =
            std::collections::HashMap::new();
        for e in &self.data.merge_queue {
            if e.state == "pending" {
                *map.entry(e.repo_github.clone()).or_insert(0) += 1;
            }
        }
        map
    }

    // ── #737: Merge Queue panel helpers ──────────────────────────────────────

    /// `true` when any merge-queue entry requires human attention:
    /// NEEDS_ATTENTION plan-status (from `merge_plan`), or — when only the
    /// legacy `merge_queue` is available — HUMAN_REQUIRED/failed state or
    /// a known CI failure.
    pub(crate) fn merge_queue_needs_attention(&self) -> bool {
        if !self.data.merge_plan.is_empty() {
            // #776 plan path: surface "NEEDS_ATTENTION" status from the server.
            return self
                .data
                .merge_plan
                .iter()
                .any(|p| p.status == "NEEDS_ATTENTION");
        }
        // Legacy fallback: derive attention from raw merge_queue state.
        self.data.merge_queue.iter().any(|e| {
            matches!(e.state.as_str(), "human_required" | "failed")
                || (e.state == "pending"
                    && e.pr_number.is_some()
                    && self.ci_failed_for_entry(e))
        })
    }

    /// Return a reference to the currently-selected merge-queue entry, if any.
    pub(crate) fn selected_merge_queue_entry(&self) -> Option<&MergeQueueEntry> {
        let n = self.data.merge_queue.len();
        if n == 0 {
            return None;
        }
        self.data.merge_queue.get(self.merge_queue_sel.min(n - 1))
    }

    /// Return a reference to the currently-selected planned merge entry, if any.
    ///
    /// Used when `data.merge_plan` is non-empty (v0.4.53+ daemon path).
    /// `merge_queue_sel` indexes into the rendered plan list order.
    pub(crate) fn selected_merge_plan_entry(&self) -> Option<&PlannedMergeEntry> {
        let n = self.data.merge_plan.len();
        if n == 0 {
            return None;
        }
        self.data.merge_plan.get(self.merge_queue_sel.min(n - 1))
    }

    /// Recompute a merge-queue entry's displayed error live instead of
    /// trusting the stored `entry.error` string verbatim (#420).
    ///
    /// `entry.error` is only refreshed by a real merge attempt (`coord
    /// merge` / `process()`) or `refresh_entry_assignment` — nothing clears
    /// it when a review approves or a smoke verdict lands through the
    /// normal interactive path (no intervening merge attempt). Left
    /// unchecked, a mergeable entry keeps showing e.g. "review required but
    /// not approved" indefinitely, inviting the operator to bounce
    /// already-approved work back for another round (the #410 case).
    ///
    /// Mirrors `coord/merge_queue.py::display_error`: only the gate
    /// messages known to go stale this way are recomputed here, using the
    /// same live gate checks the rest of the Pipeline view already uses
    /// (`issue_has_any_approved_review`, `test_stage_status_for`). Every
    /// other stored error (merge conflicts, CI results) reflects the
    /// outcome of the *last actual attempt* and is returned unchanged. This
    /// is the legacy (non-`merge_plan`) render path's counterpart — the
    /// `merge_plan` path already gets a live `reason` from the server's
    /// `coord.merge_queue.plan()`.
    pub(crate) fn merge_queue_entry_display_error(
        &self,
        entry: &MergeQueueEntry,
    ) -> Option<String> {
        let err = entry.error.as_deref().filter(|e| !e.is_empty())?;
        // #1640: a "recorded but stale against the current base" refusal is
        // NOT recomputable here. This view's only smoke evidence is
        // `test_stage_status_for`, which reads the terminal verdict and
        // knows nothing about the #1479 freshness anchors — so recomputing
        // would clear the message on exactly the entry `coord merge`
        // refuses, reinstating the false green from the other direction.
        // Render the server's wording (which names the SHAs) verbatim.
        if err.starts_with("smoke test verdict is stale:") {
            return Some(err.to_string());
        }
        let is_stale_review = matches!(
            err,
            "review required but not approved"
                | "review required but board unavailable to confirm approval"
        );
        let is_stale_smoke = matches!(
            err,
            "smoke test required but no verdict recorded"
                | "smoke test required but board unavailable to confirm verdict"
        );
        if !is_stale_review && !is_stale_smoke {
            return Some(err.to_string());
        }
        // Can't recompute without knowing which issue this entry is for —
        // fall back to the stored string.
        let Some(issue) = self.pipeline_issues.iter().find(|i| {
            entry.issue_number == Some(i.number) && i.repo_slug == entry.repo_github
        }) else {
            return Some(err.to_string());
        };
        if is_stale_review {
            if self.issue_has_any_approved_review(issue, Some(&entry.assignment_id)) {
                return None;
            }
            return Some(err.to_string());
        }
        // is_stale_smoke
        if self.test_stage_status_for(issue) == StageStatus::Done {
            return None;
        }
        Some(err.to_string())
    }

    /// Format a single merge-queue entry as a terse list label.
    ///
    /// Template: `[<STATE>] #<PR>  <issue_title>  (<error-reason>)`
    pub(crate) fn merge_queue_entry_label(&self, entry: &MergeQueueEntry) -> String {
        // State badge.
        let state = match entry.state.as_str() {
            "pending" => "PENDING",
            "active" | "running" => "ACTIVE",
            "merged" => "MERGED",
            "human_required" => "NEEDS ATTENTION",
            "failed" => "FAILED",
            _ => &entry.state,
        };
        // PR number.
        let pr = entry
            .pr_number
            .map(|n| format!(" #{}", n))
            .unwrap_or_default();
        // Issue title — join from open_issues on (repo_name, issue_number).
        let title = entry
            .issue_number
            .and_then(|num| {
                // Reverse-lookup: repo_github → coord repo name, then match open_issues.
                let coord_repo = self
                    .data
                    .pipeline_repos
                    .iter()
                    .find(|(_, gh)| gh == &entry.repo_github)
                    .map(|(name, _)| name.as_str());
                self.data.open_issues.iter().find(|oi| {
                    oi.number == num
                        && coord_repo.is_some_and(|cr| oi.repo_name == cr)
                })
            })
            .map(|oi| format!("  {}", oi.title))
            .unwrap_or_default();
        // Gate / conflict reason — the last error string from coord merge,
        // recomputed live (#420) so an approval/verdict that landed outside
        // a merge attempt doesn't leave a stale "not approved" message.
        let reason = self
            .merge_queue_entry_display_error(entry)
            .as_deref()
            .filter(|e| !e.is_empty())
            .map(|e| {
                // Truncate long error strings to keep the row terse.
                let s = e.trim();
                if s.len() > 60 {
                    format!("  ({}…)", &s[..57])
                } else {
                    format!("  ({})", s)
                }
            })
            .unwrap_or_default();
        format!("[{}]{}{}{}", state, pr, title, reason)
    }

    /// State colour for a merge-queue entry label.
    pub(crate) fn merge_queue_entry_color(&self, entry: &MergeQueueEntry) -> Color {
        match entry.state.as_str() {
            "human_required" | "failed" => Color::rgb(220, 70, 70),
            "merged" => Color::rgb(80, 200, 80),
            "active" | "running" => Color::rgb(80, 160, 220),
            _ => {
                // Pending: amber when CI is failing.
                if self.ci_failed_for_entry(entry) {
                    Color::rgb(220, 140, 40)
                } else {
                    Color::rgb(180, 180, 180)
                }
            }
        }
    }

    /// Sidebar placeholder for the Merge Queue view — shows entry count and
    /// an attention indicator when any entry needs human action.
    pub(crate) fn merge_queue_sidebar(&self) -> ListView {
        // Use merge_plan count when available (#776); fall back to merge_queue.
        let n = if !self.data.merge_plan.is_empty() {
            self.data.merge_plan.len()
        } else {
            self.data.merge_queue.len()
        };
        let attn = if self.merge_queue_needs_attention() {
            " ⚠ needs attention"
        } else {
            ""
        };
        let hint = format!("  {} entr{}{}",
            n,
            if n == 1 { "y" } else { "ies" },
            attn,
        );
        ListView {
            id: WidgetId::new("mergequeue-sidebar"),
            title: Some(StyledText::plain(" MERGE QUEUE ")),
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

    /// Render the Merge Queue main panel.
    ///
    /// **#777 — plan path (v0.4.53+ daemon):** when `data.merge_plan` is
    /// non-empty, renders the server-computed ranked plan grouped by
    /// `repo → target_branch`.  Each entry shows its rank number, plan
    /// status badge, PR number, diff size, issue title, live gate reason
    /// (for BLOCKED), and age.  The order matches what `coord merge` would do.
    ///
    /// **Legacy fallback:** when `merge_plan` is empty (older daemon or local
    /// SQLite path), falls back to displaying `data.merge_queue` entries
    /// grouped by milestone as in #737.
    pub(crate) fn render_merge_queue_panel(&self, backend: &mut dyn Backend, rect: Rect, _lh: f32) {
        // ── #777: plan path ───────────────────────────────────────────────────
        if !self.data.merge_plan.is_empty() {
            self.render_merge_plan_panel(backend, rect);
            return;
        }

        // ── Legacy path (no merge_plan from daemon) ───────────────────────────
        let n = self.data.merge_queue.len();
        let staging = &self.data.merge_staging;

        // Empty-state: no staging AND no queue entries.
        if n == 0 && staging.is_empty() {
            backend.draw_list(rect, &plain_list(
                "mergequeue-empty",
                "  No merge-queue entries — run `coord merge` to populate the queue.",
                0,
            ));
            return;
        }

        // Clamp the selection in case the queue shrank after a drop.
        let sel_entry = if n > 0 { self.merge_queue_sel.min(n - 1) } else { 0 };

        // ── Build item list ───────────────────────────────────────────────
        //
        // Layout (top → bottom):
        //   [staging header]            — only when staging is non-empty
        //   [staging rows]
        //   [queue header]              — only when BOTH sections are non-empty
        //   [milestone sub-headers]
        //   [queue rows]
        //
        // The staging section answers "did my approved PR make it into the
        // queue?" and is always shown above the active queue so it's the
        // first thing seen when opening the panel.
        //
        // `selected_idx` tracks where the selected queue entry lands in the
        // combined list, accounting for all inserted header rows above it.

        let mut items: Vec<ListItem> = Vec::with_capacity(staging.len() + n + 10);
        let mut selected_idx = 0usize;

        // ── #778: Staging section ──────────────────────────────────────────
        if !staging.is_empty() {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        " About to enter queue ".to_string(),
                        Color::rgb(180, 210, 150),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });

            for entry in staging {
                let issue_part = format!("#{:<5} {}", entry.issue_number, trunc(&entry.issue_title, 22));
                let (arrow_text, arrow_color) = if entry.status == "blocked" {
                    let reason = entry.reason.as_deref().unwrap_or("gate failing");
                    (format!("→  BLOCKED: {}", reason), Color::rgb(220, 90, 80))
                } else {
                    ("→  enqueuing on next tick".to_string(), Color::rgb(130, 200, 120))
                };
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![
                            StyledSpan::with_fg(issue_part, Color::rgb(150, 150, 240)),
                            StyledSpan::with_fg("  ".to_string(), Color::rgb(120, 120, 120)),
                            StyledSpan::with_fg(arrow_text, arrow_color),
                        ],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
            }
        }

        // ── Merge queue section ────────────────────────────────────────────
        if n > 0 {
            // When both sections are visible, add a separator so the reader
            // can immediately distinguish staged-but-pending from in-queue.
            if !staging.is_empty() {
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " In merge queue ".to_string(),
                            Color::rgb(150, 190, 230),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });
            }

            // Milestone-grouped queue entries (existing behaviour).
            let mut last_ms: Option<Option<String>> = None; // sentinel: never matches first
            for (i, entry) in self.data.merge_queue.iter().enumerate() {
                let ms = entry.milestone_title.clone();
                if last_ms.as_ref() != Some(&ms) {
                    let header_label = ms.as_deref().unwrap_or("(No milestone)");
                    items.push(ListItem {
                        text: StyledText {
                            spans: vec![StyledSpan::with_fg(
                                format!(" {} ", header_label),
                                Color::rgb(150, 190, 230),
                            )],
                        },
                        icon: None,
                        detail: None,
                        decoration: Decoration::Header,
                    });
                    last_ms = Some(ms);
                }
                if i == sel_entry {
                    selected_idx = items.len();
                }
                let label = self.merge_queue_entry_label(entry);
                let color = self.merge_queue_entry_color(entry);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(&label, color)],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
            }
        }

        let total = items.len();
        backend.draw_list(rect, &ListView {
            id: WidgetId::new("mergequeue-list"),
            title: Some(StyledText::plain(" MERGE QUEUE ")),
            items,
            selected_idx,
            scroll_offset: self.merge_queue_scroll,
            has_focus: true,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: total > 10,
        });
    }

    // ── #777: Ranked plan panel ───────────────────────────────────────────────

    /// Render the Merge Queue panel from `data.merge_plan` (#776 plan path).
    ///
    /// Layout (per the #777 spec):
    /// ```text
    /// MERGE QUEUE — order = next coord merge run
    ///
    /// repo-name → target-branch
    ///   1. [READY]   #812  +14   Fix toast clipping        queued 4m ago
    ///   2. [BLOCKED] #770  +120  CI running (2 checks)      last try 2m ago
    /// other-repo → develop
    ///   3. [READY]   #383  +41   ButtonBar primitive        queued 1h ago
    /// ```
    ///
    /// Entries are grouped by `(repo_github, target_branch)` via
    /// `Decoration::Header` separators.  Group headers show
    /// `<repo_name> → <target_branch>` using the coord-local repo name from
    /// `pipeline_repos` (falling back to the GitHub slug when not mapped).
    /// Each entry row includes rank, status badge, PR number (from
    /// `merge_queue` by `assignment_id`), size, truncated title, and age.
    pub(crate) fn render_merge_plan_panel(&self, backend: &mut dyn Backend, rect: Rect) {
        // Called only when merge_plan is non-empty (see render_merge_queue_panel).
        let plan = &self.data.merge_plan;
        let n = plan.len();

        // Build a lookup: assignment_id → pr_number (from the raw merge_queue).
        let pr_lookup: std::collections::HashMap<&str, i64> = self
            .data
            .merge_queue
            .iter()
            .filter_map(|mq| mq.pr_number.map(|pr| (mq.assignment_id.as_str(), pr)))
            .collect();

        // Current time for age formatting.  Using a 0 fallback (shows "" for
        // missing timestamps) is safe here — the field is cosmetic only.
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);

        let sel_entry = self.merge_queue_sel.min(n - 1);
        let mut items: Vec<ListItem> = Vec::with_capacity(n + 8);
        let mut selected_idx = 0usize;
        // Consecutive-run grouping by (repo_github, target_branch).
        let mut last_group: Option<(&str, &str)> = None;

        for (i, entry) in plan.iter().enumerate() {
            // ── Group header ─────────────────────────────────────────────
            let group_key = (entry.repo_github.as_str(), entry.target_branch.as_str());
            if last_group != Some(group_key) {
                // `repo_name` from the plan is the coord-local name; prefer it
                // over the GitHub slug for a friendlier group header.
                let display_repo = if !entry.repo_name.is_empty() {
                    &entry.repo_name
                } else {
                    &entry.repo_github
                };
                let header_label = format!(" {} → {} ", display_repo, entry.target_branch);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(header_label, Color::rgb(150, 190, 230))],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });
                last_group = Some(group_key);
            }

            // ── Record selection display index ────────────────────────────
            if i == sel_entry {
                selected_idx = items.len();
            }

            // ── Format the entry label ────────────────────────────────────
            // Status badge: map plan status to display string.
            let status_badge = match entry.status.as_str() {
                "READY"           => "READY",
                "BLOCKED"         => "BLOCKED",
                "MERGING"         => "MERGING",
                "MERGED"          => "MERGED",
                "NEEDS_ATTENTION" => "NEEDS ATTENTION",
                other             => other,
            };

            // PR number (cross-ref with raw merge_queue by assignment_id).
            let pr_str = pr_lookup
                .get(entry.assignment_id.as_str())
                .map(|&pr| format!(" #{}", pr))
                .unwrap_or_default();

            // Diff size.
            let size_str = entry.size
                .map(|s| format!(" +{}", s))
                .unwrap_or_default();

            // Title — truncate to keep the row terse.
            let title_short = trunc(&entry.issue_title, 40);

            // Age: show `enqueued_at` for READY/BLOCKED, `last_attempt` for
            // MERGING/terminal states that have a recent attempt.
            let age_str = if matches!(entry.status.as_str(), "MERGED" | "NEEDS_ATTENTION") {
                // Terminal / attention: show last_attempt if any.
                let age = format_age(entry.last_attempt, now);
                if age.is_empty() { String::new() } else { format!("  last try {}", age) }
            } else {
                // Active/blocked: prefer last_attempt for MERGING, enqueued_at otherwise.
                let ts = if entry.status == "MERGING" {
                    entry.last_attempt.or(entry.enqueued_at)
                } else {
                    entry.enqueued_at
                };
                // Only use "last try" when the entry is actively MERGING;
                // for READY/BLOCKED entries the timestamp is always enqueued_at
                // regardless of whether last_attempt is set, so "queued" is correct.
                let label = if entry.status == "MERGING" { "last try" } else { "queued" };
                let age = format_age(ts, now);
                if age.is_empty() { String::new() } else { format!("  {} {}", label, age) }
            };

            // Reason for BLOCKED entries — the live gate explanation from the plan.
            let reason_str = if entry.status == "BLOCKED" {
                entry.reason
                    .as_deref()
                    .filter(|r| !r.is_empty())
                    .map(|r| {
                        let s = r.trim();
                        if s.len() > 50 {
                            format!("  ({}…)", &s[..47])
                        } else {
                            format!("  ({})", s)
                        }
                    })
                    .unwrap_or_default()
            } else {
                String::new()
            };

            let label = format!(
                "  {}. [{}]{}{} {}{}{}",
                entry.rank, status_badge, pr_str, size_str, title_short, reason_str, age_str,
            );

            // Status colour.
            let color = match entry.status.as_str() {
                "NEEDS_ATTENTION" => Color::rgb(220, 70, 70),
                "MERGED"          => Color::rgb(80, 200, 80),
                "MERGING"         => Color::rgb(80, 160, 220),
                "BLOCKED"         => Color::rgb(220, 140, 40),
                _                 => Color::rgb(180, 180, 180), // READY
            };

            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(label, color)],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });
        }

        let total = items.len();
        backend.draw_list(rect, &ListView {
            id: WidgetId::new("mergequeue-list"),
            title: Some(StyledText::plain(" MERGE QUEUE — order = next coord merge run ")),
            items,
            selected_idx,
            scroll_offset: self.merge_queue_scroll,
            has_focus: true,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: total > 10,
        });
    }

    // ── #737 / #780: Merge Queue per-entry actions ───────────────────────────

    /// `coord merge --only <assignment_id>` (single-entry merge, #780).
    ///
    /// Merges exactly the selected entry and leaves all other queue entries
    /// untouched.  When `force` is true, appends `--force-merge` to bypass CI
    /// and gate checks.
    ///
    /// For BLOCKED entries: shows a warning toast with the block reason and
    /// returns without dispatching (the operator can use `M` / force to
    /// override via `--force-merge`).
    pub(crate) fn dispatch_merge_queue_merge_only(&mut self, force: bool) {
        // Prefer merge_plan (v0.4.53+ daemon); fall back to raw merge_queue.
        let (aid, status, reason) = if !self.data.merge_plan.is_empty() {
            match self.selected_merge_plan_entry() {
                Some(e) => (
                    e.assignment_id.clone(),
                    e.status.clone(),
                    e.reason.clone(),
                ),
                None => {
                    self.push_toast("Merge Queue", "No entry selected.", ToastSeverity::Warning);
                    return;
                }
            }
        } else {
            match self.selected_merge_queue_entry() {
                Some(e) => (e.assignment_id.clone(), String::new(), None),
                None => {
                    self.push_toast("Merge Queue", "No entry selected.", ToastSeverity::Warning);
                    return;
                }
            }
        };

        // For BLOCKED entries (without force) surface the reason rather than
        // silently dispatching a merge that will fail the gate.
        if !force && status == "BLOCKED" {
            let body = reason
                .as_deref()
                .filter(|r| !r.is_empty())
                .unwrap_or("gate check failed");
            self.push_toast(
                "Blocked — cannot merge",
                &format!("{}  (use M to force)", body),
                ToastSeverity::Warning,
            );
            return;
        }

        // Build the command as owned Strings so we can conditionally push
        // --force-merge before slicing into &str refs for spawn_queued.
        let mut cmd_strs: Vec<String> = vec![
            "merge".to_string(),
            "--only".to_string(),
            aid.clone(),
        ];
        if force {
            cmd_strs.push("--force-merge".to_string());
        }
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    if force { "Force-merge (only) dispatched" } else { "Merge only this dispatched" },
                    &format!("coord merge --only {}{}", aid,
                        if force { " --force-merge" } else { "" }),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast("⏳ Queued", "merge runs after current command", ToastSeverity::Info);
            }
            SpawnQueuedOutcome::Deduped => {}
        }
    }

    /// `coord merge` — drain every READY entry in the queue after one-key
    /// confirmation.  Sets `pending_merge_all_ready` with the list of READY
    /// aids so the confirm dialog can display the count and a preview.
    pub(crate) fn dispatch_merge_queue_merge_all(&mut self) {
        // Collect all READY aids from merge_plan (preferred) or legacy queue.
        let ready_aids: Vec<String> = if !self.data.merge_plan.is_empty() {
            self.data.merge_plan.iter()
                .filter(|e| e.status == "READY")
                .map(|e| e.assignment_id.clone())
                .collect()
        } else {
            // Legacy merge_queue has no status field; treat all PENDING as ready.
            self.data.merge_queue.iter()
                .map(|e| e.assignment_id.clone())
                .collect()
        };

        if ready_aids.is_empty() {
            self.push_toast(
                "No READY entries",
                "Nothing to merge — all entries are BLOCKED or the queue is empty.",
                ToastSeverity::Info,
            );
            return;
        }

        // Show confirm dialog; key handler fires `coord merge` on 'y'.
        self.pending_merge_all_ready = Some(ready_aids);
    }

    /// `coord merge --drop <assignment_id>` — remove one entry from the queue.
    pub(crate) fn dispatch_merge_queue_drop(&mut self) {
        // Prefer merge_plan (v0.4.53+ daemon); fall back to raw merge_queue.
        let aid = if !self.data.merge_plan.is_empty() {
            match self.selected_merge_plan_entry() {
                Some(e) => e.assignment_id.clone(),
                None => {
                    self.push_toast("Merge Queue", "No entry selected.", ToastSeverity::Warning);
                    return;
                }
            }
        } else {
            match self.selected_merge_queue_entry() {
                Some(e) => e.assignment_id.clone(),
                None => {
                    self.push_toast("Merge Queue", "No entry selected.", ToastSeverity::Warning);
                    return;
                }
            }
        };
        let cmd_strs: Vec<String> = vec![
            "merge".to_string(),
            "--drop".to_string(),
            aid.clone(),
        ];
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Drop queued",
                    &format!("coord merge --drop {}", aid),
                    ToastSeverity::Info,
                );
                // Advance selection to stay in-bounds after the row disappears.
                self.merge_queue_sel = self.merge_queue_sel.saturating_sub(1);
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast("⏳ Queued", "drop runs after current command", ToastSeverity::Info);
            }
            SpawnQueuedOutcome::Deduped => {}
        }
    }

    // ── #684: Start (automated) > Merge — headless queue drain ───────────────

    /// True when the selected pipeline issue has an active `type="merge"`
    /// assignment (i.e. an interactive `coord assign --merge-of` session is
    /// currently running).  Used to guard the automated merge action so the
    /// headless queue and the interactive agent never race on the same branch.
    pub(crate) fn has_active_interactive_merge_for_issue(&self, issue_num: u64) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue_num
                && a.assignment_type.as_deref() == Some("merge")
                && a.status == "running"
        })
    }

    /// True when the merge queue already has a `state="merging"` entry for
    /// `issue_num` — the headless queue is actively processing that branch.
    /// Used to guard `start-merge-interactive` so an operator doesn't launch
    /// an interactive `--merge-of` agent over a concurrent headless merge.
    pub(crate) fn has_active_headless_merge_for_issue(&self, issue_num: u64) -> bool {
        // Prefer merge_plan (v0.4.53+ daemon); fall back to raw merge_queue.
        if !self.data.merge_plan.is_empty() {
            self.data.merge_plan.iter().any(|e| {
                e.issue_number == issue_num && e.status == "MERGING"
            })
        } else {
            self.data.merge_queue.iter().any(|e| {
                e.issue_number == Some(issue_num) && e.state == "merging"
            })
        }
    }

    /// `coord merge --order <assignment_id>` sourced from the Pipeline panel's
    /// selected issue (the "Start (automated) > Merge" action, #684).
    ///
    /// Unlike `dispatch_merge_queue_merge` (which fires from the MergeQueue
    /// panel and acts on the *currently selected queue entry*), this function
    /// resolves the completed work assignment from the Pipeline selection and
    /// feeds it to the same `coord merge --order` path.  `coord merge` handles
    /// the idempotent enqueue step server-side before merging.
    pub(crate) fn dispatch_merge_automated_for_selected_pipeline_issue(&mut self) -> bool {
        let Some(aid) = self.selected_completed_work_aid() else {
            self.push_toast(
                "Start merge (automated)",
                "No completed work assignment with a branch — cannot enqueue.",
                ToastSeverity::Warning,
            );
            return false;
        };
        // Guard: refuse if an interactive --merge-of session is already running
        // for this issue — running both would race on the same branch.
        let issue_num = self
            .selected_issue_repo_and_key()
            .map(|(_, k)| k.1)
            .unwrap_or(0);
        if self.has_active_interactive_merge_for_issue(issue_num) {
            self.push_toast(
                "Start merge (automated)",
                "An interactive merge session is already running for this issue — \
                 stop it first to avoid a branch race.",
                ToastSeverity::Warning,
            );
            return false;
        }
        let cmd_strs: Vec<String> = vec![
            "merge".to_string(),
            "--order".to_string(),
            aid.clone(),
        ];
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Merge queued (automated)",
                    &format!("coord merge --order {}", &aid[..aid.len().min(8)]),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "merge runs after current command",
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Deduped => false,
        }
    }

    /// #2402: confirm handler for the Pipeline row's "Re-verify
    /// (revalidate)" dialog — the `y` keypress / "yes" button fires this
    /// with the [`PendingMergeRevalidate`] the confirm was armed against
    /// (`dispatch_context_menu_action`'s `"merge-revalidate"` arm). Runs
    /// `coord merge --revalidate --only <assignment_id>`: `--only` is the
    /// narrowest single-entry scope (`--order` still processes the whole
    /// repo-wide queue, merely moving this entry first — see
    /// `coord/commands/merge.py`'s `--only`/`--order` help text), and
    /// `--revalidate` re-tests the stale-but-passed verdict / re-runs stale
    /// CI before `process()` re-evaluates the gate. Takes `pending` by value
    /// so the caller's already-cleared `self.pending_merge_revalidate` can't
    /// accidentally stay armed.
    pub(crate) fn confirm_merge_revalidate(&mut self, pending: PendingMergeRevalidate) {
        use crate::commands::SpawnQueuedOutcome;
        let short: String = pending.assignment_id.chars().take(8).collect();
        match self.command_runner.spawn_queued(&[
            "merge",
            "--revalidate",
            "--only",
            &pending.assignment_id,
        ]) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Revalidate dispatched",
                    &format!(
                        "coord merge --revalidate --only {} — re-testing #{} against \
                         the current base",
                        short, pending.issue_num,
                    ),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "revalidate runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
    }

    /// #1151: resolve the `--for-path` argument to append (if any) to a
    /// `coord acceptance mock/author/record` dispatch for `repo`, based on
    /// `self.data.pipeline_acceptance_routes` (repo → route `match` globs,
    /// populated by `_save_config_snapshot` only for repos whose acceptance
    /// driver is *routed*, #1125 — see `AcceptanceConfig.driver_for` in
    /// `coord/config.py`). Before this, all three dispatch call sites fired
    /// the CLI with no `--for-path` unconditionally, which those commands
    /// reject once a repo's driver is routed
    /// (`coord/commands/acceptance.py`'s "no route matched" error).
    ///
    /// Three outcomes:
    /// - repo absent from the map (unrouted repo, or the daemon predates
    ///   #1151) → `Ok(None)`, dispatch exactly as before.
    /// - exactly one candidate route → `Ok(Some(glob))`, unambiguous: the
    ///   caller appends `--for-path <glob>` and the dispatch succeeds
    ///   transparently instead of hitting the CLI's routing gate at all.
    /// - more than one candidate route → first try to disambiguate (#1201)
    ///   by parsing `issue_body`'s `## Files` / `**Allowed:**` glob(s) (see
    ///   `parse_allowed_globs_from_issue_body`) and checking each against
    ///   every candidate route via `globs_overlap`. Exactly one route
    ///   overlaps → `Ok(Some(glob))`, resolved transparently just like the
    ///   single-candidate case. Zero or more than one overlap (no `## Files`
    ///   section, an allowed glob that matches no route, or one that's
    ///   ambiguous across routes) → `Err(reason)`, the original #1151
    ///   fallback: the caller must show `reason` as a toast and skip
    ///   dispatch rather than fire a command that the CLI will reject
    ///   anyway.
    pub(crate) fn acceptance_for_path_arg(
        &self,
        repo: &str,
        issue_body: &str,
    ) -> Result<Option<String>, String> {
        match self.data.pipeline_acceptance_routes.get(repo) {
            None => Ok(None),
            Some(routes) if routes.is_empty() => Ok(None),
            Some(routes) if routes.len() == 1 => Ok(Some(routes[0].clone())),
            Some(routes) => {
                let allowed = parse_allowed_globs_from_issue_body(issue_body);
                let matching: Vec<&String> = routes
                    .iter()
                    .filter(|route| allowed.iter().any(|glob| globs_overlap(glob, route)))
                    .collect();
                if matching.len() == 1 {
                    return Ok(Some(matching[0].clone()));
                }
                Err(format!(
                    "Repo '{repo}' has a routed acceptance driver with {} candidate \
                     subtrees ({}) — the TUI can't tell which one this issue's work \
                     belongs to yet. Dispatch from the CLI instead, e.g. `coord \
                     acceptance ... --for-path {}`.",
                    routes.len(),
                    routes.join(", "),
                    routes.first().map(String::as_str).unwrap_or("<glob>"),
                ))
            }
        }
    }

    /// #1059 (docs/ORACLE_LOOP.md, #930): dispatch Gate A for the selected
    /// epic/tracking-issue Pipeline row — `coord acceptance mock <repo>
    /// <tracking_issue>`. Headless, like `coord assign`: the mock-author is
    /// a normal (non-interactive) `claude -p` worker
    /// (`coord/mock_author.py`), not a live tmux session — this just fires
    /// the CLI from the row's context menu instead of a terminal. The CLI's
    /// own claim-detection refuses a duplicate dispatch (Gate A already in
    /// flight) with a clear stderr message; the command runner surfaces
    /// that as a toast via its usual failure path.
    ///
    /// #1151: resolves `--for-path` via `acceptance_for_path_arg` first —
    /// see that method's doc for the three outcomes.
    pub(crate) fn dispatch_gate_a_mock_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(repo) = issue.coord_repo.clone() else {
            self.push_toast(
                "Dispatch Gate A mock",
                "No local repo mapping for this issue — cannot dispatch.",
                ToastSeverity::Warning,
            );
            return false;
        };
        let for_path = match self.acceptance_for_path_arg(&repo, &issue.body) {
            Ok(p) => p,
            Err(reason) => {
                self.push_toast("Dispatch Gate A mock", &reason, ToastSeverity::Warning);
                return false;
            }
        };
        let issue_str = issue.number.to_string();
        let mut cmd_strs: Vec<String> = vec![
            "acceptance".to_string(),
            "mock".to_string(),
            repo.clone(),
            issue_str,
        ];
        if let Some(p) = for_path {
            cmd_strs.push("--for-path".to_string());
            cmd_strs.push(p);
        }
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Gate A mock dispatched",
                    &format!("coord {}", cmd_strs.join(" ")),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "Gate A mock dispatch runs after current command",
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Deduped => false,
        }
    }

    /// #2513 (PDR-5): dispatch `coord portal publish-mocks <repo>
    /// <tracking_issue>` for the selected epic/tracking-issue Pipeline
    /// row — the on-demand counterpart to PDR-3's merge-triggered
    /// auto-push: publish whatever mock bundle is on THIS machine's local
    /// checkout right now, no merge required (iterating on a local
    /// `--amend`, or re-publishing after a manual edit). Same
    /// headless-subprocess-spawn shape as
    /// `dispatch_gate_a_mock_for_selected_pipeline_row` right above — a
    /// normal (non-interactive) `claude`-free CLI invocation, not a live
    /// tmux session. The CLI's own validation (portal link exists, local
    /// mock bundle exists) surfaces failures via the command runner's
    /// usual toast path.
    pub(crate) fn dispatch_publish_mocks_to_portal_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(repo) = issue.coord_repo.clone() else {
            self.push_toast(
                "Publish mocks to portal",
                "No local repo mapping for this issue — cannot dispatch.",
                ToastSeverity::Warning,
            );
            return false;
        };
        let cmd_strs: Vec<String> = vec![
            "portal".to_string(),
            "publish-mocks".to_string(),
            repo,
            issue.number.to_string(),
        ];
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Publishing mocks to portal",
                    &format!("coord {}", cmd_strs.join(" ")),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "Publish mocks to portal runs after current command",
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Deduped => false,
        }
    }

    /// #2063: record the human Gate-A verdict for the selected epic row —
    /// `coord gate-a --approved|--changes <repo> <tracking_issue>`.
    ///
    /// Sits beside "View Gate A mock (PR)" 👁 deliberately: reviewing and
    /// recording should be the same gesture. Before this, sign-off was a
    /// convention ("merging the PR is sign-off") that anything able to merge
    /// a PR satisfied silently, and nothing downstream ever checked — the
    /// mocks for two consecutive coord-portal milestones merged unseen.
    ///
    /// `approved == false` records `--changes`. #2500: the note the toast
    /// used to point operators at typing separately (`coord acceptance mock
    /// <repo> <issue> --amend "..."`) is now collected up front via
    /// `pending_gate_a_changes_note` (`open_gate_a_changes_note_input` /
    /// `submit_gate_a_changes_note_input`, same single-field
    /// `DialogInput::TextInput` shape as "Add to drive queue after…") and
    /// passed straight through as `--note`. This function itself stays the
    /// low-level dispatcher — `approved == true` calls it directly with an
    /// empty note (approval has no note to carry).
    pub(crate) fn dispatch_gate_a_verdict_for_selected_pipeline_row(
        &mut self,
        approved: bool,
        note: &str,
    ) -> bool {
        let title = if approved {
            "Approve Gate A"
        } else {
            "Request Gate A changes"
        };
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(repo) = issue.coord_repo.clone() else {
            self.push_toast(
                title,
                "No local repo mapping for this issue — cannot record a verdict.",
                ToastSeverity::Warning,
            );
            return false;
        };
        self.dispatch_gate_a_verdict(&repo, issue.number, approved, note)
    }

    /// #2500: the shared low-level `coord gate-a` dispatcher — both the
    /// direct "Approve Gate A" path (`note` always `""`) and
    /// `submit_gate_a_changes_note_input` (after the operator types a note
    /// or submits blank) land here with an already-resolved `(repo, issue)`.
    /// `--note` is only ever appended for `--changes`; `--approved` has no
    /// use for one.
    pub(crate) fn dispatch_gate_a_verdict(
        &mut self,
        repo: &str,
        issue: u64,
        approved: bool,
        note: &str,
    ) -> bool {
        let flag = if approved { "--approved" } else { "--changes" };
        let issue_str = issue.to_string();
        let mut cmd_strs: Vec<String> = vec![
            "gate-a".to_string(),
            flag.to_string(),
            repo.to_string(),
            issue_str,
        ];
        let note = note.trim();
        if !approved && !note.is_empty() {
            cmd_strs.push("--note".to_string());
            cmd_strs.push(note.to_string());
        }
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    if approved {
                        "Gate A approved"
                    } else {
                        "Gate A changes requested"
                    },
                    &format!("coord {}", cmd_strs.join(" ")),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "Gate A verdict runs after current command",
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Deduped => false,
        }
    }

    /// #2500: open the "What needs to change?" prompt for the selected
    /// epic row ahead of `--changes`. Resolves `(repo, issue)` from
    /// `pipeline_sel` up front — same posture as
    /// `dispatch_gate_a_verdict_for_selected_pipeline_row` itself — so
    /// `submit_gate_a_changes_note_input` doesn't need to re-derive the
    /// selection after the operator has been typing (which could in theory
    /// have moved, though the dialog owns all input while open).
    pub(crate) fn open_gate_a_changes_note_input(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(repo) = issue.coord_repo.clone() else {
            self.push_toast(
                "Request Gate A changes",
                "No local repo mapping for this issue — cannot record a verdict.",
                ToastSeverity::Warning,
            );
            return false;
        };
        self.pending_gate_a_changes_note = Some(PendingGateAChangesNote {
            repo_name: repo,
            issue_number: issue.number,
            buf: String::new(),
        });
        true
    }

    /// #2500: submit the "What needs to change?" prompt — an empty buffer
    /// is fine (falls back to the pre-#2500 behavior of `--changes` with no
    /// `--note`); Cancel (`events.rs` / `dialogs.rs` fire_dialog_button)
    /// never reaches here at all, so unlike before this gesture the click
    /// always fired.
    pub(crate) fn submit_gate_a_changes_note_input(&mut self, input: PendingGateAChangesNote) {
        self.dispatch_gate_a_verdict(&input.repo_name, input.issue_number, false, &input.buf);
    }

    /// #1060 (docs/ORACLE_LOOP.md, #931): resolve the milestone
    /// tracking-issue number for `issue` via `self.data.milestone_work_orders`
    /// (#795) — the epic that owns the `## Work order` block `issue` is a
    /// member node of. `None` when the issue has no `coord_repo`, the daemon
    /// predates #795 (empty list), or the issue isn't a member of any
    /// tracked work order. Used to build the `coord acceptance author
    /// <repo> <tracking_issue> --issue <N>` argument list without a new
    /// board field — the work-order projection already carries it.
    pub(crate) fn milestone_tracking_issue_for(&self, issue: &PipelineIssue) -> Option<u64> {
        let repo = issue.coord_repo.as_deref()?;
        self.data.milestone_work_orders.iter().find_map(|mwo| {
            (mwo.repo_name == repo && mwo.nodes.iter().any(|n| n.issue_number == issue.number))
                .then_some(mwo.tracking_issue)
        })
    }

    /// #1060: local-filesystem check mirroring Python's
    /// `coord.acceptance.gate_a_contract_path` — whether
    /// `tests/acceptance/ms-NN/contract.md` exists in the issue's local
    /// checkout, where NN is the issue's milestone number (via
    /// `pipeline_issue_milestone`). Used to gate "Author acceptance tests":
    /// the independent test-author reads the contract from its own checkout
    /// (#931), so JIT authoring is meaningless before Gate A has written it.
    /// Conservatively `false` (disabled) when the issue has no resolvable
    /// milestone or no local checkout path is known — the same "can't
    /// dispatch, so don't offer it" default the other gates in this file use.
    pub(crate) fn gate_a_contract_exists_for(&self, issue: &PipelineIssue) -> bool {
        let Some((milestone_number, _)) = self.pipeline_issue_milestone(issue) else {
            return false;
        };
        let Some(repo) = issue.coord_repo.as_deref() else {
            return false;
        };
        let Some(local_path) = self.data.pipeline_repo_paths.get(repo) else {
            return false;
        };
        std::path::Path::new(local_path.as_str())
            .join("tests")
            .join("acceptance")
            .join(format!("ms-{}", milestone_number))
            .join("contract.md")
            .is_file()
    }

    /// #2513 (PDR-5): local-filesystem check — whether
    /// `tests/acceptance/ms-NN/mocks/` exists and has at least one rendered
    /// `.html` file in the issue's local checkout, where NN is the issue's
    /// milestone number (via `pipeline_issue_milestone`). Same shape as
    /// `gate_a_contract_exists_for` right above (#2501's "can't dispatch,
    /// so don't offer it" posture), checking the mocks directory instead of
    /// `contract.md` — gates "Publish mocks to portal": there is nothing on
    /// disk to upload before Gate A has rendered at least one mock.
    /// Conservatively `false` when the issue has no resolvable milestone or
    /// no local checkout path is known.
    ///
    /// Filters on `.html` suffix to mirror the CLI's own
    /// `_collect_local_mock_bundle_files` (`coord/commands/portal.py`),
    /// which globs `mocks/*.html` — a `mocks/` dir containing only
    /// non-`.html` junk (a stray file, an empty subdirectory, `.DS_Store`)
    /// would otherwise enable this menu item only for the dispatched
    /// `coord portal publish-mocks` to fail loud with "nothing to publish".
    ///
    /// #2501 shares this same gate for "View Gate A mock (local)": that
    /// action opens a *rendered* `.html` mock, so the identical "at least
    /// one `.html` on disk" question is the right one there too.
    /// Deliberately cheap and synchronous (no network, no pull) for both
    /// callers — #2501's action itself fast-forward-pulls the local checkout
    /// before opening (see `pull_default_branch_ff_only`) to catch a mock
    /// that landed upstream after this checkout was last refreshed, so this
    /// check only needs to answer "is there anything here yet worth offering
    /// the menu item for".
    pub(crate) fn gate_a_mocks_dir_exists_for(&self, issue: &PipelineIssue) -> bool {
        let Some((milestone_number, _)) = self.pipeline_issue_milestone(issue) else {
            return false;
        };
        let Some(repo) = issue.coord_repo.as_deref() else {
            return false;
        };
        let Some(local_path) = self.data.pipeline_repo_paths.get(repo) else {
            return false;
        };
        let mocks_dir = std::path::Path::new(local_path.as_str())
            .join("tests")
            .join("acceptance")
            .join(format!("ms-{}", milestone_number))
            .join("mocks");
        mocks_dir
            .read_dir()
            .map(|entries| {
                entries.filter_map(Result::ok).any(|entry| {
                    entry.path().is_file()
                        && entry
                            .path()
                            .extension()
                            .is_some_and(|ext| ext.eq_ignore_ascii_case("html"))
                })
            })
            .unwrap_or(false)
    }

    /// #2501: "View Gate A mock (local)" — the actual dispatch behind the
    /// menu item gated by `gate_a_mocks_dir_exists_for` above. Opens the
    /// mock-author's rendered mock(s) straight from the local checkout
    /// rather than the worker's PR, which GitHub can never render live
    /// (`.html` shows as a source diff in "Files changed" — there has never
    /// actually been a way to *see* the rendered mock through the PR route).
    ///
    /// Freshness first: the whole point of this action is to see the
    /// *latest* render, and the operator's local checkout can be stale (the
    /// "I had to git pull myself" gap this issue exists to close) — so this
    /// fast-forward-pulls the checkout's default branch via
    /// `pull_default_branch_ff_only` before ever touching the mocks
    /// directory. That helper never forces and never touches a dirty or
    /// diverged working tree; any non-clean outcome aborts here with a
    /// toast telling the operator to sort out the checkout themselves,
    /// exactly the posture this repo already takes everywhere else around
    /// destructive git operations. Only once the pull is clean (or already
    /// up to date) do we check for the mocks directory — deliberately
    /// *after* the pull, not before, so a first-ever pull that just
    /// produced the mocks dir locally still works, not only a refresh of an
    /// already-existing one.
    ///
    /// #2512 will add a deterministic `mocks/index.html` linking every
    /// screen-state together; once it exists we open that, otherwise we
    /// fall back to opening the mocks directory itself so this issue isn't
    /// blocked on #2512 landing first.
    pub(crate) fn dispatch_view_gate_a_mock_local_for_selected_pipeline_row(&mut self) -> bool {
        let title = "View Gate A mock (local)";
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some((milestone_number, _)) = self.pipeline_issue_milestone(&issue) else {
            self.push_toast(
                title,
                "No resolvable milestone for this issue — cannot locate its mocks.",
                ToastSeverity::Warning,
            );
            return false;
        };
        let Some(repo) = issue.coord_repo.clone() else {
            self.push_toast(
                title,
                "No local repo mapping for this issue.",
                ToastSeverity::Warning,
            );
            return false;
        };
        let Some(local_path) = self.data.pipeline_repo_paths.get(&repo).cloned() else {
            self.push_toast(
                title,
                "No local checkout path known for this repo.",
                ToastSeverity::Warning,
            );
            return false;
        };
        let repo_dir = std::path::Path::new(&local_path);

        match pull_default_branch_ff_only(repo_dir) {
            LocalPullOutcome::Dirty => {
                self.push_toast(
                    title,
                    "Local checkout has uncommitted changes — sort out the \
                     checkout yourself, then retry (never auto-discarding \
                     local work).",
                    ToastSeverity::Warning,
                );
                return false;
            }
            LocalPullOutcome::WrongBranch(branch) => {
                self.push_toast(
                    title,
                    &format!(
                        "Local checkout has '{branch}' checked out, not the \
                         remote's default branch — switch and pull yourself, \
                         then retry."
                    ),
                    ToastSeverity::Warning,
                );
                return false;
            }
            LocalPullOutcome::Diverged => {
                // #2501 review: `Diverged` fires on ANY `git merge --ff-only`
                // failure, not only a genuine divergence (a rarer
                // merge-time error can hit this path too) — the abort
                // behaviour is correct either way (never merges), but say
                // "usually" rather than asserting divergence outright so
                // the wording stays honest in that rarer case.
                self.push_toast(
                    title,
                    "Local checkout could not be fast-forwarded onto the \
                     remote default branch (usually because it has \
                     diverged) — sort out the checkout yourself, then retry \
                     (never force-pulling).",
                    ToastSeverity::Warning,
                );
                return false;
            }
            LocalPullOutcome::Failed(reason) => {
                self.push_toast(
                    title,
                    &format!("git pull failed: {reason}"),
                    ToastSeverity::Warning,
                );
                return false;
            }
            LocalPullOutcome::Ok => {}
        }

        let mocks_dir = repo_dir
            .join("tests")
            .join("acceptance")
            .join(format!("ms-{}", milestone_number))
            .join("mocks");
        if !mocks_dir.is_dir() {
            self.push_toast(
                title,
                "No mocks found for this milestone after pulling — dispatch \
                 Gate A mock first, or wait for the worker to push its \
                 branch.",
                ToastSeverity::Warning,
            );
            return false;
        }
        // #2512: once the deterministic master index lands, open that so
        // every screen-state is reachable from one page; until then (or for
        // a milestone whose mock predates #2512) fall back to the mocks
        // directory itself.
        let index = mocks_dir.join("index.html");
        // `opened_what` rides along in the status message purely so this is
        // observable from a test without needing to mock the OS opener
        // (`open_local_path_in_browser` fires a real subprocess and doesn't
        // report back what it opened).
        let (target, opened_what) = if index.is_file() {
            (index, "index.html")
        } else {
            (mocks_dir, "mocks/")
        };
        open_local_path_in_browser(&target);
        self.pipeline_status = Some((
            format!(
                "opening Gate A mock for ms-{} ({}) in browser…",
                milestone_number, opened_what
            ),
            Instant::now(),
        ));
        true
    }

    /// #1174: resolve `(issue, repo, tracking_issue, for_path)` for the
    /// selected pipeline row's acceptance-author dispatch — shared by the
    /// headless (`dispatch_acceptance_author_for_selected_pipeline_row`,
    /// #1060) and interactive
    /// (`launch_acceptance_author_interactive_for_selected_pipeline_row`,
    /// #1173) variants so the gating logic and toast wording live in one
    /// place rather than being duplicated between the two dispatch shapes.
    /// `label` is the caller's toast title, pushed on any resolution
    /// failure. Returns `None` (having already pushed the appropriate
    /// toast, except for the "no row selected" case which is a silent
    /// no-op like every other selection-dependent dispatcher here) when
    /// resolution fails.
    fn resolve_acceptance_author_target(
        &mut self,
        label: &str,
    ) -> Option<(PipelineIssue, String, u64, Option<String>)> {
        let idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(idx)?.clone();
        let Some(repo) = issue.coord_repo.clone() else {
            self.push_toast(
                label,
                "No local repo mapping for this issue — cannot dispatch.",
                ToastSeverity::Warning,
            );
            return None;
        };
        let Some(tracking_issue) = self.milestone_tracking_issue_for(&issue) else {
            self.push_toast(
                label,
                "This issue isn't a member of any tracked milestone work order — \
                 cannot resolve the tracking issue to author against.",
                ToastSeverity::Warning,
            );
            return None;
        };
        // #1151: resolve --for-path before dispatching — see
        // `acceptance_for_path_arg`'s doc for the three outcomes.
        let for_path = match self.acceptance_for_path_arg(&repo, &issue.body) {
            Ok(p) => p,
            Err(reason) => {
                self.push_toast(label, &reason, ToastSeverity::Warning);
                return None;
            }
        };
        Some((issue, repo, tracking_issue, for_path))
    }

    /// #1060 (docs/ORACLE_LOOP.md, #931): dispatch `coord acceptance author
    /// <repo> <tracking_issue> --issue <N>` for the selected pipeline row —
    /// the independent test-author's JIT slice for this issue, extending the
    /// milestone's sealed acceptance suite from its Gate A contract.
    /// Headless, like `dispatch_gate_a_mock_for_selected_pipeline_row`: a
    /// normal (non-interactive) `claude -p` worker, not a live tmux session.
    /// The CLI's own validation (contract exists, issue is a work-order
    /// member) surfaces failures via the command runner's usual toast path.
    pub(crate) fn dispatch_acceptance_author_for_selected_pipeline_row(&mut self) -> bool {
        let Some((issue, repo, tracking_issue, for_path)) =
            self.resolve_acceptance_author_target("Author acceptance tests")
        else {
            return false;
        };
        let mut cmd_strs: Vec<String> = vec![
            "acceptance".to_string(),
            "author".to_string(),
            repo.clone(),
            tracking_issue.to_string(),
            "--issue".to_string(),
            issue.number.to_string(),
        ];
        if let Some(p) = for_path {
            cmd_strs.push("--for-path".to_string());
            cmd_strs.push(p);
        }
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Acceptance author dispatched",
                    &format!("coord {}", cmd_strs.join(" ")),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "Acceptance author dispatch runs after current command",
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Deduped => false,
        }
    }

    /// #1174 (docs/ORACLE_LOOP.md, #1173): launch `coord acceptance author
    /// <repo> <tracking_issue> --issue <N> [--for-path <glob>]
    /// --interactive` in the standalone Terminal pane for the selected
    /// Pipeline row — the human-attended counterpart to
    /// `dispatch_acceptance_author_for_selected_pipeline_row`, giving the
    /// test-authoring stage the same "(interactive)" sibling that
    /// Test/Review/Merge already have.
    ///
    /// Unlike Work/Review/Fix/Test/Merge, this dispatch has no work_aid to
    /// key off — it targets a milestone/tracking-issue slice, not an
    /// existing work assignment — so it does NOT go through
    /// `InteractiveLaunchMode` / `launch_interactive_session_for_selected_issue`
    /// (whose machine-picker/reattach machinery is all built around
    /// `coord assign --interactive --xxx-of <work_aid>`). It instead
    /// follows the bespoke shape `launch_merge_queue_interactive` /
    /// `launch_milestone_chat_session` use: build the launch line, spawn
    /// (or reuse) the standalone Terminal pane, send it. The CLI's
    /// `--interactive` flag (#1173) does all the PTY/tmux/local-vs-remote
    /// work itself — this function does not reimplement any of that, only
    /// the same one-line "auto-run the launcher" trick every other
    /// interactive flavour uses (see `build_interactive_launch_cmd`'s doc).
    ///
    /// Reattach: if a `type="test-author"` session is already running for
    /// this issue, attach to it instead of launching a second one — the
    /// same "running session, matching type" gate
    /// `reattachable_session_aid` applies for the `InteractiveLaunchMode`
    /// flavours, inlined here since `test-author` isn't one of that enum's
    /// assignment types.
    ///
    /// #1223 fix: this is per-issue (tied to one specific pipeline row),
    /// exactly like Work/Review/Fix/Test/Merge — so, unlike a genuinely
    /// row-independent target (merge-queue, milestone chat), it must land
    /// the PTY in the row's OWN `detail_terminal_sessions` slot (keyed by
    /// `(repo_slug, issue_num)`) and the Pipeline detail Terminal TAB, not
    /// the global standalone `terminal_session` / `SidebarView::Terminal`
    /// panel. Launching for issue A then issue B used to clobber/reuse the
    /// same global PTY — the correctness hazard #1223 called out.
    pub(crate) fn launch_acceptance_author_interactive_for_selected_pipeline_row(&mut self) -> bool {
        let label = "Author acceptance tests (interactive)";
        let Some((issue, repo, tracking_issue, for_path)) =
            self.resolve_acceptance_author_target(label)
        else {
            return false;
        };
        let issue_key = (issue.repo_slug.clone(), issue.number);

        let reattach_aid = self
            .live_tmux_sessions
            .iter()
            .filter(|s| {
                s.issue_number == Some(issue.number) && s.repo_name.as_deref() == Some(repo.as_str())
            })
            .map(|s| s.assignment_id.clone())
            .find(|aid| {
                self.session_assignment_is_running(aid)
                    && self.assignment_type_of(aid) == Some("test-author")
            });

        let cfg_path = self
            .command_runner
            .config_path
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned());
        let launch_line = if let Some(aid) = reattach_aid.as_deref() {
            let cfg = match cfg_path.as_deref() {
                Some(p) if !p.is_empty() => format!("--config {} ", shell_quote_arg(p)),
                _ => String::new(),
            };
            format!("coord reattach {}{}\r", cfg, shell_quote_arg(aid))
        } else {
            build_acceptance_author_interactive_launch_cmd(
                cfg_path.as_deref(),
                &repo,
                tracking_issue,
                issue.number,
                for_path.as_deref(),
            )
        };

        if let Some(sess) = self.detail_terminal_sessions.get_mut(&issue_key) {
            sess.send_str(&launch_line);
        } else {
            let cwd = self.detail_terminal_cwd(&issue_key);
            let (cols, rows) = self.detail_terminal_pending_dims.get().unwrap_or((80, 24));
            let shell = quadraui::terminal_engine::default_shell();
            match quadraui::terminal_engine::TerminalSession::spawn(
                cols.max(20),
                rows.max(5),
                &shell,
                &cwd,
                10_000,
            ) {
                Ok(mut sess) => {
                    sess.send_str(&launch_line);
                    self.detail_terminal_sessions.insert(issue_key.clone(), sess);
                    self.detail_terminal_spawn_errors.remove(&issue_key);
                }
                Err(e) => {
                    self.detail_terminal_spawn_errors
                        .insert(issue_key, e.to_string());
                    self.push_toast(
                        "Terminal error",
                        &format!("Failed to spawn terminal: {}", e),
                        ToastSeverity::Error,
                    );
                    return false;
                }
            }
        }

        self.detail_terminal_focused = true;
        let status_verb = if reattach_aid.is_some() {
            "Reattaching to"
        } else {
            "Launched"
        };
        self.push_toast(
            label,
            &format!("{} interactive test-author for #{}", status_verb, issue.number),
            ToastSeverity::Info,
        );
        true
    }

    /// #1060 (docs/ORACLE_LOOP.md, #932): dispatch `coord acceptance record
    /// --repo <repo> --issue <N> --sha <sha>` for the selected pipeline
    /// row — the coordinator's external trust-gate re-run of the sealed
    /// acceptance slice against the pushed SHA. The SHA is resolved from the
    /// latest completed work assignment's branch by reading the local
    /// checkout's git refs directly (`read_git_branch_head`, the same
    /// mechanism `poll_test_plan_staleness` uses) — a headless worker can lie
    /// about "green" in its own session; it can't fake the coordinator
    /// re-running the sealed suite itself against the exact SHA it pushed.
    pub(crate) fn dispatch_acceptance_record_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(repo) = issue.coord_repo.clone() else {
            self.push_toast(
                "Record acceptance",
                "No local repo mapping for this issue — cannot dispatch.",
                ToastSeverity::Warning,
            );
            return false;
        };
        // #1151: resolve --for-path (or bail with the designed toast) BEFORE
        // the branch/SHA lookup below — no point reading git refs for a
        // dispatch the CLI's routing gate will reject anyway. See
        // `acceptance_for_path_arg`'s doc for the three outcomes.
        let for_path = match self.acceptance_for_path_arg(&repo, &issue.body) {
            Ok(p) => p,
            Err(reason) => {
                self.push_toast("Record acceptance", &reason, ToastSeverity::Warning);
                return false;
            }
        };
        let Some(work) = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number && a.repo == repo)
            .filter(|a| {
                matches!(a.assignment_type.as_deref().unwrap_or("work"), "work" | "mock-author")
            })
            .filter(|a| a.status == "done")
            .filter(|a| a.branch.as_deref().map(|b| !b.is_empty()).unwrap_or(false))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .cloned()
        else {
            self.push_toast(
                "Record acceptance",
                "No completed work assignment with a branch for this issue — \
                 nothing to record against yet.",
                ToastSeverity::Warning,
            );
            return false;
        };
        let branch = work.branch.clone().unwrap_or_default();
        let sha = self
            .data
            .pipeline_repo_paths
            .get(repo.as_str())
            .and_then(|path| read_git_branch_head(std::path::Path::new(path.as_str()), &branch));
        let Some(sha) = sha else {
            self.push_toast(
                "Record acceptance",
                &format!(
                    "Could not resolve the HEAD SHA for branch '{}' in the local \
                     checkout — fetch it locally first, or run `coord acceptance \
                     record --repo {} --issue {} --sha <sha>` manually.",
                    branch, repo, issue.number
                ),
                ToastSeverity::Warning,
            );
            return false;
        };
        let mut cmd_strs: Vec<String> = vec![
            "acceptance".to_string(),
            "record".to_string(),
            "--repo".to_string(),
            repo.clone(),
            "--issue".to_string(),
            issue.number.to_string(),
            "--sha".to_string(),
            sha.clone(),
        ];
        if let Some(p) = for_path {
            cmd_strs.push("--for-path".to_string());
            cmd_strs.push(p);
        }
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                let short_sha = &sha[..sha.len().min(8)];
                let display_cmd: Vec<&str> = cmd_strs
                    .iter()
                    .map(|s| if s == &sha { short_sha } else { s.as_str() })
                    .collect();
                self.push_toast(
                    "Acceptance record dispatched",
                    &format!("coord {}", display_cmd.join(" ")),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "Acceptance record runs after current command",
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Deduped => false,
        }
    }

    /// Launch `coord assign --interactive --merge-of <assignment_id>` in the
    /// standalone Terminal pane (SidebarView::Terminal), reusing the same PTY
    /// infrastructure as Chat/Troubleshoot modes.
    pub(crate) fn launch_merge_queue_interactive(&mut self) {
        // #955: a selected Terminal-tree leaf takes over the main pane
        // (routes to `fleet_terminal_sessions` — see
        // `standalone_pty_session_mut`), which would hide the merge-agent
        // session this call is about to launch into `terminal_session`.
        // Clear the tree selection so the bare-shell pane is visible.
        self.terminal_tree_selected = None;

        // Prefer merge_plan (v0.4.53+ daemon); fall back to raw merge_queue.
        let (work_aid, repo_github, issue_num): (String, String, u64) =
            if !self.data.merge_plan.is_empty() {
                match self.selected_merge_plan_entry() {
                    Some(e) => (e.assignment_id.clone(), e.repo_github.clone(), e.issue_number),
                    None => {
                        self.push_toast(
                            "Resolve interactively",
                            "No entry selected.",
                            ToastSeverity::Warning,
                        );
                        return;
                    }
                }
            } else {
                match self.selected_merge_queue_entry() {
                    Some(e) => (
                        e.assignment_id.clone(),
                        e.repo_github.clone(),
                        e.issue_number.unwrap_or(0),
                    ),
                    None => {
                        self.push_toast(
                            "Resolve interactively",
                            "No entry selected.",
                            ToastSeverity::Warning,
                        );
                        return;
                    }
                }
            };

        // Reverse-lookup: repo_github → coord local repo name.
        let repo: String = self
            .data
            .pipeline_repos
            .iter()
            .find(|(_, gh)| **gh == repo_github)
            .map(|(name, _)| name.clone())
            .unwrap_or_default();
        let machine = self.data.local_machine.clone();

        // Resolve the local checkout path for the terminal's CWD.
        let cwd: std::path::PathBuf = if repo.is_empty() {
            std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"))
        } else if let Some(path_str) = self.data.pipeline_repo_paths.get(&repo) {
            std::path::PathBuf::from(path_str)
        } else {
            std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"))
        };

        let cfg_path = self
            .command_runner
            .config_path
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned());

        // Build the launch command.
        let launch_line = build_interactive_launch_cmd(
            cfg_path.as_deref(),
            &machine,
            if repo.is_empty() { "unknown-repo" } else { &repo },
            issue_num,
            InteractiveLaunchMode::Merge,
            Some(&work_aid),
        );

        let (cols, rows) = self
            .terminal_pending_dims
            .get()
            .unwrap_or((80, 24));
        let shell = quadraui::terminal_engine::default_shell();

        match quadraui::terminal_engine::TerminalSession::spawn(
            cols.max(20),
            rows.max(5),
            &shell,
            &cwd,
            10_000,
        ) {
            Ok(mut sess) => {
                sess.send_str(&launch_line);
                self.terminal_session = Some(sess);
                self.terminal_spawn_error = None;
                self.terminal_focused = true;
                // #1029 bug A: keep the ActivityBar/header chrome in sync too.
                self.switch_active_view(SidebarView::Terminal);
                self.push_toast(
                    "Resolve interactively",
                    &format!("Launched merge agent for #{}", issue_num),
                    ToastSeverity::Info,
                );
            }
            Err(e) => {
                self.terminal_spawn_error = Some(e.to_string());
                self.push_toast(
                    "Terminal error",
                    &format!("Failed to spawn terminal: {}", e),
                    ToastSeverity::Error,
                );
            }
        }
    }

    /// Build the SidebarSystem entries for the Pipeline panel.
    ///
    /// One section per repo; within each repo, issues are bucketed into
    /// five lifecycle sub-groups (New → Refining → Pending → In-progress →
    /// Done).  Empty sub-groups collapse automatically.  Re-runs after every
    /// successful `gh` poll.
    ///
    /// `prev_sel_override` carries the (repo_slug, issue#) of the
    /// previously-selected issue when the caller has just replaced
    /// `self.pipeline_issues` (and thus the internal capture below would
    /// read garbage).  When `None`, the function captures from the current
    /// in-memory state — that's correct for callers that haven't swapped
    /// `pipeline_issues`.  See [`capture_pipeline_selection_id`].
    pub(crate) fn rebuild_pipeline_sidebar(&mut self, prev_sel_override: Option<(String, u64)>) {
        // Preserve selection across rebuilds by (repo_slug, issue#).  Use
        // the caller-provided value when available (it captured before any
        // pipeline_issues swap) and only fall back to the internal capture
        // when the caller didn't touch the list.
        let prev_sel = prev_sel_override.or_else(|| self.capture_pipeline_selection_id());
        // #2284 (ms-65 §2f, applied per-scope): the issue whose Pipeline
        // document tab is active — mirrors `mod.rs`'s `rebuild_board_sidebar`
        // `active_doc` local exactly, keyed by (`repo_slug`, number) to match
        // this scope's own `DocKey` convention (see `pipeline_doc_active_key`'s
        // doc comment). Every issue-row builder below splices a `▸ ` marker
        // ahead of its `#<N>` span when this matches, so "which document am I
        // looking at" is legible in the Pipeline tree exactly like it is on
        // Board — never the Board's own `▸`, which lives in a completely
        // separate `DocTabGroup` (contract §3b: the two scopes never merge).
        let active_doc: Option<DocKey> = self.pipeline_doc_tabs().active_key().cloned();
        // Shared by every issue-row builder below (In-progress/Done/New/
        // Refining-Pending each build their own `spans` vec) so the `▸ N`
        // splice point — same colour, same position ahead of the `#<N>` span
        // — can't drift between the four call sites.
        let doc_marker_span = |issue: &PipelineIssue| -> Option<StyledSpan> {
            active_doc
                .as_ref()
                .is_some_and(|(r, n)| *r == issue.repo_slug && *n == issue.number)
                .then(|| StyledSpan::with_fg("▸ ".to_string(), Color::rgb(220, 220, 120)))
        };
        // Preserve panel scroll across rebuilds — without this, every 15 s
        // refresh resets the sidebar's scroll to 0 and yanks the visible
        // area back to the top, even when the selection itself is restored
        // correctly.
        let prev_panel_scroll = self.pipeline_sidebar.panel_scroll();
        // Section 0 is always the FILTER form; state sections start at
        // `search_offset`.
        let search_offset = 1usize;
        // Preserve per-section collapse state by state key (section indices
        // may shift if sections appear/disappear between rebuilds, so we key
        // by the state identifier rather than the section index).
        let prev_state_collapsed: std::collections::HashMap<&'static str, bool> = self
            .pipeline_state_section_names
            .iter()
            .enumerate()
            .map(|(i, &name)| (name, self.pipeline_sidebar.is_collapsed(i + search_offset)))
            .collect();

        // Collect unique repo keys in stable order.
        let mut repos: Vec<String> = Vec::new();
        for issue in &self.pipeline_issues {
            let key = Self::pipeline_repo_key(issue).to_string();
            if !repos.contains(&key) {
                repos.push(key);
            }
        }

        // ── Pre-compute pending merge-queue depth per repo slug ────────────
        // Owned map (no lifetime tie to self) so the rest of the function can
        // still call &self and &mut self methods freely.
        let pending_depth_by_slug = self.pending_merge_queue_depth_by_slug();

        // ── Compute the five state buckets ──────────────────────────────
        //
        // #194: lifecycle-driven board — display order matches the issue
        // spec: New → Refining → Pending → In-progress → Done.
        // Active issues stay in a flat list (one node per work assignment).
        // The other four states are grouped by repo with expandable sub-
        // headers, since they routinely accumulate multiple items per repo.
        // Each bucket is computed once here and reused for rows + restore.
        let active_flat: Vec<usize> = self.pipeline_active_issues();
        // #1487: repo grouping for the in-progress section (was the Live/Idle
        // split — liveness is now a per-row badge, not a grouping axis).
        let active_by_repo: Vec<(String, Vec<usize>)> = self.pipeline_active_by_repo();
        // #194: the non-Active states split into New / Refining / Pending,
        // each repo-grouped (Pending = the old single "New"/status:ready bucket).
        let new_by_repo: Vec<(String, Vec<usize>)> = self.pipeline_repos_for_state("new");
        let refining_by_repo: Vec<(String, Vec<usize>)> =
            self.pipeline_repos_for_state("refining");
        let pending_by_repo: Vec<(String, Vec<usize>)> = self.pipeline_repos_for_state("pending");
        // Build the list of non-empty state sections in display order.
        // #815: In-progress on top — active work is the most relevant item
        // to see immediately; the pre-dispatch lifecycle states follow in
        // order (New → Refining → Pending). #2405: there is no Done section
        // any more — completed issues live in the `Completed` tab's grid.
        let mut state_sections: Vec<(&'static str, &'static str)> = Vec::new();
        if !active_flat.is_empty() {
            state_sections.push(("in-progress", "In-progress"));
        }
        if !new_by_repo.is_empty() {
            state_sections.push(("new", "New"));
        }
        if !refining_by_repo.is_empty() {
            state_sections.push(("refining", "Refining"));
        }
        if !pending_by_repo.is_empty() {
            state_sections.push(("pending", "Pending"));
        }

        // ── Build sidebar section definitions ────────────────────────────
        let mut defs: Vec<SidebarSectionDef> = Vec::new();
        defs.push(SidebarSectionDef::form("pipeline-search", "FILTER"));
        for &(lc_key, _lc_label) in &state_sections {
            let label = match lc_key {
                "new" => "New".to_string(),
                "refining" => "Refining".to_string(),
                "pending" => "Pending".to_string(),
                "in-progress" => "In-progress".to_string(),
                other => other.to_string(),
            };
            let mut def =
                SidebarSectionDef::new(format!("section:state:{}", lc_key), label);
            def.show_chevron = true;
            def.size = SectionSize::Content;
            defs.push(def);
        }

        // #2452: row shape per section, captured alongside `sidebar` below —
        // mirrors `mod.rs`'s `board_section_rows` exactly (same
        // `(TreePath, is_header)` shape, same reason: `SidebarSystem` exposes
        // no `rows()` getter, so `scroll_pipeline_selection_into_view` has no
        // other way to work out how far down the panel a row sits). Indexed
        // by section, so it has to cover every section, form section (0)
        // included — that slot stays an empty vec (the FILTER form's height
        // comes from its field count instead, same as Board's).
        let defs_len = defs.len();
        let mut section_rows: Vec<Vec<(TreePath, bool)>> = vec![Vec::new(); defs_len];

        let mut sidebar = SidebarSystem::new(defs);
        sidebar.set_navigation_mode(NavigationMode::Selection);
        sidebar.set_allow_collapse(true);
        sidebar.set_scroll_mode(ScrollMode::WholePanel);

        // Populate search form (section 0).
        sidebar.set_form(
            0,
            self.pipeline_search
                .form("pipeline-search", "Filter issues… (#4 = number only)"),
        );

        // Colour palette per lifecycle state — mirrors the Board's
        // Backlog / Refining / Refined / In-flight / Completed palette
        // for visual consistency between the Board and Pipeline panels.
        let state_color = |lc: &str| match lc {
            "in-progress" => Color::rgb(80, 220, 80),
            "pending" => Color::rgb(140, 180, 240),
            "refining" => Color::rgb(200, 170, 90), // amber — refinement in flight
            "new" => Color::rgb(160, 160, 200),     // muted — pre-pipeline / no label
            _ => Color::rgb(140, 140, 160),
        };

        // #1197: (section_idx, path) → epic collapse key, accumulated as the
        // epic rows are emitted below and published to
        // `self.pipeline_epic_row_keys` once the loop releases its &self
        // borrows.  Lets the RowToggleExpand handler recognise an epic row
        // by identity instead of guessing from the path's shape.
        let mut epic_row_keys: std::collections::HashMap<(usize, TreePath), (String, u64)> =
            std::collections::HashMap::new();

        // #1253: which children are nested under *some* epic, computed
        // ONCE across the whole pipeline rather than per-bucket. Bug 1's
        // classifier fix means an epic and an independently-tracked child
        // of it can now land in *different* lifecycle sections (e.g. the
        // epic moves to "in-progress" the moment any child has progress,
        // while a not-yet-started sibling child stays "new") — #1197's
        // original per-bucket `EpicNesting::nested` only suppressed a flat
        // duplicate when the epic and the child shared the same bucket, so
        // once they diverge the child rendered BOTH nested under the epic
        // AND flat in its own section. See `pipeline_globally_nested_children`.
        let globally_nested = self.pipeline_globally_nested_children();

        // ── Populate rows for each state section ─────────────────────────
        for (state_idx, &(lc_key, _lc_label)) in state_sections.iter().enumerate() {
            let section_idx = state_idx + search_offset;
            let mut rows: Vec<TreeRow> = Vec::new();

            match lc_key {
                "in-progress" => {
                    // #1487: one collapsible group per **repo** — the same
                    // grouping the New / Refining / Pending sections use.
                    // (Before #1487 the top level was a Live/Idle liveness
                    // split, which straddled a single epic across two places
                    // in the tree: one child under Live, the rest under Idle,
                    // with the `N/M done` badge rendered twice.  Liveness is
                    // a per-row attribute and now renders as a per-row
                    // `in-flight`/`ready` badge instead.)
                    //
                    // #1069: within each repo group, issues are further
                    // grouped by milestone (3-level path [gi, mi, ii]) — the
                    // same `pipeline_milestones_for_issues` helper the New
                    // section uses.  The milestone header carries no repo
                    // letter badge any more (#1487): the repo node above it
                    // already names the repo, and a repo group can't be
                    // mixed-repo by construction.
                    sidebar.set_section_badge(
                        section_idx,
                        Some(StyledText::plain(format!("({})", active_flat.len()))),
                    );
                    for (gi, (group_key, issue_idxs)) in active_by_repo.iter().enumerate() {
                        let is_expanded = self
                            .pipeline_lifecycle_expanded
                            .get(&("in-progress".to_string(), group_key.clone()))
                            .copied()
                            .unwrap_or(true);
                        rows.push(TreeRow {
                            path: vec![gi as u16],
                            indent: 1,
                            icon: None,
                            text: StyledText {
                                spans: vec![StyledSpan::with_fg(
                                    format!("{} ({})", group_key, issue_idxs.len()),
                                    state_color("in-progress"),
                                )],
                            },
                            badge: None,
                            is_expanded: Some(is_expanded),
                            decoration: Decoration::Header,
                            edit: None,
                        });
                        if !is_expanded {
                            continue;
                        }
                        let milestones = self.pipeline_milestones_for_issues(issue_idxs);
                        for (mi, (mil_key, mil_display, mil_issue_idxs)) in
                            milestones.iter().enumerate()
                        {
                            let is_mil_expanded = self
                                .pipeline_milestone_expanded
                                .get(&(
                                    "in-progress".to_string(),
                                    group_key.clone(),
                                    mil_key.clone(),
                                ))
                                .copied()
                                .unwrap_or(true); // #1069: In-progress defaults expanded.
                            let mil_color = if mil_key == "no-milestone" {
                                Color::rgb(100, 100, 120) // dim for unassigned
                            } else {
                                Color::rgb(160, 160, 200) // muted purple for named
                            };
                            rows.push(TreeRow {
                                path: vec![gi as u16, mi as u16],
                                indent: 2,
                                icon: None,
                                text: StyledText {
                                    spans: vec![StyledSpan::with_fg(
                                        format!(
                                            "{} ({})",
                                            mil_display,
                                            mil_issue_idxs.len()
                                        ),
                                        mil_color,
                                    )],
                                },
                                // #1487: no repo letter badge — the repo node
                                // above already names the repo.
                                badge: None,
                                is_expanded: Some(is_mil_expanded),
                                decoration: Decoration::Header,
                                edit: None,
                            });
                            if !is_mil_expanded {
                                continue;
                            }
                            // #1197: nest each epic's children beneath its
                            // row instead of listing them a second time as
                            // flat siblings in this same milestone bucket.
                            //
                            // #1269/#1487: `full_nesting` (every child,
                            // unfiltered) backs the "N/M done" badge below —
                            // that summary reports against the WHOLE child
                            // set. `nesting` is what actually expands beneath
                            // the row: ALL non-Done children (the epic renders
                            // exactly once now that the section groups by
                            // repo), ordered in-flight first — see
                            // `filter_epic_nesting_for_in_progress`.
                            let full_nesting = self.compute_epic_nesting(mil_issue_idxs);
                            let nesting = Self::filter_epic_nesting_for_in_progress(&full_nesting);
                            for (ii, &issue_idx) in mil_issue_idxs.iter().enumerate() {
                                let issue = &self.pipeline_issues[issue_idx];
                                if Self::is_globally_nested(issue, &globally_nested) {
                                    continue;
                                }
                                let title_color = if issue.coord_repo.is_some() {
                                    Color::rgb(210, 210, 210)
                                } else {
                                    Color::rgb(140, 140, 140)
                                };
                                // #1487: per-row in-flight/ready status — the
                                // signal the Live/Idle group node used to
                                // carry. (The old mixed-repo repo-letter
                                // fallback badge is gone: a repo group can't
                                // be mixed-repo.)
                                let row_badge = Some(
                                    self.in_progress_status_badge(
                                        self.active_issue_in_flight(issue_idx),
                                    ),
                                );
                                let row_path = vec![gi as u16, mi as u16, ii as u16];
                                // #1197: an epic row is a *branch* — it keeps
                                // Decoration::Normal (it's real, actionable
                                // work, not a bucket header) but carries
                                // `is_expanded`, which is what drives the
                                // chevron + its hit region in quadraui's tree.
                                let epic_children = nesting.by_epic.get(&issue_idx);
                                let epic = epic_children.and_then(|children| {
                                    self.epic_expand_state(issue, children)
                                        .map(|(key, expanded)| (key, expanded, children))
                                });
                                let mut spans = Vec::new();
                                // #2284 (ms-65 §2f): active-document marker,
                                // spliced ahead of the `#<N>` span itself.
                                if let Some(marker) = doc_marker_span(issue) {
                                    spans.push(marker);
                                }
                                spans.push(StyledSpan::with_fg(
                                    format!("#{:<5}", issue.number),
                                    Color::rgb(150, 150, 240),
                                ));
                                // #1198: epic marker, label-driven — see
                                // `epic_badge_span`. Spliced between `#N` and
                                // the title (not appended at the end) so it
                                // can't be clipped by a long title or
                                // overwritten by the right-aligned badge.
                                if let Some(marker) = epic_badge_span(issue) {
                                    spans.push(marker);
                                }
                                // #1398: "driving" badge, same splice point.
                                if let Some(marker) = self.drive_badge_span(issue) {
                                    spans.push(marker);
                                }
                                // #1505: "[stuck]" driver-escalation badge,
                                // same splice point.
                                if let Some(marker) = self.escalation_badge_span(issue) {
                                    spans.push(marker);
                                }
                                spans.push(StyledSpan::with_fg(
                                    trunc(&issue.title, 20),
                                    title_color,
                                ));
                                // #1253: "N/M done" child-progress summary,
                                // visible whether expanded or collapsed.
                                // #1269: keyed off `full_nesting` (unfiltered)
                                // so the badge reports the epic's WHOLE child
                                // set, not just the non-Done children that
                                // actually nest beneath the row.
                                if let Some(children) = full_nesting.by_epic.get(&issue_idx) {
                                    if let Some(span) = Self::epic_progress_span(children) {
                                        spans.push(span);
                                    }
                                }
                                rows.push(TreeRow {
                                    path: row_path.clone(),
                                    indent: 3,
                                    icon: None,
                                    text: StyledText { spans },
                                    badge: row_badge,
                                    is_expanded: epic.as_ref().map(|(_, e, _)| *e),
                                    decoration: Decoration::Normal,
                                    edit: None,
                                });
                                if let Some((key, expanded, children)) = epic {
                                    epic_row_keys.insert((section_idx, row_path.clone()), key);
                                    if expanded {
                                        for (ci, child) in children.iter().enumerate() {
                                            rows.push(self.epic_child_tree_row(
                                                &row_path, 4, ci, child,
                                            ));
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                _ => {
                    // New / Refining / Pending: grouped by repo with
                    // expandable repo sub-headers.  #194 expanded this from
                    // the original three sections to all four non-Active
                    // lifecycle states.
                    //
                    // #668: New additionally groups by milestone beneath
                    // the repo level (repo → milestone → issue, 3-level path
                    // [ri, mi, ii]).  Refining and Pending remain 2-level.
                    let repo_groups: &Vec<(String, Vec<usize>)> = match lc_key {
                        "new" => &new_by_repo,
                        "refining" => &refining_by_repo,
                        "pending" => &pending_by_repo,
                        // `state_sections` is built in this function and only
                        // ever contains the five known keys above — this arm
                        // is unreachable.
                        _ => unreachable!("invalid lifecycle key in state_sections"),
                    };
                    let total: usize = repo_groups.iter().map(|(_, v)| v.len()).sum();
                    sidebar.set_section_badge(
                        section_idx,
                        Some(StyledText::plain(format!("({})", total))),
                    );
                    let milestone_grouped = lc_key == "new";
                    for (ri, (repo_key, issue_idxs)) in repo_groups.iter().enumerate() {
                        // Repo sub-header: expand/collapse keyed by
                        // (lifecycle_key, repo_key) — lifecycle first, repo
                        // second, matching the new field comment semantics.
                        let is_expanded = self
                            .pipeline_lifecycle_expanded
                            .get(&(lc_key.to_string(), repo_key.clone()))
                            .copied()
                            .unwrap_or(true);
                        // ── #526: pending merge-queue depth badge ─────────
                        // Resolve coord-local repo key → GitHub slug, then
                        // look up the pre-computed depth for this repo.
                        let repo_slug = self
                            .data
                            .pipeline_repos
                            .iter()
                            .find(|(coord, _)| coord == repo_key)
                            .map(|(_, slug)| slug.as_str())
                            .unwrap_or(repo_key.as_str());
                        let queue_depth = pending_depth_by_slug
                            .get(repo_slug)
                            .copied()
                            .unwrap_or(0);
                        // Show a badge when there are pending queue entries:
                        //   > 5 → amber warning
                        //   1–5 → muted indicator
                        //   0   → no badge
                        let queue_badge = if queue_depth > 5 {
                            Some(Badge::colored(
                                format!("Q:{}", queue_depth),
                                Color::rgb(220, 160, 40), // amber
                            ))
                        } else if queue_depth > 0 {
                            Some(Badge::colored(
                                format!("Q:{}", queue_depth),
                                Color::rgb(160, 140, 100), // muted
                            ))
                        } else {
                            None
                        };
                        rows.push(TreeRow {
                            path: vec![ri as u16],
                            indent: 1,
                            icon: None,
                            text: StyledText {
                                spans: vec![StyledSpan::with_fg(
                                    format!("{} ({})", repo_key, issue_idxs.len()),
                                    state_color(lc_key),
                                )],
                            },
                            badge: queue_badge,
                            is_expanded: Some(is_expanded),
                            decoration: Decoration::Header,
                            edit: None,
                        });
                        if !is_expanded {
                            continue;
                        }

                        if milestone_grouped {
                            // #668: New — 3-level tree: repo → milestone → issue.
                            // Compute milestones for this repo's issue list.
                            let milestones =
                                self.pipeline_milestones_for_issues(issue_idxs);
                            for (mi, (mil_key, mil_display, mil_issue_idxs)) in
                                milestones.iter().enumerate()
                            {
                                // #857: milestones-first view — an untouched
                                // milestone key defaults to collapsed so the
                                // New section opens showing just milestone
                                // headers; once toggled, the choice persists
                                // across rebuilds (handled by the `.get(...)`
                                // lookup below finding the stored value).
                                let is_mil_expanded = self
                                    .pipeline_milestone_expanded
                                    .get(&(
                                        lc_key.to_string(),
                                        repo_key.clone(),
                                        mil_key.clone(),
                                    ))
                                    .copied()
                                    .unwrap_or(false);
                                let mil_color = if mil_key == "no-milestone" {
                                    Color::rgb(100, 100, 120) // dim for unassigned
                                } else {
                                    Color::rgb(160, 160, 200) // muted purple for named
                                };
                                rows.push(TreeRow {
                                    path: vec![ri as u16, mi as u16],
                                    indent: 2,
                                    icon: None,
                                    text: StyledText {
                                        spans: vec![StyledSpan::with_fg(
                                            format!(
                                                "{} ({})",
                                                mil_display,
                                                mil_issue_idxs.len()
                                            ),
                                            mil_color,
                                        )],
                                    },
                                    badge: None,
                                    is_expanded: Some(is_mil_expanded),
                                    decoration: Decoration::Header,
                                    edit: None,
                                });
                                if !is_mil_expanded {
                                    continue;
                                }
                                // #1197: nest each epic's children beneath
                                // its row instead of listing them a second
                                // time as flat siblings in this bucket.
                                let nesting = self.compute_epic_nesting(mil_issue_idxs);
                                for (ii, &issue_idx) in mil_issue_idxs.iter().enumerate() {
                                    let issue = &self.pipeline_issues[issue_idx];
                                    if Self::is_globally_nested(issue, &globally_nested) {
                                        continue;
                                    }
                                    let stage_name = self.derive_current_stage(issue);
                                    let (badge_text, badge_color) = stage_badge(&stage_name, &self.active_theme);
                                    let title_color = if issue.coord_repo.is_some() {
                                        Color::rgb(210, 210, 210)
                                    } else {
                                        Color::rgb(140, 140, 140)
                                    };
                                    let has_live_stream = self.watch_pool.values().any(|ctx| {
                                        ctx.state.issue_number == issue.number && !ctx.sse.done
                                    });
                                    // #795: look up server-computed work-order
                                    // node; only present when the daemon is
                                    // v#795+ and this issue is in a work order.
                                    let wo_node = issue.coord_repo.as_ref().and_then(|rn| {
                                        self.work_order_node_for(rn, issue.number)
                                    });
                                    let mut spans = Vec::new();
                                    // #2284 (ms-65 §2f): active-document
                                    // marker, spliced ahead of the `#<N>`
                                    // span itself.
                                    if let Some(marker) = doc_marker_span(issue) {
                                        spans.push(marker);
                                    }
                                    spans.push(StyledSpan::with_fg(
                                        format!("#{:<5}", issue.number),
                                        Color::rgb(150, 150, 240),
                                    ));
                                    // #1198: epic marker, label-driven — see
                                    // `epic_badge_span`. Spliced between `#N`
                                    // and the title (not appended at the
                                    // end) so it can't be clipped by a long
                                    // title or overwritten by the
                                    // right-aligned stage badge.
                                    if let Some(marker) = epic_badge_span(issue) {
                                        spans.push(marker);
                                    }
                                    // #1398: "driving" badge, same splice point.
                                    if let Some(marker) = self.drive_badge_span(issue) {
                                        spans.push(marker);
                                    }
                                    // #1505: "[stuck]" driver-escalation
                                    // badge, same splice point.
                                    if let Some(marker) = self.escalation_badge_span(issue) {
                                        spans.push(marker);
                                    }
                                    spans.push(StyledSpan::with_fg(
                                        trunc(&issue.title, 20),
                                        title_color,
                                    ));
                                    // #1253: "N/M done" child-progress
                                    // summary, visible whether expanded or
                                    // collapsed.
                                    let epic_children = nesting.by_epic.get(&issue_idx);
                                    if let Some(children) = epic_children {
                                        if let Some(span) = Self::epic_progress_span(children) {
                                            spans.push(span);
                                        }
                                    }
                                    if has_live_stream {
                                        spans.push(StyledSpan::with_fg(
                                            " ▶".to_string(),
                                            Color::rgb(60, 200, 80),
                                        ));
                                    }
                                    // #795: append work-order state badges
                                    // after any live-stream indicator.
                                    if let Some(node) = wo_node {
                                        if node.next_up {
                                            // Ready + unclaimed → dispatcher's
                                            // next candidate for this milestone.
                                            spans.push(StyledSpan::with_fg(
                                                format!(" ↑W{}", node.rank + 1),
                                                Color::rgb(80, 200, 120), // green
                                            ));
                                        } else if node.ready {
                                            // Ready but claimed (in-flight on
                                            // another machine) — dim indicator.
                                            spans.push(StyledSpan::with_fg(
                                                format!(" W{}", node.rank + 1),
                                                Color::rgb(120, 160, 120), // dim green
                                            ));
                                        } else {
                                            // Blocked: show rank + first blocker.
                                            let blocker = node
                                                .blocked_on
                                                .first()
                                                .map(|b| format!("#{b}"))
                                                .unwrap_or_default();
                                            spans.push(StyledSpan::with_fg(
                                                format!(" ⊘W{}←{blocker}", node.rank + 1),
                                                Color::rgb(200, 100, 80), // muted red
                                            ));
                                        }
                                    }
                                    let row_path = vec![ri as u16, mi as u16, ii as u16];
                                    // #1197: epic branch row — see the
                                    // in-progress site.
                                    let epic = epic_children.and_then(|children| {
                                        self.epic_expand_state(issue, children)
                                            .map(|(key, expanded)| (key, expanded, children))
                                    });
                                    rows.push(TreeRow {
                                        path: row_path.clone(),
                                        indent: 3,
                                        icon: None,
                                        text: StyledText { spans },
                                        badge: Some(Badge::colored(&badge_text, badge_color)),
                                        is_expanded: epic.as_ref().map(|(_, e, _)| *e),
                                        decoration: Decoration::Normal,
                                        edit: None,
                                    });
                                    if let Some((key, expanded, children)) = epic {
                                        epic_row_keys
                                            .insert((section_idx, row_path.clone()), key);
                                        if expanded {
                                            for (ci, child) in children.iter().enumerate() {
                                                rows.push(self.epic_child_tree_row(
                                                    &row_path, 4, ci, child,
                                                ));
                                            }
                                        }
                                    }
                                }
                            }
                        } else {
                            // Refining / Pending — 2-level tree: repo → issue.
                            // #1197: nest each epic's children beneath its
                            // row instead of listing them a second time as
                            // flat siblings in this bucket.
                            let nesting = self.compute_epic_nesting(issue_idxs);
                            for (ii, &issue_idx) in issue_idxs.iter().enumerate() {
                                let issue = &self.pipeline_issues[issue_idx];
                                if Self::is_globally_nested(issue, &globally_nested) {
                                    continue;
                                }
                                let stage_name = self.derive_current_stage(issue);
                                let (badge_text, badge_color) = stage_badge(&stage_name, &self.active_theme);
                                let title_color = if issue.coord_repo.is_some() {
                                    Color::rgb(210, 210, 210)
                                } else {
                                    Color::rgb(140, 140, 140)
                                };
                                let has_live_stream = self
                                    .watch_pool
                                    .values()
                                    .any(|ctx| ctx.state.issue_number == issue.number && !ctx.sse.done);
                                let mut spans = Vec::new();
                                // #2284 (ms-65 §2f): active-document marker,
                                // spliced ahead of the `#<N>` span itself.
                                if let Some(marker) = doc_marker_span(issue) {
                                    spans.push(marker);
                                }
                                spans.push(StyledSpan::with_fg(
                                    format!("#{:<5}", issue.number),
                                    Color::rgb(150, 150, 240),
                                ));
                                // #1198: epic marker, label-driven — see
                                // `epic_badge_span`. Spliced between `#N`
                                // and the title (not appended at the end)
                                // so it can't be clipped by a long title or
                                // overwritten by the right-aligned stage
                                // badge.
                                if let Some(marker) = epic_badge_span(issue) {
                                    spans.push(marker);
                                }
                                // #1398: "driving" badge, same splice point.
                                if let Some(marker) = self.drive_badge_span(issue) {
                                    spans.push(marker);
                                }
                                // #1505: "[stuck]" driver-escalation badge,
                                // same splice point.
                                if let Some(marker) = self.escalation_badge_span(issue) {
                                    spans.push(marker);
                                }
                                spans.push(StyledSpan::with_fg(trunc(&issue.title, 20), title_color));
                                // #1253: "N/M done" child-progress summary,
                                // visible whether expanded or collapsed.
                                let epic_children = nesting.by_epic.get(&issue_idx);
                                if let Some(children) = epic_children {
                                    if let Some(span) = Self::epic_progress_span(children) {
                                        spans.push(span);
                                    }
                                }
                                if has_live_stream {
                                    spans.push(StyledSpan::with_fg(
                                        " ▶".to_string(),
                                        Color::rgb(60, 200, 80),
                                    ));
                                }
                                let row_path = vec![ri as u16, ii as u16];
                                // #1197: epic branch row — see the in-progress site.
                                let epic = epic_children.and_then(|children| {
                                    self.epic_expand_state(issue, children)
                                        .map(|(key, expanded)| (key, expanded, children))
                                });
                                rows.push(TreeRow {
                                    path: row_path.clone(),
                                    indent: 2,
                                    icon: None,
                                    text: StyledText { spans },
                                    badge: Some(Badge::colored(&badge_text, badge_color)),
                                    is_expanded: epic.as_ref().map(|(_, e, _)| *e),
                                    decoration: Decoration::Normal,
                                    edit: None,
                                });
                                if let Some((key, expanded, children)) = epic {
                                    epic_row_keys.insert((section_idx, row_path.clone()), key);
                                    if expanded {
                                        for (ci, child) in children.iter().enumerate() {
                                            rows.push(
                                                self.epic_child_tree_row(&row_path, 3, ci, child),
                                            );
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            // #2452: remember this section's row shape before the rows are
            // handed to the sidebar (which offers no way to read them back) —
            // same capture Board takes just before its own `set_rows` call.
            if let Some(slot) = section_rows.get_mut(section_idx) {
                *slot = rows
                    .iter()
                    .map(|r| (r.path.clone(), matches!(r.decoration, Decoration::Header)))
                    .collect();
            }
            sidebar.set_rows(section_idx, rows);
        }
        // #1197: publish this pass's epic-row identity map (stale entries
        // from the previous rebuild are dropped wholesale — paths shift as
        // issues move between sections).
        self.pipeline_epic_row_keys = epic_row_keys;
        self.pipeline_section_rows = section_rows;

        // Default-select the first issue in the first non-empty state section.
        if sidebar.active_section().is_none() && !state_sections.is_empty() {
            let section_idx = search_offset; // first state section
            sidebar.set_active_section(Some(section_idx));
            // Done / Refining / Pending use path [group_idx, issue_idx] (2-level).
            // New and In-progress use path [group_idx, milestone_idx, issue_idx]
            // (3-level — New since #668, In-progress since #1069).
            // #728: Done is now flat [0, issue_idx] (2-level), not 3-level.
            let first_state_key = state_sections.get(0).map(|&(k, _)| k).unwrap_or("");
            let default_path = if first_state_key == "new" || first_state_key == "in-progress" {
                vec![0u16, 0u16, 0u16]
            } else {
                vec![0u16, 0u16]
            };
            sidebar.set_selected_path(section_idx, Some(default_path));
        }

        self.pipeline_repo_names = repos;
        let new_state_section_names: Vec<&'static str> =
            state_sections.iter().map(|&(k, _)| k).collect();
        self.pipeline_state_section_names = new_state_section_names;
        self.pipeline_sidebar = sidebar;

        // Capture the previous issue number.  Used to decide whether the
        // focused-stage index needs to be recomputed (we only reset on issue
        // change, not on every refresh).
        let prev_issue_num: Option<u64> = prev_sel.as_ref().map(|(_, num)| *num);

        // Restore previous selection if the issue still exists in the new
        // layout.  `locate_pipeline_selection` is nesting-aware — it finds a
        // row whether it renders top-level or nested under an epic (#1199),
        // where the old inline top-level-only search would fail and let the
        // default-select snap the selection to the epic parent.  A nested
        // child that isn't independently tracked resolves to `sel = None`:
        // the row stays visually selected, and `pipeline_sel` is synced from
        // the sidebar path just below (never left pointing at the epic).
        //
        // #2452: `prev_sel` names a SPECIFIC target — either the issue that
        // was actually selected before this rebuild, or (via
        // `reveal_pipeline_active_doc`'s `prev_sel_override`) the Pipeline
        // document tab that just became active. When the active Pipeline
        // search filter hides that target's row, `locate_pipeline_selection`
        // returns `None` and this `if let` used to just fall through — which
        // meant the default-select-first block above (`:5421-5446`) silently
        // stood, highlighting an unrelated issue's row with nothing on
        // screen to say that isn't what's actually open/selected. Explicitly
        // clear the tree's selection in that case instead: the search filter
        // itself is never touched (`self.pipeline_search` isn't read here),
        // only the highlight. The ordinary "no target was ever requested"
        // path (`prev_sel` is `None`, e.g. a fresh first paint) is untouched
        // — the default-select-first-row block's own selection stands.
        if let Some((repo, num)) = prev_sel {
            if let Some((section_idx, path, sel)) = self.locate_pipeline_selection(&repo, num) {
                self.pipeline_sidebar.set_active_section(Some(section_idx));
                self.pipeline_sidebar
                    .set_selected_path(section_idx, Some(path));
                self.pipeline_sel = sel;
            } else if let Some(section) = self.pipeline_sidebar.active_section() {
                self.pipeline_sidebar.set_selected_path(section, None);
            }
        }
        // Sync `pipeline_sel` to the sidebar's actual selection.
        self.pipeline_sel = self.selected_pipeline_index();
        // #2452: when a Pipeline document tab is active AND the tree above
        // failed to resolve ANY selection of its own — neither a tracked
        // `pipeline_sel` nor an untracked nested epic-child
        // (`selected_nested_child_id`, #1197/#1199) — fall back to the
        // active tab's issue so the detail pane still follows it, the same
        // principle Board's `board_detail_issue_group` applies. This is the
        // filtered-out case the restore block above just handled by
        // explicitly clearing the tree's selection: without this fallback a
        // search filter hiding the active tab's row would blank the detail
        // pane even though the tab strip still shows that document open.
        //
        // This must NOT fire whenever a doc tab merely happens to be open
        // elsewhere while the tree DOES have its own resolved selection —
        // unlike Board, which keeps two separate accessors for the
        // action-target (`board_selected_issue_group`) vs. the
        // render-target (`board_detail_issue_group`, see mod.rs:7067-7087),
        // Pipeline's `pipeline_sel` alone drives both the detail pane AND
        // every per-issue keybind/context-menu action. Overwriting it
        // unconditionally would silently redirect actions away from a
        // deliberately-selected untracked nested child onto the doc tab's
        // issue instead, the instant any doc tab happened to be open.
        if self.pipeline_sel.is_none() && self.selected_nested_child_id().is_none() {
            if let Some((repo, num)) = self.pipeline_doc_active_key().cloned() {
                if let Some(idx) = self
                    .pipeline_issues
                    .iter()
                    .position(|pi| pi.repo_slug == repo && pi.number == num)
                {
                    self.pipeline_sel = Some(idx);
                }
            }
        }
        // Restore per-section collapse state by state key.  New sections
        // that weren't present before default to expanded. (#815's
        // collapsed-by-default carve-out was Done-only and went with it in
        // #2405 — every remaining section is active work.)
        for (i, &state_key) in self.pipeline_state_section_names.iter().enumerate() {
            if let Some(&was_collapsed) = prev_state_collapsed.get(state_key) {
                self.pipeline_sidebar
                    .set_collapsed(i + search_offset, was_collapsed);
            }
        }
        // Restore panel scroll so the visible area doesn't jump back to the
        // top on every refresh.  Done after selection restore so the active-
        // section's keyboard nav state is set up correctly before scroll is
        // applied.
        self.pipeline_sidebar.set_panel_scroll(prev_panel_scroll);
        // Auto-focus: when the selected issue changes (or on first render
        // with no previous selection), default to the smart stage rather
        // than leaving `pipeline_focused_stage = None`.  When the same
        // issue is re-selected after a data refresh, keep the focus stable
        // so a 15 s refresh doesn't clobber the user's explicit choice.
        let new_issue_num = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|iss| iss.number);
        if new_issue_num != prev_issue_num {
            self.pipeline_focused_stage = self.default_focused_stage_for_selected_issue();
            self.pipeline_stage_content_scroll = 0;
        }
        // Re-apply filter focus after rebuild so the cursor survives auto-refresh.
        if self.pipeline_search.focused {
            self.pipeline_sidebar.focus_form(0, true);
        }
    }

    /// Resolve the SidebarSystem's current selection to a `pipeline_issues`
    /// index.
    ///
    /// Path depth varies by state:
    /// - `in-progress`: `[repo_idx, milestone_idx, issue_idx]` (3-level, #1069 —
    ///   `repo_idx` indexes `pipeline_active_by_repo`; #1487 replaced the
    ///   old Live/Idle liveness bucket with a repo node)
    /// - `new`: `[repo_idx, milestone_idx, issue_idx]` (3-level, #668)
    /// - `done`: `[0, issue_idx]` (2-level flat, #728 — no repo/milestone groups)
    /// - `refining` / `pending`: `[repo_idx, issue_idx]` (2-level)
    ///
    /// A path shorter than the minimum for the state (header row selected)
    /// returns `None`.
    ///
    /// #1197: a path ONE segment longer than the state's base depth selects
    /// a nested epic-child row rather than the ordinary issue at that depth
    /// — resolved via [`Self::resolve_nested_child_index`] so Go/dispatch/etc.
    /// act on the child itself, not silently on the parent epic.
    pub(crate) fn selected_pipeline_index(&self) -> Option<usize> {
        // Section 0 is the FILTER form; state sections start at search_offset.
        let search_offset = 1usize;
        let section = self.pipeline_sidebar.active_section()?;
        if section < search_offset {
            return None; // search section selected, not a state section
        }
        let state_idx = section - search_offset;
        let &state_key = self.pipeline_state_section_names.get(state_idx)?;
        let path = self.pipeline_sidebar.selected_path(section)?;

        if state_key == "new" {
            // #668: 3-level path [repo_idx, milestone_idx, issue_idx].
            if path.len() < 3 {
                return None; // repo or milestone header selected
            }
            let ri = path[0] as usize;
            let mi = path[1] as usize;
            let ii = path[2] as usize;
            let repo_groups = self.pipeline_repos_for_state(state_key);
            let (_, repo_issue_idxs) = repo_groups.get(ri)?;
            let milestones = self.pipeline_milestones_for_issues(repo_issue_idxs);
            let (_, _, mil_issue_idxs) = milestones.get(mi)?;
            let parent_idx = mil_issue_idxs.get(ii).copied()?;
            if path.len() > 3 {
                self.resolve_nested_child_index(parent_idx, path[3] as usize, false)
            } else {
                Some(parent_idx)
            }
        } else if state_key == "in-progress" {
            // #1069/#1487: 3-level path [repo_idx, milestone_idx, issue_idx].
            if path.len() < 3 {
                return None; // repo or milestone header selected
            }
            let gi = path[0] as usize;
            let mi = path[1] as usize;
            let ii = path[2] as usize;
            let groups = self.pipeline_active_by_repo();
            let (_, issue_idxs) = groups.get(gi)?;
            let milestones = self.pipeline_milestones_for_issues(issue_idxs);
            let (_, _, mil_issue_idxs) = milestones.get(mi)?;
            let parent_idx = mil_issue_idxs.get(ii).copied()?;
            if path.len() > 3 {
                // #1281/#1487: a nested child was rendered from the filtered +
                // in-flight-first-sorted child list, so resolving it back to a
                // `pipeline_issues` index must apply the SAME transform.
                self.resolve_nested_child_index(parent_idx, path[3] as usize, true)
            } else {
                Some(parent_idx)
            }
        } else {
            // refining / pending — 2-level [repo_idx, issue_idx].
            if path.len() < 2 {
                return None; // repo sub-header selected
            }
            let gi = path[0] as usize;
            let ii = path[1] as usize;
            let groups = self.pipeline_repos_for_state(state_key);
            let (_, issue_idxs) = groups.get(gi)?;
            let parent_idx = issue_idxs.get(ii).copied()?;
            if path.len() > 2 {
                self.resolve_nested_child_index(parent_idx, path[2] as usize, false)
            } else {
                Some(parent_idx)
            }
        }
    }

    /// #1199 fix (selection snaps to the epic on refresh): locate the sidebar
    /// target — `(section_idx, tree_path, resolved pipeline index)` — for a
    /// previously-selected `(repo_slug, issue#)`, **nesting-aware**.
    ///
    /// The pre-existing restore in `rebuild_pipeline_sidebar` only searched
    /// the *top-level* bucket rows, so it could never re-find a row that is
    /// rendered **nested under an epic**:
    /// - a closed child (e.g. #1097) whose own lifecycle bucket is `done` but
    ///   which renders nested under its still-open epic — the epic lives in a
    ///   different bucket, so no top-level loop over the epic's bucket, nor
    ///   the windowed `done` list, ever contained it; and
    /// - an untracked child (e.g. #1031/#1032/#1033) absent from
    ///   `pipeline_issues` altogether.
    /// Unmatched, the restore fell through to the default-select, which
    /// snapped the selection to the epic parent and replaced the child's own
    /// pipeline with the epic's gate lane — exactly the reported regression.
    ///
    /// This mirrors the row-emission loops (same buckets, same
    /// `compute_epic_nesting`, same path shapes) and checks, per leaf group,
    /// (1) a top-level match that is **not** suppressed by nesting, then
    /// (2) a nested-child match under any epic in the group. The returned
    /// pipeline index is `None` for an untracked nested child (kept visually
    /// selected, but with the "no issue" detail placeholder — never snapped
    /// onto the epic).
    pub(crate) fn locate_pipeline_selection(
        &self,
        repo: &str,
        num: u64,
    ) -> Option<(usize, Vec<u16>, Option<usize>)> {
        let search_offset = 1usize;
        // #1253: mirror the row-emission loops' global (not per-bucket)
        // nested-child check — see `pipeline_globally_nested_children`.
        let globally_nested = self.pipeline_globally_nested_children();
        for (state_idx, &state_key) in self.pipeline_state_section_names.iter().enumerate() {
            let section_idx = state_idx + search_offset;
            // Leaf groups for this state as (path_prefix, issue_idxs,
            // in_progress) — the same shapes the row-emission and
            // `selected_pipeline_index` decoders use. #1487: `in_progress` is
            // `true` only for "in-progress" leaves (whose nested children are
            // filtered + reordered); every other state nests the full,
            // unfiltered child list, matching #1197.
            let leaves: Vec<(Vec<u16>, Vec<usize>, bool)> = match state_key {
                "in-progress" => {
                    let mut out = Vec::new();
                    for (gi, (_, issue_idxs)) in
                        self.pipeline_active_by_repo().iter().enumerate()
                    {
                        for (mi, (_, _, mil)) in self
                            .pipeline_milestones_for_issues(issue_idxs)
                            .into_iter()
                            .enumerate()
                        {
                            out.push((vec![gi as u16, mi as u16], mil, true));
                        }
                    }
                    out
                }
                "new" => {
                    let mut out = Vec::new();
                    for (ri, (_, issue_idxs)) in
                        self.pipeline_repos_for_state("new").iter().enumerate()
                    {
                        for (mi, (_, _, mil)) in self
                            .pipeline_milestones_for_issues(issue_idxs)
                            .into_iter()
                            .enumerate()
                        {
                            out.push((vec![ri as u16, mi as u16], mil, false));
                        }
                    }
                    out
                }
                "refining" | "pending" => self
                    .pipeline_repos_for_state(state_key)
                    .into_iter()
                    .enumerate()
                    .map(|(ri, (_, idxs))| (vec![ri as u16], idxs, false))
                    .collect(),
                _ => continue,
            };

            for (prefix, issue_idxs, in_progress) in &leaves {
                let full_nesting = self.compute_epic_nesting(issue_idxs);
                let nesting = if *in_progress {
                    Self::filter_epic_nesting_for_in_progress(&full_nesting)
                } else {
                    full_nesting
                };
                // (1) top-level row (not suppressed by nesting).
                for (ii, &idx) in issue_idxs.iter().enumerate() {
                    let issue = &self.pipeline_issues[idx];
                    if issue.repo_slug == repo
                        && issue.number == num
                        && !Self::is_globally_nested(issue, &globally_nested)
                    {
                        let mut path = prefix.clone();
                        path.push(ii as u16);
                        return Some((section_idx, path, Some(idx)));
                    }
                }
                // (2) nested epic-child row.
                for (ii, &idx) in issue_idxs.iter().enumerate() {
                    let epic = &self.pipeline_issues[idx];
                    if epic.repo_slug != repo {
                        continue;
                    }
                    if let Some(children) = nesting.by_epic.get(&idx) {
                        if let Some(ci) = children.iter().position(|c| c.number == num) {
                            let mut path = prefix.clone();
                            path.push(ii as u16);
                            path.push(ci as u16);
                            let sel = self.resolve_nested_child_index(idx, ci, *in_progress);
                            return Some((section_idx, path, sel));
                        }
                    }
                }
            }
        }
        None
    }

    /// #550: look up *stage*'s status for *issue* in the server-computed
    /// `issue_stage_projection` (from `/board`, see `coord.stage_projection`),
    /// when present.  Returns `None` when the daemon predates #550, the
    /// local-SQLite read path is in use (no daemon at all), or the issue has
    /// no `coord_repo` mapping — all cases where the caller should fall back
    /// to local computation.
    pub(crate) fn server_stage_status(&self, issue: &PipelineIssue, stage: &str) -> Option<StageStatus> {
        let repo = issue.coord_repo.as_deref()?;
        let entry = self
            .data
            .issue_stage_projection
            .iter()
            .find(|p| p.repo_name == repo && p.issue_number == issue.number)?;
        entry.stages.get(stage).map(|s| parse_stage_status(s))
    }

    /// Resolve the per-stage status of an issue from existing assignments.
    ///
    /// "work" is the first stage and matches assignments with
    /// `assignment_type` `None` or `"work"`.  Other stage names match
    /// assignments by exact `assignment_type`.  The "merge" stage is
    /// special-cased to read from the `merge_queue` table instead, since
    /// merges are not modelled as assignments.
    pub(crate) fn stage_status_for(&self, issue: &PipelineIssue, stage: &str) -> StageStatus {
        if stage == "merge" {
            return self.merge_stage_status_for(issue);
        }
        if stage == "test" {
            return self.test_stage_status_for(issue);
        }
        // #550: prefer the server-computed projection when present — falls
        // back to the local computation below on the local-SQLite read path
        // (no daemon) or against a daemon older than #550.
        if let Some(s) = self.server_stage_status(issue, stage) {
            return s;
        }
        self.stage_status_for_local(issue, stage)
    }

    /// Local fallback for [`stage_status_for`] — the generic (non merge/test)
    /// stage computation `pipeline.rs` used exclusively before #550.  Kept as
    /// the authoritative path for local-SQLite-mode coord-tui (no `coord
    /// serve` daemon to compute the server-side projection) and as a safety
    /// net against older daemons.
    pub(crate) fn stage_status_for_local(&self, issue: &PipelineIssue, stage: &str) -> StageStatus {
        let matching = self.assignments_for_stage(issue, stage);
        // #1566: "finalizing" is a review row whose agent finished but whose
        // verdict hasn't been parsed + persisted yet (`coord notify`'s
        // slower, separate step). Treat it like "running" — without this,
        // the #473/#812 verdict-based match below reads a still-in-flight
        // review as a terminal "done" with no verdict and paints it FAILED,
        // indistinguishable from a genuinely dropped verdict. Mirrors
        // `coord.stage_projection.stage_status_for`.
        if matching.iter().any(|a| a.status == "running" || a.status == "finalizing") {
            return StageStatus::Active;
        }
        let latest = matching.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        if let Some(latest) = latest {
            let mapped = match latest.status.as_str() {
                // #473: the Review stage colour must reflect the VERDICT, not
                // merely that the review process ran (a review always ends
                // status="done").  Keying on status alone painted a
                // `request-changes` verdict GREEN — reading as "approved" and
                // inviting a premature merge.  Consult `review_verdict`:
                //   approve         → Done   (green ✓)
                //   request-changes → Failed (red ✗ — changes requested)
                //   None/unknown    → Failed (red ✗ — #812: a terminal done
                //                     row with no verdict is a dead end, not
                //                     in-progress.  Renders as recoverable
                //                     Failed rather than permanent blue Active)
                // Note: in-flight reviews (status="running") are handled by
                // the `any(status=="running")` guard at the top of this
                // function and never reach this match arm.
                "done" if stage == "review" => match latest.review_verdict.as_deref() {
                    Some("approve") => Some(StageStatus::Done),
                    Some("request-changes") | Some("fail") => Some(StageStatus::Failed),
                    _ => Some(StageStatus::Failed),
                },
                "done" => Some(StageStatus::Done),
                "failed" => Some(StageStatus::Failed),
                _ => None,
            };
            if let Some(v) = mapped {
                // #193: a Done/Failed verdict is only trustworthy if no upstream
                // stage has been re-dispatched since. If any upstream's latest
                // dispatched_at is newer than this stage's latest, the verdict
                // is against an older revision — render as Stale.
                if let Some(this_dispatched) = latest.dispatched_at {
                    if self
                        .upstream_max_dispatched_at(issue, stage)
                        .map_or(false, |u| u > this_dispatched)
                    {
                        return StageStatus::Stale;
                    }
                }
                return v;
            }
        }
        // No matching assignment found.  For closed issues this means the stage
        // was never run through coord — use Skipped so the UI can distinguish
        // "waiting to run" (Pending) from "never ran, issue already closed"
        // (Skipped).
        if issue.is_closed {
            StageStatus::Skipped
        } else {
            StageStatus::Pending
        }
    }

    /// Return all assignments for `issue` whose `assignment_type` matches
    /// `stage`. When pipeline has no Plan gate, plan-typed assignments also
    /// count as Work so the stage advances correctly.
    pub(crate) fn assignments_for_stage<'a>(
        &'a self,
        issue: &PipelineIssue,
        stage: &str,
    ) -> Vec<&'a Assignment> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| {
                if let Some(local) = &issue.coord_repo {
                    a.repo == *local
                } else {
                    true
                }
            })
            .filter(|a| {
                let t = a.assignment_type.as_deref().unwrap_or("work");
                if stage == "work"
                    && !self.data.pipeline_require_plan
                    && !self.issue_has_plan_assignment(issue)
                {
                    // Legacy fold: when there's no Plan stage in this
                    // issue's strip, plan-typed assignments are still
                    // counted as Work so a `--plan-only` dispatch
                    // without `require_plan` doesn't disappear.
                    t == "work" || t == "plan"
                } else {
                    t == stage
                }
            })
            .collect()
    }

    /// Return the max `dispatched_at` across all stages strictly upstream of
    /// `stage`, or None if no upstream stage has an assignment.
    ///
    /// Used by `stage_status_for` to detect stale downstream verdicts.
    pub(crate) fn upstream_max_dispatched_at(&self, issue: &PipelineIssue, stage: &str) -> Option<f64> {
        let names = self.pipeline_stage_names_for_issue(issue);
        let idx = names.iter().position(|s| s == stage)?;
        if idx == 0 {
            return None;
        }
        names[..idx]
            .iter()
            .flat_map(|s| self.assignments_for_stage(issue, s))
            .filter_map(|a| a.dispatched_at)
            .fold(None, |acc, t| Some(acc.map_or(t, |x: f64| x.max(t))))
    }

    /// Resolve the merge stage status from `merge_queue` entries.
    ///
    /// `merged` → Done, `open`/`queued` → Active, `failed` → Failed, anything
    /// else (or no entry) → Pending for open issues, Skipped for closed issues
    /// (the merge never ran through coord).
    pub(crate) fn merge_stage_status_for(&self, issue: &PipelineIssue) -> StageStatus {
        // #241: a running conflict-fix worker keeps the Merge stage Active —
        // the auto-rebase is part of the merge phase, not a separate stage.
        if self.has_active_conflict_fix(issue) {
            return StageStatus::Active;
        }
        // #290: if a merge was just dispatched from the Go button, optimistically
        // return Active immediately so the Merge box turns blue and the Go button
        // disappears — without waiting for the next DB refresh to land the real
        // merge_queue entry.  The flag is cleared in apply_pending_data once the
        // entry exists in the DB, at which point the real state takes over.
        if self
            .pipeline_inflight_merges
            .contains(&(issue.repo_slug.clone(), issue.number))
        {
            let has_real_entry = self
                .data
                .merge_queue
                .iter()
                .any(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
            if !has_real_entry {
                return StageStatus::Active;
            }
        }
        // #550: prefer the server-computed projection when present — falls
        // back to the local computation below on the local-SQLite read path
        // (no daemon) or against a daemon older than #550.
        if let Some(s) = self.server_stage_status(issue, "merge") {
            return s;
        }
        self.merge_stage_status_for_local(issue)
    }

    /// Local fallback for [`merge_stage_status_for`] — the raw
    /// `merge_queue`-derived computation `pipeline.rs` used exclusively
    /// before #550. Kept as the authoritative path for local-SQLite-mode
    /// coord-tui and as a safety net against older daemons.
    pub(crate) fn merge_stage_status_for_local(&self, issue: &PipelineIssue) -> StageStatus {
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
        match entry.map(|e| e.state.as_str()) {
            Some("merged") => StageStatus::Done,
            Some("open") | Some("queued") => StageStatus::Active,
            // #241: HUMAN_REQUIRED (failed conflict-fix) — the merge needs a
            // human, so Failed.  `failed` (legacy / direct) is also Failed.
            // #919 review: `conflict` is a genuine resting/terminal state
            // (coord/merge_queue.py) reached whenever GitHub reports a real
            // merge conflict and no conflict-fix worker is currently
            // resolving it — without this arm it fell through to `_` below,
            // where `ci_failed_for_entry` is reliably false for a
            // conflicting PR (GitHub reports zero CI checks for one, per
            // merge_queue.py's #1877 comment), so the Merge stage box rendered
            // Pending with a lit one-click [Go] — exactly the false green
            // #919 exists to close.
            Some("failed") | Some("human_required") | Some("conflict") => StageStatus::Failed,
            _ => {
                // A pending (not yet merged/active) entry whose PR has failing
                // CI checks goes Failed so the Merge box is red at a glance —
                // the user shouldn't have to open the detail row or wait for a
                // GitHub email to learn a check broke.
                if entry.is_some_and(|e| self.ci_failed_for_entry(e)) {
                    StageStatus::Failed
                } else if self.data.assignments.iter().any(|a| {
                    // #775: the daemon's merge-reconcile tick prunes the queue
                    // row after flipping the work assignment to status='merged'.
                    // Once the row is gone the match above falls here, so we
                    // also check the assignment itself — a merged work assignment
                    // is sufficient evidence that the Merge stage is Done, even
                    // with no surviving queue entry.
                    a.issue_number == issue.number
                        && issue
                            .coord_repo
                            .as_deref()
                            .map(|r| r == a.repo)
                            .unwrap_or(true)
                        && a.assignment_type.as_deref() == Some("work")
                        && a.status == "merged"
                }) {
                    StageStatus::Done
                } else if issue.is_closed {
                    StageStatus::Skipped
                } else if entry.is_some_and(|e| e.state == "skipped") {
                    // #919 review (round 2): `skipped` is the third state the
                    // server folds into PLAN_NEEDS_ATTENTION
                    // (`_state_to_plan_status`) and treats as terminal
                    // alongside `merged` (`_RESOLVE_TERMINAL_STATES`) — a
                    // superseded row on a still-open issue will never
                    // self-resolve, so Pending here lit a one-click [Go] that
                    // `pipeline_merge_state()` (this same commit's sibling
                    // classifier) would refuse to dispatch. Same false green as
                    // `conflict`, one state value over.
                    //
                    // Deliberately checked *here* rather than in the match arm
                    // above: unlike conflict/human_required, a superseded row
                    // routinely survives next to the *successful* attempt for
                    // the same issue whose own row the reconcile tick then
                    // prunes (#775), so matching it earlier would paint a
                    // finished issue red instead of Done/Skipped.
                    StageStatus::Failed
                } else {
                    StageStatus::Pending
                }
            }
        }
    }

    /// #2397: the merge-queue's own computed block reason for a Pending
    /// Merge stage, resolved against the live-computed `data.merge_plan`
    /// (`coord.merge_queue.plan()`, #776) and formatted with the
    /// human/in-flight/needs-a-person affordance
    /// (`format::fmt_merge_block_reason`). `None` when the daemon hasn't
    /// sent a `merge_plan` (pre-#776 daemon, or local-SQLite mode) or no
    /// entry matches — callers already have a bare-clock fallback for that
    /// case.
    ///
    /// Deliberately does not consult `entry.auto_drain`: per
    /// `format::merge_wait_affordance`'s doc comment (#2397 review fix), the
    /// flag has no bearing on whether a BLOCKED plan entry — which is the
    /// only case `entry.reason` is `Some` — ever gets retried automatically.
    fn merge_plan_block_reason(entry: Option<&PlannedMergeEntry>) -> Option<String> {
        let entry = entry?;
        fmt_merge_block_reason(entry.reason.as_deref())
    }

    /// Branch-keyed lookup for the #1084 prereq track (Gate A / Acceptance
    /// Authoring): the merge queue dedups by branch, and a shared tracking
    /// issue can carry BOTH Gate A's and a member issue's JIT test-author
    /// queue entries — an `issue_number`-keyed match (as
    /// [`Self::merge_plan_block_reason_for_issue`] uses for the ordinary
    /// Work track) would ambiguously match either one. Mirrors the raw
    /// `merge_queue` branch lookup `prereq_pipeline_status_from` already
    /// does for `state`, but against `merge_plan` so `reason` is available.
    pub(crate) fn merge_plan_block_reason_for_branch(&self, repo_slug: &str, branch: &str) -> Option<String> {
        Self::merge_plan_block_reason(
            self.data
                .merge_plan
                .iter()
                .find(|e| e.repo_github == repo_slug && e.branch == branch),
        )
    }

    /// #2402: the raw merge-plan entry backing `issue_number`'s Merge stage —
    /// unlike [`Self::merge_plan_block_reason_for_issue`] (which returns the
    /// *formatted*, truncated + affordance-tagged display string), this hands
    /// back the whole [`PlannedMergeEntry`] so a caller can read
    /// `status`/`reason`/`assignment_id` together. The Pipeline row's
    /// "Re-verify (revalidate)" context-menu action needs exactly that: the
    /// raw `reason` text to run through `format::merge_revalidate_eligible`,
    /// and `assignment_id` to pass to `coord merge --only <assignment_id>`.
    /// Same issue-number-keyed lookup as `merge_plan_block_reason_for_issue`
    /// — safe here since a member issue's own Merge stage has exactly one
    /// queue entry (unlike the shared-tracking-issue prereq case above).
    pub(crate) fn merge_plan_entry_for_issue(
        &self,
        repo_slug: &str,
        issue_number: u64,
    ) -> Option<&PlannedMergeEntry> {
        self.data
            .merge_plan
            .iter()
            .find(|e| e.repo_github == repo_slug && e.issue_number == issue_number)
    }

    /// Issue-number-keyed lookup for the ordinary Work→Test→Review→Merge
    /// track, mirroring `merge_stage_status_for_local`'s own `merge_queue`
    /// lookup (safe here since a member issue's own Merge stage has exactly
    /// one queue entry, unlike the shared-tracking-issue prereq case above).
    pub(crate) fn merge_plan_block_reason_for_issue(&self, repo_slug: &str, issue_number: u64) -> Option<String> {
        Self::merge_plan_block_reason(self.merge_plan_entry_for_issue(repo_slug, issue_number))
    }

    /// True when *entry*'s PR has a fetched CI summary with failing checks.
    /// Looks up `pipeline_ci_checks` by `(repo_github, pr_number)`; returns
    /// false when the entry has no PR yet or no summary has been fetched.
    pub(crate) fn ci_failed_for_entry(&self, entry: &MergeQueueEntry) -> bool {
        let Some(pr) = entry.pr_number else {
            return false;
        };
        self.pipeline_ci_checks
            .get(&(entry.repo_github.clone(), pr))
            .is_some_and(|s| s.has_failures())
    }

    /// #241: is there a conflict-fix worker currently in flight for *issue*?
    pub(crate) fn has_active_conflict_fix(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && a.assignment_type.as_deref() == Some("conflict-fix")
                && (a.status == "running" || a.status == "pending")
        })
    }

    /// #585: is there a manual/interactive smoke (or test-chat) session in
    /// flight for *issue*?  Mirrors [`has_active_conflict_fix`] for the Merge
    /// box: while the operator is re-verifying, the Test box should read blue
    /// (Active) — even over a prior automated `passed` verdict — so the green
    /// box doesn't imply the verdict is already in when it isn't yet.  The
    /// interactive testing agent (`--smoke-of`) is `type="smoke"`; the
    /// conversational gate is `type="test-chat"`.
    pub(crate) fn has_active_smoke_session(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && issue.coord_repo.as_deref().map_or(true, |r| a.repo == r)
                && matches!(
                    a.assignment_type.as_deref(),
                    Some("smoke") | Some("test-chat")
                )
                && (a.status == "running" || a.status == "pending")
        })
    }

    /// #200: Resolve the Test gate status from `test_state` on the latest
    /// Work assignment for `issue`.
    ///
    /// `passed`/`skipped` → Done; `failed` → Failed; otherwise Pending while
    /// Work is settled and Pending/Skipped while Work isn't done yet.
    /// #235: When a Phase 1 build is in flight for the latest Work
    /// assignment, the badge goes Active ("Building") even though Test is
    /// still a human gate — the user needs to know something is running.
    pub(crate) fn test_stage_status_for(&self, issue: &PipelineIssue) -> StageStatus {
        // Test is gated on Work; if Work hasn't finished, Test isn't actionable.
        let work_status = self.stage_status_for_internal_work(issue);
        if work_status != StageStatus::Done {
            // Work is still Active/Pending/Failed/Stale — Test inherits Pending
            // (or Skipped for closed issues that bypassed Work).
            return if issue.is_closed {
                StageStatus::Skipped
            } else {
                StageStatus::Pending
            };
        }
        // Work is done — read the verdict off the latest Work assignment.
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        // #235: Phase 1 build in flight beats any prior verdict — the user
        // pressed `B` to re-test, so the old verdict is no longer current.
        // TUI-session-local (a locally-spawned subprocess); no server
        // equivalent, so this overlay is always applied before consulting
        // the server-computed projection below.
        if let Some(a) = latest.as_ref() {
            if self.test_build_in_flight(&a.id) {
                return StageStatus::Active;
            }
        }
        // #550: prefer the server-computed projection when present — falls
        // back to the local computation below on the local-SQLite read path
        // (no daemon) or against a daemon older than #550.
        if let Some(s) = self.server_stage_status(issue, "test") {
            return s;
        }
        // #585: a manual/interactive smoke session in flight keeps the Test box
        // blue (Active) — even over a prior `passed` verdict — so the operator
        // sees the gate is being re-evaluated, not already decided.  Resolves to
        // green/red once the session ends and its verdict is the latest.
        if self.has_active_smoke_session(issue) {
            return StageStatus::Active;
        }
        // #310: a review bounce creates a new fix-work assignment that carries
        // an *empty* test_state. The test genuinely passed on the earlier work,
        // so don't revert Test to Pending (which would strand the Merge "Go"
        // button via prior_all_done). Resolve the verdict from the most recent
        // work assignment that actually carries one. A later re-test that
        // failed will itself carry test_state="failed" and win as the most
        // recent verdict; an in-flight re-test is handled above.
        let verdict = work
            .iter()
            .filter(|a| {
                a.test_state
                    .as_deref()
                    .map(|s| !s.is_empty())
                    .unwrap_or(false)
            })
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .and_then(|a| a.test_state.as_deref());
        // #1395: "running" is a transient, non-verdict marker an unattended
        // driver (scripts/drive-issue.sh) sets while it runs the suite
        // locally (bypassing dispatch_smoke, so there's no `type="smoke"`
        // assignment for `has_active_smoke_session` above to catch) —
        // mirrors `coord.stage_projection.test_stage_status_for`. It is
        // overwritten with a terminal passed/failed/skipped verdict when the
        // run concludes; every gate elsewhere keys off the terminal values
        // explicitly, so this never reads as a verdict anywhere else.
        match verdict {
            Some("passed") | Some("skipped") => StageStatus::Done,
            // #2579: "contested" (coord.notify.TEST_STATE_CONTESTED) is an
            // independent #2464 re-run REFUTING a pass claim whose review
            // had already approved it — deliberately distinct from the
            // literal "failed" so no automatic fix-dispatch door mistakes it
            // for an ordinary Test-stage failure, but it IS a statement
            // about the branch, so it renders identically to "failed" here
            // (mirrors coord.stage_projection.test_stage_status_for).
            Some("failed") | Some("contested") => StageStatus::Failed,
            Some("running") => StageStatus::Active,
            _ => StageStatus::Pending,
        }
    }

    /// #932/#944: the Acceptance box's status — reported and gated
    /// SEPARATELY from Test (docs/ORACLE_LOOP.md), so this reads
    /// `Assignment.acceptance_state` off the work assignment row directly
    /// rather than going through `test_stage_status_for`'s Work-gating
    /// dance: `coord acceptance record` stamps the verdict whenever the
    /// external re-run happens, independent of whether the human Test gate
    /// has run yet.
    ///
    /// Distinct from Test in one more way: an issue with no acceptance
    /// suite authored yet (no manifest slice, so `acceptance record` was
    /// never run against it) has no signal at all — Skipped rather than
    /// Pending, since the Acceptance box only applies to oracle-loop
    /// milestones, not every issue on the board.
    pub(crate) fn acceptance_stage_status_for(&self, issue: &PipelineIssue) -> StageStatus {
        // #550: prefer the server-computed projection when present — falls
        // back to the local computation below on the local-SQLite read path
        // (no daemon) or against a daemon older than #550.
        if let Some(s) = self.server_stage_status(issue, "acceptance") {
            return s;
        }
        self.acceptance_stage_status_for_local(issue)
    }

    /// Local fallback for [`acceptance_stage_status_for`] — authoritative
    /// for local-SQLite-mode coord-tui (no `coord serve` daemon) and a
    /// safety net against older daemons that predate #932's projection.
    pub(crate) fn acceptance_stage_status_for_local(&self, issue: &PipelineIssue) -> StageStatus {
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
        match latest.and_then(|a| a.acceptance_state.as_deref()) {
            Some("passed") => StageStatus::Done,
            Some("failed") => StageStatus::Failed,
            _ => StageStatus::Skipped,
        }
    }

    /// The Acceptance box's per-test progress (e.g. "3/7") from the latest
    /// recorded verdict, or `None` when no verdict exists yet or it
    /// predates #932's per-test counts. Reporting only — the Acceptance
    /// box's colour comes from [`acceptance_stage_status_for`], not this.
    pub(crate) fn acceptance_progress_for(&self, issue: &PipelineIssue) -> Option<(i64, i64)> {
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
            })?;
        Some((latest.acceptance_passed?, latest.acceptance_total?))
    }

    /// Core resolver shared by [`gate_a_prereq_status`] and
    /// [`acceptance_authoring_prereq_status`]: given the already-resolved
    /// Author-stage assignment (or `None` if the track never ran), compute
    /// the 4-stage status. `repo_slug` is the issue's GitHub `owner/repo`
    /// (used to key the Merge stage's `merge_queue` lookup by branch).
    pub(crate) fn prereq_pipeline_status_from(
        &self,
        repo_slug: &str,
        author: Option<&Assignment>,
    ) -> PrereqPipelineStatus {
        let Some(author) = author else {
            return PrereqPipelineStatus {
                author_id: None,
                stages: [
                    PrereqStage { status: StageStatus::Pending, since: None, block_reason: None },
                    PrereqStage { status: StageStatus::Pending, since: None, block_reason: None },
                    PrereqStage { status: StageStatus::Pending, since: None, block_reason: None },
                    PrereqStage { status: StageStatus::Pending, since: None, block_reason: None },
                ],
            };
        };

        // #1581: `coord/dao.py`'s `TERMINAL_STATUSES` is {done, merged, failed,
        // cancelled, advisory} — every terminal value an assignment can settle
        // into. This match must account for all of them, not just the "done"/
        // "failed" pair a plain Work assignment cycles through, or a terminal
        // status silently falls through the `_` arm to `StageStatus::Pending`
        // with `since: None` — which #1572 defined to mean "never dispatched"
        // and renders as "not started" even though the track fully ran.
        let author_status = match author.status.as_str() {
            "running" => StageStatus::Active,
            // "merged" strictly implies "done": the daemon's merge-reconcile
            // tick (coord/reconcile.py, #609/#1574) flips a completed
            // mock-author/test-author assignment straight to status="merged"
            // once its branch lands — it never passes back through "done" the
            // way a normal Work assignment does. Missing this arm was the
            // root cause of #1581 (a fully merged track rendering all four
            // stages "not started"). Deliberately mapped to the same
            // `StageStatus::Done` the Merge stage below already uses for a
            // `merge_queue` entry in state "merged" (no distinct "Merged"
            // variant exists on `quadraui::StageStatus` to render otherwise).
            "done" | "merged" => StageStatus::Done,
            // "cancelled" (an operator's explicit cancel of a running/pending
            // assignment) is terminal but unsuccessful — Failed keeps it
            // visually distinct from "never dispatched", same reasoning as
            // "merged" above: silently defaulting a terminal status to
            // Pending is exactly the bug class this issue fixed.
            "failed" | "cancelled" => StageStatus::Failed,
            // #2234: "refused_policy" (`coord.agent.REFUSED_POLICY`) is
            // drawn from `_ZERO_COMMIT_TYPES` = ("work", "mock-author",
            // "test-author") in coord/agent.py — unlike "advisory" below
            // (restricted to the narrower `_ADVISORY_TYPES` = ("work",)),
            // it CAN land on the mock-author/test-author rows this function
            // resolves: a JIT test-author track that correctly refuses a
            // CLAUDE.md-prohibited task exits 0-commit exactly like a plain
            // Work assignment would. Mapped to the same `StageStatus::Failed`
            // as "cancelled"/"failed" above (terminal-but-not-Done) — this
            // is the re-audit the "advisory" comment below asked for the
            // next time a new terminal status was added for these types;
            // without this arm, a track that fully ran and correctly
            // refused falls through to Pending and renders "not started",
            // reintroducing #1581 for this status.
            "refused_policy" => StageStatus::Failed,
            // "advisory" is restricted to `type="work"` assignments
            // (`_ADVISORY_TYPES` in coord/agent.py) and never appears on the
            // mock-author/test-author rows this function resolves, so it (and
            // "pending"/any future value) falls through to Pending here —
            // correct for "never dispatched", but re-audit this match if a
            // new terminal status is ever added for these assignment types.
            _ => StageStatus::Pending,
        };
        let author_since = match author_status {
            StageStatus::Active => author.dispatched_at,
            _ => None,
        };

        // Test: mirrors `test_stage_status_for`'s verdict mapping, but reads
        // `test_state` straight off the Author assignment itself (mock-
        // author/test-author carry their own Test-gate verdict — they don't
        // have a separate "work" assignment to key off).
        let (test_status, test_since) = if author_status != StageStatus::Done {
            (StageStatus::Pending, None)
        } else {
            match author.test_state.as_deref() {
                Some("passed") | Some("skipped") => (StageStatus::Done, None),
                Some("failed") => (StageStatus::Failed, None),
                _ => (StageStatus::Pending, author.finished_at.or(author.dispatched_at)),
            }
        };

        // Review: found via `review_of_assignment_id` linkage (NOT a generic
        // issue_number+type filter) — both Gate A's review and a per-issue
        // test-author's review are recorded under the milestone's *tracking*
        // issue number, so a plain issue_number match would conflate the
        // two tracks (and conflate sibling issues' JIT reviews with each
        // other) on a shared tracking issue.
        let review = self
            .data
            .assignments
            .iter()
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .filter(|a| a.review_of_assignment_id.as_deref() == Some(author.id.as_str()))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        let (review_status, review_since) = if test_status != StageStatus::Done {
            (StageStatus::Pending, None)
        } else {
            match review {
                None => (StageStatus::Pending, test_since.or(author.finished_at)),
                Some(r) if r.status == "running" => (StageStatus::Active, r.dispatched_at),
                Some(r) if r.status == "done" => match r.review_verdict.as_deref() {
                    Some("approve") => (StageStatus::Done, None),
                    _ => (StageStatus::Failed, None),
                },
                Some(r) if r.status == "failed" => (StageStatus::Failed, None),
                Some(_) => (StageStatus::Pending, None),
            }
        };

        // Merge: `merge_queue` dedups by branch (coord/merge_queue.py's
        // `enqueue` — "the merge queue dedups by branch we'd merge, not the
        // assignment_id"), so keying by the Author assignment's own branch
        // is the precise lookup — an issue_number+repo match (as
        // `merge_stage_status_for_local` uses for the normal Work track)
        // would ambiguously match BOTH Gate A's and test-author's queue
        // entries once both exist under the same tracking issue.
        let (merge_status, merge_since, merge_block_reason) = if review_status != StageStatus::Done {
            (StageStatus::Pending, None, None)
        } else if author.status == "merged" {
            // #1589: `coord reconcile-merges` prunes the `merge_queue` entry
            // once a branch lands (coord/reconcile.py) — the very entry the
            // lookup below depends on is deleted precisely when the merge
            // succeeds, so treating its absence as "not yet merged" is
            // guaranteed wrong for every merged track, not merely stale.
            // The author row's own `status="merged"` — set directly by the
            // merge-reconcile tick (#609/#1574) — is authoritative on its
            // own and needs no queue corroboration. A completed stage has
            // nothing left to wait for, so no `since` is attached (mirrors
            // the `Some("merged")` queue-entry arm below, and the
            // Author-stage `since: None` for Done from #1581). This mirrors
            // the precedent `merge_stage_status_for_local` already set for
            // the normal Work track (#775: a `status="merged"` assignment is
            // sufficient evidence even with no surviving queue entry) — that
            // fallback just never got extended to this prereq-pipeline path.
            (StageStatus::Done, None, None)
        } else {
            match author.branch.as_deref() {
                None => (
                    StageStatus::Pending,
                    review_since.or(review.and_then(|r| r.finished_at)),
                    None,
                ),
                Some(branch) => {
                    let entry = self
                        .data
                        .merge_queue
                        .iter()
                        .find(|m| m.repo_github == repo_slug && m.branch.as_deref() == Some(branch));
                    match entry.map(|e| e.state.as_str()) {
                        Some("merged") => (StageStatus::Done, None, None),
                        Some("open") | Some("queued") => (StageStatus::Active, None, None),
                        Some("failed") | Some("human_required") => (StageStatus::Failed, None, None),
                        _ => (
                            StageStatus::Pending,
                            review_since.or(review.and_then(|r| r.finished_at)),
                            // #2397: prefer the live-computed `merge_plan`
                            // (carries `reason` + `auto_drain`) over the raw
                            // `merge_queue` row matched above, which is only
                            // used here for its coarse `state`.
                            self.merge_plan_block_reason_for_branch(repo_slug, branch),
                        ),
                    }
                }
            }
        };

        PrereqPipelineStatus {
            author_id: Some(author.id.clone()),
            stages: [
                PrereqStage { status: author_status, since: author_since, block_reason: None },
                PrereqStage { status: test_status, since: test_since, block_reason: None },
                PrereqStage { status: review_status, since: review_since, block_reason: None },
                PrereqStage { status: merge_status, since: merge_since, block_reason: merge_block_reason },
            ],
        }
    }

    /// #1084: Gate A's Author→Test→Review→Merge status for the milestone
    /// whose tracking issue is `issue` (an epic row — Gate A runs once per
    /// milestone, keyed directly on the tracking issue's own
    /// `issue_number`, mirroring how `pipeline_pr_number` already keys the
    /// existing "View Gate A mock (PR)" menu item).
    pub(crate) fn gate_a_prereq_status(&self, issue: &PipelineIssue) -> PrereqPipelineStatus {
        let author = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| issue.coord_repo.as_deref().is_none_or(|r| a.repo == r))
            .filter(|a| a.assignment_type.as_deref() == Some("mock-author"))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        self.prereq_pipeline_status_from(&issue.repo_slug, author)
    }

    /// #1084: the per-issue Acceptance-Authoring status for `issue`'s slice
    /// of `tracking_issue`'s milestone acceptance suite. Resolves the
    /// specific `test-author` assignment extending *this* issue's slice via
    /// `for_issue_number` (set at dispatch by
    /// `coord.test_author.dispatch_test_author`); falls back to the latest
    /// test-author assignment recorded under the tracking issue for rows
    /// predating that correlation field, so older boards degrade to "some
    /// JIT authoring happened for this milestone" rather than showing
    /// nothing at all.
    pub(crate) fn acceptance_authoring_prereq_status(
        &self,
        issue: &PipelineIssue,
        tracking_issue: u64,
    ) -> PrereqPipelineStatus {
        let candidates: Vec<&Assignment> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == tracking_issue)
            .filter(|a| issue.coord_repo.as_deref().is_none_or(|r| a.repo == r))
            .filter(|a| a.assignment_type.as_deref() == Some("test-author"))
            .collect();
        let author = candidates
            .iter()
            .filter(|a| a.for_issue_number == Some(issue.number))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .or_else(|| {
                candidates
                    .iter()
                    .filter(|a| a.for_issue_number.is_none())
                    .max_by(|a, b| {
                        a.dispatched_at
                            .partial_cmp(&b.dispatched_at)
                            .unwrap_or(std::cmp::Ordering::Equal)
                    })
            })
            .copied();
        self.prereq_pipeline_status_from(&issue.repo_slug, author)
    }

    /// Compute the Work stage status without going through the dispatch in
    /// `stage_status_for` (which would special-case "test" too). Used by
    /// `test_stage_status_for` to decide whether Test is actionable yet.
    pub(crate) fn stage_status_for_internal_work(&self, issue: &PipelineIssue) -> StageStatus {
        // #550: prefer the server-computed projection when present — falls
        // back to the local computation below on the local-SQLite read path
        // (no daemon) or against a daemon older than #550.
        if let Some(s) = self.server_stage_status(issue, "work") {
            return s;
        }
        let matching = self.assignments_for_stage(issue, "work");
        if matching.iter().any(|a| a.status == "running") {
            return StageStatus::Active;
        }
        let latest = matching.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        if let Some(latest) = latest {
            match latest.status.as_str() {
                "done" => return StageStatus::Done,
                "failed" => return StageStatus::Failed,
                _ => {}
            }
        }
        if issue.is_closed {
            StageStatus::Skipped
        } else {
            StageStatus::Pending
        }
    }

    /// Returns the *display* current stage for the sidebar badge — the
    /// first non-Done/non-Skipped stage, or "done" once every meaningful
    /// stage is Done or Skipped.
    ///
    /// Skipped stages (closed-issue stages that never ran through coord) are
    /// treated the same as Done for badge purposes: they don't represent a
    /// meaningful "current" action and should not halt the badge at "work".
    ///
    /// The lifecycle section is used as the source of truth for the "done"
    /// state: if `pipeline_lifecycle_section` returns `"done"`, the badge
    /// always reads "done" regardless of individual stage statuses.  This
    /// prevents two categories of inconsistency:
    ///
    /// 1. **Open-but-merged issues** (`merge_stage_status_for == Done`, GitHub
    ///    issue still open): `stage_status_for` returns `Pending` (not
    ///    `Skipped`) for stages with no assignment because `is_closed` is
    ///    `false`.  Without the short-circuit, `derive_current_stage` would
    ///    halt at "review" or "work" and the sidebar badge would read "review"
    ///    or "work" for an issue that the coordinator has already merged.
    ///
    /// 2. **Stale downstream stages on closed issues**: if a work assignment
    ///    was re-dispatched after a review completed, the review stage becomes
    ///    `Stale` (not `Done` or `Skipped`), which would again halt the badge
    ///    at "review" even though the issue is closed.
    pub(crate) fn derive_current_stage(&self, issue: &PipelineIssue) -> String {
        // Lifecycle section is the authoritative summary.  If the issue is
        // already in "done", the badge must read "done" — never a bare stage
        // name that would be visually indistinguishable from a lifecycle label.
        if self.pipeline_lifecycle_section(issue) == "done" {
            return "done".to_string();
        }
        let stages = self.pipeline_stage_names();
        for s in &stages {
            let st = self.stage_status_for(issue, s);
            if st != StageStatus::Done && st != StageStatus::Skipped {
                return s.clone();
            }
        }
        "done".to_string()
    }

    /// Compute the default focused-stage index for the currently-selected
    /// pipeline issue.  Called whenever the selection changes so the
    /// Pipeline tab always opens on the most relevant content without
    /// requiring the user to click or press `[`/`]` first.
    ///
    /// - **In-progress** issues → first stage that is not Done or Skipped
    ///   (the "current" stage the user is waiting on).
    /// - **Done** issues (all stages Done/Skipped) → last stage index
    ///   (typically merge), so the user immediately sees the completion detail.
    /// - Returns `None` when no issue is selected or the issue has no stages.
    pub(crate) fn default_focused_stage_for_selected_issue(&self) -> Option<usize> {
        self.default_focused_stage_for(self.pipeline_sel?)
    }

    /// #2405: the same default-focused-stage rule for an *arbitrary*
    /// `pipeline_issues` index, so the Completed grid's row detail can pick a
    /// stage for an issue that is deliberately not the sidebar selection.
    pub(crate) fn default_focused_stage_for(&self, idx: usize) -> Option<usize> {
        let issue = self.pipeline_issues.get(idx)?;
        let stages = self.pipeline_stage_names_for_issue(issue);
        if stages.is_empty() {
            return None;
        }
        // First stage that is not yet finished — that's the "current" stage.
        for (i, name) in stages.iter().enumerate() {
            let status = self.stage_status_for(issue, name);
            if status != StageStatus::Done && status != StageStatus::Skipped {
                return Some(i);
            }
        }
        // All stages settled → point at the last one so the user sees the
        // final outcome (merge details, review verdict, etc.) immediately.
        Some(stages.len() - 1)
    }

    /// Build the quadraui `PipelineView` widget for the selected issue.
    ///
    /// The `[Go]` button is attached to the leftmost stage that is
    /// (a) Pending, (b) dispatchable from the TUI (work, review, merge),
    /// (c) preceded only by Done stages, and (d) for an issue we can map
    /// back to a coordinator-local repo.
    ///
    /// Returns `None` for closed issues that have zero assignment rows in the
    /// DB — these were resolved outside the coord pipeline, so showing an
    /// all-Skipped stage widget would be misleading. The caller will render
    /// the "closed without coord pipeline" placeholder instead.
    /// Move the Pipeline > Stages focus to the next stage (right).
    /// Wraps to the first stage from the last.  Resets the content
    /// scroll so the user starts at the top of the new content.
    pub(crate) fn focus_next_pipeline_stage(&mut self) {
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return;
        };
        let stages = self.pipeline_stage_names_for_issue(issue);
        if stages.is_empty() {
            return;
        }
        let next = match self.pipeline_focused_stage {
            None => 0,
            Some(i) => (i + 1) % stages.len(),
        };
        self.pipeline_focused_stage = Some(next);
        self.pipeline_stage_content_scroll = 0;
    }

    /// Move the Pipeline > Stages focus to the previous stage (left).
    /// Wraps to the last stage from the first.
    pub(crate) fn focus_prev_pipeline_stage(&mut self) {
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return;
        };
        let stages = self.pipeline_stage_names_for_issue(issue);
        if stages.is_empty() {
            return;
        }
        let prev = match self.pipeline_focused_stage {
            None => stages.len() - 1,
            Some(i) => (i + stages.len() - 1) % stages.len(),
        };
        self.pipeline_focused_stage = Some(prev);
        self.pipeline_stage_content_scroll = 0;
    }

    /// #264: True when the currently-open `inject_chat` overlay is bound to
    /// a `type="refinement"` assignment for a specific issue (rather than a
    /// board-level or worker-guidance session).  Refinement chats render
    /// inline in the Pipeline Refinement tab so tab-switching keeps working.
    ///
    /// #316: Board-level chats (`issue_number == 0`) are excluded here and
    /// handled separately by `chat_is_board_chat()`.
    pub(crate) fn chat_is_refinement(&self) -> bool {
        if self.inject_chat.is_none() {
            return false;
        }
        self.focused_watch_state()
            .map(|w| w.assignment_type == "refinement" && w.issue_number > 0)
            .unwrap_or(false)
    }

    /// #316: True when the currently-open `inject_chat` overlay is a
    /// board-level chat (`issue_number == 0`).  Board chats render inline in
    /// `BoardDetailTab::Chat` rather than a Pipeline Refinement tab.  Covers
    /// both `type="new-issue-chat"` and board `type="refinement"` sessions.
    ///
    /// #1017: `type="milestone-chat"` sessions also render here regardless of
    /// their tracking-issue number — they are plan/board-level conversations
    /// (the Plans-panel New/Open/Add-sub-issue chat entries), so the Board
    /// Chat tab is their natural home.
    pub(crate) fn chat_is_board_chat(&self) -> bool {
        if self.inject_chat.is_none() {
            return false;
        }
        self.focused_watch_state()
            .map(|w| w.issue_number == 0 || w.assignment_type == "milestone-chat")
            .unwrap_or(false)
    }

    /// #303: The single button rendered in the pipeline button bar above the
    /// stage row.  Returns `(label, stage_index)` for the stage that owns the
    /// action (today: at most one `[Go]` or `[Retry]` per pipeline), or
    /// `None` when no stage is dispatchable.
    ///
    /// Uses the same widget builder as `dispatch_pipeline_active_go` so the
    /// bar and the Enter keybind dispatch the exact same stage.
    pub(crate) fn pipeline_action_button(&self) -> Option<(String, usize)> {
        let view = self.build_pipeline_widget()?;
        view.stages
            .iter()
            .enumerate()
            .find_map(|(i, s)| s.action.as_ref().map(|label| (label.clone(), i)))
    }

    /// Build the [`Toolbar`] for the pipeline action bar (the `[ Go ⏎ ]` /
    /// `[ Retry ⏎ ]` strip above the stage boxes) when any stage is
    /// dispatchable.  Returns `None` when no stage is actionable.
    ///
    /// Extracted so the render path and the mouse-move hover-update path
    /// share one definition and can't drift apart.
    pub(crate) fn pipeline_action_bar_toolbar(&self) -> Option<Toolbar> {
        let (label, _stage_idx) = self.pipeline_action_button()?;
        Some(Toolbar {
            focused_index: None,
            id: WidgetId::new("pipeline-action-bar"),
            buttons: vec![ToolbarButton::Action {
                id: WidgetId::new("pipeline-action:dispatch"),
                label: label.clone(),
                icon: None,
                key_hint: Some("⏎".to_string()),
                enabled: true,
                is_active: false,
                tooltip: format!(
                    "Dispatch {} for the active stage (Enter)",
                    label.to_lowercase()
                ),
            }],
            bg: None,
        })
    }

    pub(crate) fn build_pipeline_widget(&self) -> Option<QuiPipelineView> {
        let idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(idx)?;

        // Closed with no assignment rows → suppress widget; placeholder message shown.
        if issue.is_closed && !self.issue_has_any_assignment(issue) {
            return None;
        }

        // #1199: an epic/tracking-issue row is gated by the milestone gate
        // lane (Gate A → B → C → D, docs/PIPELINE_V2.md), not the per-issue
        // Work → Test → Review → Merge lane it will never run — an epic
        // doesn't get worked, tested, reviewed or merged, it gets *gated*.
        // Route it to its own builder before any of the work-lane machinery
        // below (stage_names/statuses/Go-Retry attachment) ever runs.
        if labels_carry_epic_label(&issue.all_labels) {
            return Some(self.build_epic_gate_lane_widget(issue));
        }

        // Use the per-issue stage list so a plan-typed assignment
        // shows up as a Plan stage even when `pipeline_require_plan`
        // is false globally (the #262 right-click → Start with Plan path).
        let stage_names = self.pipeline_stage_names_for_issue(issue);

        // Precompute every stage's status once so we can check predecessors
        // without re-querying.
        let statuses: Vec<StageStatus> = stage_names
            .iter()
            .map(|name| self.stage_status_for(issue, name))
            .collect();

        let mut go_attached = false;
        let stages: Vec<QuiPipelineStage> = stage_names
            .iter()
            .enumerate()
            .map(|(i, name)| {
                let status = statuses[i].clone();
                let mut label = match name.as_str() {
                    "work" => "Work".to_string(),
                    other => {
                        let mut s = other.to_string();
                        if let Some(c) = s.get_mut(0..1) {
                            c.make_ascii_uppercase();
                        }
                        s
                    }
                };
                // #235: When Phase 1 is mid-build for this issue, swap the
                // Test label to "Building" so the Active badge has meaningful
                // text (otherwise it'd just say "Test" while running).
                if name == "test" && status == StageStatus::Active {
                    if let Some(work_id) = self
                        .assignments_for_stage(issue, "work")
                        .iter()
                        .max_by(|a, b| {
                            a.dispatched_at
                                .partial_cmp(&b.dispatched_at)
                                .unwrap_or(std::cmp::Ordering::Equal)
                        })
                        .map(|a| a.id.clone())
                    {
                        if self.test_build_in_flight(&work_id) {
                            label = "Building".to_string();
                        }
                    }
                }
                // Show turn count + elapsed on Active stage box.
                // Prefer live SSE turn count when watching, fall back to local log.
                // Elapsed is computed from dispatched_at to now (wall-clock).
                if status == StageStatus::Active {
                    let running = self
                        .assignments_for_stage(issue, name)
                        .into_iter()
                        .find(|a| a.status == "running");
                    if let Some(a) = running {
                        // Use cached SSE turn count from any pool entry for
                        // this assignment (not just the focused stream), so
                        // the badge updates even for background watches.
                        let turns = if let Some(ctx) = self.watch_pool.get(&a.id) {
                            if ctx.sse.current_turn > 0 {
                                ctx.sse.current_turn
                            } else {
                                self.turn_count_from_log(&a.id)
                            }
                        } else {
                            self.turn_count_from_log(&a.id)
                        };
                        if turns > 0 {
                            label = format!("{} T{}", label, turns);
                        }
                        // Elapsed since dispatch — second line in the box.
                        if let Some(dispatched_f) = a.dispatched_at {
                            let now_secs = std::time::SystemTime::now()
                                .duration_since(std::time::UNIX_EPOCH)
                                .map(|d| d.as_secs_f64())
                                .unwrap_or(dispatched_f);
                            let elapsed = (now_secs - dispatched_f).max(0.0) as u64;
                            label = format!("{}\n{}", label, fmt_elapsed(elapsed));
                        }
                    }
                }
                // #2397: surface the merge-queue's own computed block reason
                // on a Pending Merge box — previously this rendered as a bare
                // "Merge — pending" with zero signal for why, unlike the
                // Active branch above which at least shows elapsed time.
                // Self-guarding: `merge_plan_block_reason_for_issue` returns
                // `None` when no plan entry exists yet (no PR queued), which
                // is also true whenever "pending" just means "hasn't reached
                // Merge yet" rather than "blocked".
                if name == "merge" && status == StageStatus::Pending {
                    if let Some(reason) =
                        self.merge_plan_block_reason_for_issue(&issue.repo_slug, issue.number)
                    {
                        label = format!("{}\n{}", label, reason);
                    }
                }
                // #2427: annotate the box a dead-end `coord escalate record`
                // actually named — e.g. `coord drive` died with "review came
                // back request-changes and no loop dispatches a fix for this
                // row" (`coord.dead_end`'s terminal-and-unactionable shapes,
                // or `coord.drive._escalate_merge`) — but until now that
                // board record only surfaced as a sidebar-row "[stuck]"
                // badge (`escalation_badge_span`, a different splice point:
                // the Pipeline list, not this per-issue Overview strip) or
                // an operator-pulled `coord escalate list`. Matched on
                // `esc.stage == name` so only the box the driver actually
                // blamed gets the marker, not every stage on the row.
                if let Some(repo) = issue.coord_repo.as_deref() {
                    if let Some(esc) = self.escalation_for(repo, issue.number) {
                        if esc.stage == *name {
                            label = format!("{}\n⚠ blocked — see Log", label);
                        }
                    }
                }
                // Skipped counts as "settled" for prior_all_done: a closed-issue
                // stage that never ran is logically done.
                let prior_all_done = statuses[..i]
                    .iter()
                    .all(|s| *s == StageStatus::Done || *s == StageStatus::Skipped);
                let action = if !go_attached
                    && status == StageStatus::Pending
                    && prior_all_done
                    && is_dispatchable_stage(name)
                    && issue.coord_repo.is_some()
                {
                    go_attached = true;
                    Some("Go".to_string())
                } else if (status == StageStatus::Failed || status == StageStatus::Stale)
                    && prior_all_done
                    && is_dispatchable_stage(name)
                    && issue.coord_repo.is_some()
                {
                    // Failed → user-initiated retry of the failed dispatch.
                    // Stale → re-dispatch because an upstream stage was re-run.
                    // In both cases the user has to act; only show Retry when
                    // predecessors are settled so we don't race a running upstream.
                    Some("Retry".to_string())
                } else {
                    None
                };
                QuiPipelineStage {
                    label,
                    status,
                    action,
                }
            })
            .collect();

        // Clamp the persisted focus to the current stage count so a
        // smaller pipeline (e.g. issue without a Plan stage) doesn't
        // get a focus pointing past its last stage.
        let focused_stage = self.pipeline_focused_stage.filter(|&i| i < stages.len());
        Some(QuiPipelineView {
            id: WidgetId::new("pipeline:detail"),
            stages,
            focused_stage,
        })
    }

    /// #1199: build the epic row's own pipeline — the milestone gate lane
    /// (Gate A → B → C → D, docs/PIPELINE_V2.md) — rather than the per-issue
    /// Work → Test → Review → Merge lane, which is meaningless for a
    /// tracking issue (it is never itself worked/tested/reviewed/merged).
    ///
    /// **Gate A** reuses the existing #1084 `gate_a_prereq_status` machinery
    /// (the mock-author Author→Test→Review→Merge sub-pipeline), collapsed to
    /// a single stage status via `collapse_prereq_pipeline_status` — no
    /// recomputation. **Gates B/C/D have no board-readable status yet**: B
    /// (#933, post-milestone architecture review) and D (#934, the
    /// `develop` + feature-branch git model) are open in milestone #20 with
    /// no dispatch mechanism at all; C (`coord milestone gate-c`) exists but
    /// is a manual CLI check that persists nothing the board can read. All
    /// three render as a static "not yet available" Pending box rather than
    /// depending on B/D landing or fabricating a status for C.
    ///
    /// No stage carries a Go/Retry `action` — dispatching a gate from this
    /// row is out of scope for this presentation-only slice, and it also
    /// closes the latent hazard where the per-issue lane's "Go" on a
    /// Pending Work stage could otherwise be clicked on an epic row and
    /// dispatch normal work against the tracking issue.
    fn build_epic_gate_lane_widget(&self, issue: &PipelineIssue) -> QuiPipelineView {
        let gate_a_status = collapse_prereq_pipeline_status(&self.gate_a_prereq_status(issue));
        let stages: Vec<QuiPipelineStage> = [
            ("Gate A", gate_a_status),
            ("Gate B", StageStatus::Pending),
            ("Gate C", StageStatus::Pending),
            ("Gate D", StageStatus::Pending),
        ]
        .into_iter()
        .map(|(label, status)| QuiPipelineStage {
            label: label.to_string(),
            status,
            action: None,
        })
        .collect();
        let focused_stage = self.pipeline_focused_stage.filter(|&i| i < stages.len());
        QuiPipelineView {
            id: WidgetId::new("pipeline:detail"),
            stages,
            focused_stage,
        }
    }

    /// Pick the best machine to dispatch `coord_repo` work to.
    ///
    /// Prefers reachable machines that list `coord_repo` in their `repos`
    /// and have the fewest currently-running assignments.  Returns `None`
    /// when no reachable machine claims this repo.
    pub(crate) fn best_machine_for(&self, coord_repo: &str) -> Option<&Machine> {
        self.data
            .machines
            .iter()
            .filter(|m| m.reachable && m.repos.iter().any(|r| r == coord_repo))
            .filter(|m| !self.paused_machines.contains(&m.name))
            .min_by_key(|m| m.active_count)
    }

    /// #200: Find the latest Work assignment id for the currently-selected
    /// pipeline issue. Returns None if no issue is selected or no work assignment
    /// exists yet.
    pub(crate) fn pipeline_selected_work_id(&self) -> Option<String> {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })?;
        Some(latest.id.clone())
    }

    /// #200: Apply a Test gate verdict to the selected issue's latest Work
    /// assignment. Toasts the outcome. Returns true on success (a redraw is
    /// needed regardless).
    pub(crate) fn record_test_verdict(&mut self, verdict: &str, reason: Option<&str>) -> bool {
        let Some(work_id) = self.pipeline_selected_work_id() else {
            self.push_toast(
                "Test verdict skipped",
                "No work assignment to mark — dispatch Work first.",
                ToastSeverity::Error,
            );
            return false;
        };
        match record_test_verdict_remote(&work_id, verdict, reason) {
            Ok(()) => {
                let verb = match verdict {
                    "passed" => "PASSED",
                    "failed" => "FAILED",
                    "skipped" => "SKIPPED",
                    _ => verdict,
                };
                // #236: include the next-action hint so the user doesn't
                // have to read the status bar to find what to press.
                let suffix = match verdict {
                    "failed" => " — press R to re-dispatch Work",
                    "passed" | "skipped" => " — press R to dispatch review",
                    _ => "",
                };
                self.push_toast(
                    "Test gate",
                    &format!(
                        "Marked {} (work {}){}",
                        verb,
                        work_id.chars().take(8).collect::<String>(),
                        suffix,
                    ),
                    ToastSeverity::Info,
                );
                self.refresh();
                true
            }
            Err(e) => {
                self.push_toast(
                    "Test verdict failed",
                    &format!("{}", e),
                    ToastSeverity::Error,
                );
                false
            }
        }
    }

    /// #200: True when the selected issue's Test stage is pending and ready
    /// for a verdict (Work is Done, no verdict yet).
    pub(crate) fn test_gate_actionable(&self) -> bool {
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return false;
        };
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "test") {
            return false;
        }
        self.test_stage_status_for(issue) == StageStatus::Pending
            && self.stage_status_for_internal_work(issue) == StageStatus::Done
    }

    /// #349: True when the Pipeline view is active, an issue is selected, and
    /// the currently focused stage is "test".  Used to gate the 1–9 step-run
    /// keybindings so they don't fire on unrelated digit presses elsewhere.
    pub(crate) fn is_test_stage_focused(&self) -> bool {
        let Some(sel_idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(sel_idx) else {
            return false;
        };
        let stage_names = self.pipeline_stage_names_for_issue(issue);
        self.pipeline_focused_stage
            .and_then(|fi| stage_names.get(fi).map(|n| n.as_str() == "test"))
            .unwrap_or(false)
    }

    /// #349: Return the original step index of the first `kind: "pull"` step
    /// in the test plan for the currently-selected pipeline issue.  Returns
    /// `None` when no issue is selected, no plan is loaded, or the plan has
    /// no pull step.  Used by the `[a]` keybind handler to route pull steps
    /// through `run_test_plan_step` rather than the artifact-badge path.
    pub(crate) fn test_plan_pull_step_idx(&self) -> Option<usize> {
        let sel_idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(sel_idx)?;
        let local_repo = issue.coord_repo.as_deref();
        let work = self
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
            })?;
        work.test_plan
            .as_ref()?
            .iter()
            .position(|s| s.kind == "pull")
    }

    /// #349: Return the original step index of the `key_num`-th non-pull step
    /// (1-indexed) in the test plan for the currently-selected pipeline issue.
    ///
    /// Pull steps are assigned the `[a]` keybind and excluded from the
    /// 1–9 numbering.  Pressing `3` maps to the 3rd step whose kind is NOT
    /// `"pull"`, regardless of how many pull steps precede it.
    ///
    /// Returns `None` when no plan is loaded or `key_num` exceeds the count
    /// of non-pull steps.
    pub(crate) fn test_plan_runnable_step_idx(&self, key_num: usize) -> Option<usize> {
        let sel_idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(sel_idx)?;
        let local_repo = issue.coord_repo.as_deref();
        let work = self
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
            })?;
        let mut count = 0usize;
        for (i, step) in work.test_plan.as_ref()?.iter().enumerate() {
            if step.kind != "pull" {
                count += 1;
                if count == key_num {
                    return Some(i);
                }
            }
        }
        None
    }

    // ── #336 artifact helpers ────────────────────────────────────────────────

    /// #336: Resolve the artifact fetch target for the currently-selected
    /// pipeline issue.  Returns `(agent_host, repo, sanitized_branch, work_id)`
    /// when a work assignment with a known branch + machine is found, or `None`.
    pub(crate) fn artifact_fetch_target(&self) -> Option<(String, String, String, String)> {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })?;
        let branch = latest.branch.as_ref()?;
        let sanitized = sanitize_branch(branch);
        let host = self
            .data
            .machines
            .iter()
            .find(|m| m.name == latest.machine)
            .map(|m| m.host.clone())?;
        if host.is_empty() {
            return None;
        }
        Some((host, latest.repo.clone(), sanitized, latest.id.clone()))
    }

    /// #1337: the `(assignment_id, stored_len)` cache key of the selected
    /// pipeline issue's latest review, when its `review_findings` were
    /// bounded on the /board wire and no usable cache entry exists yet —
    /// i.e. the Review pane wants a full-findings detail fetch armed.
    /// `None` when local-mode (wire never truncates), nothing selected, the
    /// findings arrived whole, hydration already succeeded, or a recent
    /// failed attempt is still backing off.
    pub(crate) fn findings_fetch_target(&self) -> Option<(String, i64)> {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
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
            })?;
        if !review.review_findings_truncated {
            return None;
        }
        let len = review.review_findings_len?;
        let key = (review.id.clone(), len);
        match self.findings_detail_cache.get(&key) {
            // Hydrated — nothing to fetch.
            Some(e) if e.full.is_some() => None,
            // Failed recently — back off before re-arming.
            Some(e) if e.fetched_at.elapsed() < Duration::from_secs(30) => None,
            _ => Some(key),
        }
    }

    /// #2497: the `(repo, issue_number)` cache key of the issue currently
    /// shown on the Board Issue tab, the Pipeline Issue tab, or the Drive/
    /// Merge Queue tab's issue-detail pane when its body was bounded on the
    /// `/board` wire (`body_truncated`) and no usable cache entry exists yet
    /// — i.e. the pane wants a full-body detail fetch armed. Mirrors
    /// [`Self::findings_fetch_target`] (#1337) for `issues.body` instead of
    /// `assignments.review_findings`, keyed on `(repo, issue_number)` alone
    /// (unlike findings, an issue body has no per-fetch `len` to fold into
    /// the key — see `issue_detail_cache`'s doc comment).
    ///
    /// #1939: the Queue arm was added when `/board` started truncating
    /// **open** (not just closed) non-epic bodies too —
    /// `queue_issue_body_list`'s fast path (`drive_queue.rs`) reads the same
    /// `data.open_issues` row `board_issue_body_list` does, so it needs the
    /// same hydration arm or it can never recover the real text.
    ///
    /// `None` when none of the three panes is the active view/tab, nothing
    /// is selected, the body arrived whole, hydration already succeeded, or
    /// a recent failed attempt is still backing off.
    pub(crate) fn issue_body_fetch_target(&self) -> Option<(String, u64)> {
        let (repo, number, truncated, len) = match self.active_view {
            SidebarView::Board if self.board_detail_tab == BoardDetailTab::Issue => {
                let repo = self.board_detail_repo()?;
                let group = self.board_detail_issue_group()?;
                let oi = self
                    .data
                    .open_issues
                    .iter()
                    .find(|oi| oi.repo_name == repo && oi.number == group.issue_number)?;
                (
                    oi.repo_name.clone(),
                    oi.number,
                    oi.body_truncated,
                    oi.body_len,
                )
            }
            SidebarView::Pipeline if self.pipeline_detail_tab == PipelineDetailTab::Issue => {
                let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))?;
                let repo = issue.coord_repo.clone()?;
                (repo, issue.number, issue.body_truncated, issue.body_len)
            }
            SidebarView::MergeQueue => {
                let row = self.queue_selected_row()?;
                let number = u64::try_from(row.issue_number).ok()?;
                let oi = self
                    .data
                    .open_issues
                    .iter()
                    .find(|oi| oi.repo_name == row.repo_name && oi.number == number)?;
                (
                    oi.repo_name.clone(),
                    oi.number,
                    oi.body_truncated,
                    oi.body_len,
                )
            }
            _ => return None,
        };
        if !truncated {
            return None;
        }
        // Defensive: only arm once the wire has stamped a length alongside
        // the truncation flag (mirrors `findings_fetch_target`'s `?` on
        // `review_findings_len`) — an old daemon that somehow set the flag
        // without the length isn't a fetchable state.
        len?;
        let key = (repo, number);
        match self.issue_detail_cache.get(&key) {
            // Hydrated — nothing to fetch.
            Some(e) if e.full.is_some() => None,
            // Failed recently — back off before re-arming.
            Some(e) if e.fetched_at.elapsed() < Duration::from_secs(30) => None,
            _ => Some(key),
        }
    }

    /// #336: True when the selected pipeline issue has a non-empty artifact
    /// manifest in the 30-second TTL cache — i.e. the artifact badge is
    /// currently rendered and the `a` keybind should be live.
    pub(crate) fn artifact_badge_visible(&self) -> bool {
        let issue = match self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) {
            Some(i) => i,
            None => return false,
        };
        let work = self.assignments_for_stage(issue, "work");
        let latest = match work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        }) {
            Some(a) => a,
            None => return false,
        };
        let branch = match &latest.branch {
            Some(b) => b,
            None => return false,
        };
        let key = (latest.repo.clone(), sanitize_branch(branch));
        self.artifact_cache
            .get(&key)
            .and_then(|e| e.manifest.as_ref())
            .map(|m| !m.files.is_empty())
            .unwrap_or(false)
    }

    /// #532: Decide what pressing `a` on the selected pipeline row should do
    /// for the artifact flow.  Read-only — does not spawn commands or mutate
    /// state.  Production calls this from the `Key::Char('a')` arm and
    /// dispatches the returned action; tests call it directly to verify the
    /// routing matches each of the three cases (cached, badge, absence).
    ///
    /// Returns `None` when there's no work assignment in scope, no badge,
    /// no cached pull, and no recorded absence reason — i.e. the keybind
    /// would be a no-op.
    pub(crate) fn compute_a_key_artifact_action(&self) -> Option<AKeyArtifactAction> {
        let (_, repo, sanitized, work_id) = self.artifact_fetch_target()?;
        // 1. Existing pull result → re-open the dialog (highest priority so
        //    `a` doesn't kick off a redundant re-pull on a known result).
        if let Some(pull) = self.last_artifact_pulls.get(&work_id).cloned() {
            let dlg = if pull.exit_code == 0 {
                ArtifactPullDialog {
                    path: Some(pull.message.clone()),
                    body: format!("Saved to:\n{}", pull.message),
                }
            } else {
                ArtifactPullDialog {
                    path: None,
                    body: format!(
                        "Pull failed (exit {}):\n{}",
                        pull.exit_code, pull.message
                    ),
                }
            };
            return Some(AKeyArtifactAction::ReopenDialog(dlg));
        }
        // 2. Badge visible (manifest non-empty) → start a pull.
        if self.artifact_badge_visible() {
            return Some(AKeyArtifactAction::StartPull {
                work_id,
                repo,
                sanitized,
            });
        }
        // 3. No badge but a known absence reason → explain it.  An empty
        //    stash is the common, confusing case (#563/#569): distinguish a
        //    non-building CLI change (nothing to pull, ever) from a build that
        //    should have happened but didn't.
        let produces_artifact = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|iss| issue_produces_build_artifact(&repo, &iss.title))
            .unwrap_or(true);
        let cache_key = (repo, sanitized.clone());
        let body = self
            .artifact_cache
            .get(&cache_key)
            .and_then(|e| e.absence_reason.as_ref())
            .map(|absence| match absence {
                ArtifactAbsence::NotStashed | ArtifactAbsence::ManifestEmpty => {
                    artifact_absence_body(produces_artifact, &sanitized)
                }
                ArtifactAbsence::AgentUnreachable(e) => {
                    let msg: String = e.chars().take(200).collect();
                    format!("Agent unreachable:\n{}", msg)
                }
            })?;
        Some(AKeyArtifactAction::ShowAbsence(ArtifactPullDialog {
            path: None,
            body,
        }))
    }

    /// #236: True when the Test gate just passed (or was skipped) and the
    /// Review stage is Pending — the user can press `R` to dispatch the
    /// review immediately instead of waiting for the reconcile auto-dispatch.
    ///
    /// Drives the status-bar hint swap so the affordance is discoverable.
    /// The R keybind's existing `dispatch_pipeline_active_go` path already
    /// targets the Pending Review stage in this state; this predicate just
    /// surfaces it.
    pub(crate) fn can_dispatch_review_after_test_done(&self) -> bool {
        if self.active_view != SidebarView::Pipeline {
            return false;
        }
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return false;
        };
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "review") {
            return false;
        }
        // Test must be Done (passed/skipped) — Skipped (closed-issue path)
        // is not a "fresh pass" the user is acting on, so exclude it.
        let test_done = stages.iter().any(|s| s == "test")
            && self.test_stage_status_for(issue) == StageStatus::Done;
        if !test_done {
            return false;
        }
        // Review must be Pending and dispatchable (we have a coord_repo).
        let review_pending = self.stage_status_for(issue, "review") == StageStatus::Pending;
        review_pending && issue.coord_repo.is_some()
    }

    /// #236: True when the Test gate just failed and the user needs to
    /// bounce back to Work — fix the code, re-dispatch.
    ///
    /// The Pipeline widget's button-attachment logic does NOT attach a
    /// `[Retry]` to a Failed Test stage (test isn't dispatchable via the
    /// worker pipeline — it's a human gate), and Work shows as Done
    /// (the prior Work succeeded against the agent's own check; Test is
    /// what's failed).  So without this, the user has no in-TUI keybind
    /// to "send Work back for another iteration based on the test
    /// failure feedback".  When this returns true, the `R` keybind
    /// short-circuits and calls `dispatch_pipeline_work()` for a fresh
    /// Work attempt.
    pub(crate) fn can_bounce_work_after_test_fail(&self) -> bool {
        if self.active_view != SidebarView::Pipeline {
            return false;
        }
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return false;
        };
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "test") {
            return false;
        }
        // Test must be Failed and the failure must apply to a Done Work
        // (otherwise the user's next step isn't "re-dispatch Work").
        let test_failed = self.test_stage_status_for(issue) == StageStatus::Failed;
        let work_done = self.stage_status_for_internal_work(issue) == StageStatus::Done;
        test_failed && work_done && issue.coord_repo.is_some()
    }

    /// #235: True when a Phase 1 build for the given work assignment is
    /// currently running (entry present in `test_build_jobs`).  Removed when
    /// `poll_test_build_jobs` drains the completion message.
    pub(crate) fn test_build_in_flight(&self, work_id: &str) -> bool {
        self.test_build_jobs.contains_key(work_id)
    }

    /// #235: Gate for the `B` keybind.  True when:
    /// - the Pipeline view is active,
    /// - the Test gate is actionable (Work Done, no verdict yet),
    /// - the latest Work assignment has a branch recorded, and
    /// - no Phase 1 build is already in flight for this work id.
    ///
    /// We do NOT check `repo.build_command` here: `coord test` handles a
    /// missing build_command by just doing the checkout, which is still
    /// useful (the user gets the branch locally for manual inspection).
    pub(crate) fn can_trigger_test_build(&self) -> bool {
        if self.active_view != SidebarView::Pipeline {
            return false;
        }
        if !self.test_gate_actionable() {
            return false;
        }
        let Some(work_id) = self.pipeline_selected_work_id() else {
            return false;
        };
        if self.test_build_in_flight(&work_id) {
            return false;
        }
        let work = self.data.assignments.iter().find(|a| a.id == work_id);
        work.and_then(|a| a.branch.as_ref()).is_some()
    }

    /// #235 Phase 1 trigger: spawn `coord test <work_id>` on the local
    /// machine in a background thread, capturing combined stdout+stderr to
    /// `~/.coord/test-build-<work_id>.log`.  Returns `true` when a job was
    /// scheduled (a redraw is warranted for the new "Building" badge).
    ///
    /// Idempotent: a no-op when a build for `work_id` is already in flight.
    pub(crate) fn spawn_test_build(&mut self, work_id: String, branch: String, issue_number: u64) -> bool {
        if self.test_build_jobs.contains_key(&work_id) {
            return false;
        }
        let log_dir = coord_dir();
        if let Err(e) = std::fs::create_dir_all(&log_dir) {
            self.push_toast(
                "Build setup failed",
                &format!("create {} failed: {}", log_dir.display(), e),
                ToastSeverity::Error,
            );
            return false;
        }
        let log_path = log_dir.join(format!("test-build-{}.log", work_id));
        let cfg_path = self.command_runner.config_path.clone();

        let (tx, rx) = std::sync::mpsc::channel::<TestBuildOutcome>();
        let work_id_thread = work_id.clone();
        let log_path_thread = log_path.clone();
        std::thread::spawn(move || {
            use std::process::{Command, Stdio};
            let mut cmd = Command::new("coord");
            cmd.arg("test");
            if let Some(cfg) = &cfg_path {
                cmd.arg("--config").arg(cfg);
            }
            cmd.arg(&work_id_thread);
            // Belt-and-braces: even though main.rs sets GIT_TERMINAL_PROMPT=0
            // and BatchMode=yes, explicitly null-out stdin so no descendant
            // can grab the TUI's TTY for a prompt.  Combined with main.rs's
            // env vars this guarantees the build either succeeds against
            // ssh-agent-loaded keys or fails fast with a clear error.
            cmd.stdin(Stdio::null());
            let (exit_code, first_error) = match cmd.output() {
                Ok(out) => {
                    let mut buf = Vec::with_capacity(out.stdout.len() + out.stderr.len() + 64);
                    buf.extend_from_slice(b"--- stdout ---\n");
                    buf.extend_from_slice(&out.stdout);
                    buf.extend_from_slice(b"\n--- stderr ---\n");
                    buf.extend_from_slice(&out.stderr);
                    let _ = std::fs::write(&log_path_thread, buf);
                    let code = out.status.code().unwrap_or(-1);
                    // On failure, grab the first non-empty stderr line (or
                    // stdout if stderr is empty) for the toast.  Strip the
                    // "error: " prefix that `coord` prepends so the user
                    // sees the actual diagnostic.
                    let first = if code != 0 {
                        let pick = |bytes: &[u8]| -> Option<String> {
                            String::from_utf8_lossy(bytes)
                                .lines()
                                .map(|l| l.trim())
                                .find(|l| !l.is_empty())
                                .map(|l| l.trim_start_matches("error: ").to_string())
                        };
                        pick(&out.stderr)
                            .or_else(|| pick(&out.stdout))
                            .unwrap_or_default()
                    } else {
                        String::new()
                    };
                    (code, first)
                }
                Err(e) => {
                    let msg = format!("failed to spawn coord test: {}", e);
                    let _ = std::fs::write(&log_path_thread, format!("{}\n", msg));
                    (-1, msg)
                }
            };
            let _ = tx.send(TestBuildOutcome {
                exit_code,
                first_error,
            });
        });

        self.push_toast(
            "Phase 1 build started",
            &format!("#{} on {} — fetching and building…", issue_number, branch),
            ToastSeverity::Info,
        );
        self.test_build_jobs.insert(
            work_id.clone(),
            TestBuildJob {
                work_id,
                issue_number,
                branch,
                log_path,
                started_at: Instant::now(),
                rx,
            },
        );
        true
    }

    /// #235: Drain completed Phase 1 build jobs, toast their outcome, and
    /// remove them from the in-flight map.  Returns `true` when at least
    /// one job finished (a redraw is needed so the "Building" badge clears).
    pub(crate) fn poll_test_build_jobs(&mut self) -> bool {
        if self.test_build_jobs.is_empty() {
            return false;
        }
        use std::sync::mpsc::TryRecvError;
        let mut done: Vec<(String, Result<TestBuildOutcome, ()>)> = Vec::new();
        for (id, job) in self.test_build_jobs.iter() {
            match job.rx.try_recv() {
                Ok(outcome) => done.push((id.clone(), Ok(outcome))),
                Err(TryRecvError::Disconnected) => done.push((id.clone(), Err(()))),
                Err(TryRecvError::Empty) => {}
            }
        }
        if done.is_empty() {
            return false;
        }
        for (id, result) in done {
            let job = self.test_build_jobs.remove(&id).expect("just observed");
            let dur_secs = job.started_at.elapsed().as_secs();
            // #271 part 2: persist the outcome so the Pipeline detail
            // panel can show "Last build: …" long after the toast
            // expires.  Pull values out before move-into-toast paths.
            let persist_exit;
            let persist_first_error;
            match &result {
                Ok(o) => {
                    persist_exit = o.exit_code;
                    persist_first_error = o.first_error.clone();
                }
                Err(()) => {
                    persist_exit = -1;
                    persist_first_error = "build worker disappeared".to_string();
                }
            }
            self.last_test_builds.insert(
                id.clone(),
                TestBuildResult {
                    branch: job.branch.clone(),
                    issue_number: job.issue_number,
                    exit_code: persist_exit,
                    first_error: persist_first_error,
                    log_path: job.log_path.clone(),
                    duration_secs: dur_secs,
                    finished_at: Instant::now(),
                },
            );
            match result {
                Ok(TestBuildOutcome { exit_code: 0, .. }) => {
                    self.push_toast(
                        "Phase 1 build ✓",
                        &format!(
                            "#{} ready to test on {} ({}s) — press P / F / S",
                            job.issue_number, job.branch, dur_secs
                        ),
                        ToastSeverity::Info,
                    );
                }
                Ok(TestBuildOutcome {
                    exit_code: code,
                    first_error,
                }) => {
                    // Truncate the error to ~120 chars so the toast stays
                    // readable; the full log is at log_path for details.
                    let snippet = if first_error.is_empty() {
                        format!("see {}", job.log_path.display())
                    } else {
                        let trimmed: String = first_error.chars().take(120).collect();
                        if first_error.chars().count() > 120 {
                            format!("{}… (see {})", trimmed, job.log_path.display())
                        } else {
                            format!("{} (see {})", trimmed, job.log_path.display())
                        }
                    };
                    self.push_toast(
                        "Phase 1 build ✗",
                        &format!("#{} exit {}: {}", job.issue_number, code, snippet),
                        ToastSeverity::Error,
                    );
                }
                Err(()) => {
                    self.push_toast(
                        "Phase 1 build ✗",
                        &format!(
                            "#{} build worker disappeared — see {}",
                            job.issue_number,
                            job.log_path.display()
                        ),
                        ToastSeverity::Error,
                    );
                }
            }
        }
        true
    }

    // ── #349: Test-plan lifecycle ─────────────────────────────────────────────

    /// #349: Spawn `coord test-plan [--refresh] <work_id>` via CommandRunner
    /// when the test stage is focused and no plan is cached (or it is stale).
    ///
    /// Called from `run_periodic_work` each tick.  Idempotent: will not spawn
    /// a second time while the first is still in-flight (CommandRunner is
    /// single-slot and the work_id will be in `test_plan_pending`).
    pub(crate) fn maybe_spawn_test_plan(&mut self) {
        // Only act when the Pipeline view is active and a test stage is focused.
        if self.active_view != SidebarView::Pipeline {
            return;
        }
        let Some(sel_idx) = self.pipeline_sel else {
            return;
        };
        let Some(issue) = self.pipeline_issues.get(sel_idx).cloned() else {
            return;
        };

        // Determine which stage is focused and whether it's the "test" stage.
        let stage_names = self.pipeline_stage_names_for_issue(&issue);
        let focused_is_test = self
            .pipeline_focused_stage
            .and_then(|fi| stage_names.get(fi).map(|n| n.as_str() == "test"))
            .unwrap_or(false);
        if !focused_is_test {
            return;
        }

        // Find the latest work assignment for this issue.
        let local_repo = issue.coord_repo.as_deref();
        let work = self
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
            })
            .cloned();
        let Some(work) = work else {
            return;
        };
        let work_id = work.id.clone();

        // ── Staleness check ────────────────────────────────────────────────
        // Run once per work_id (tracked by test_plan_staleness_checked_for).
        if self.test_plan_staleness_checked_for.as_deref() != Some(&work_id) {
            self.test_plan_staleness_checked_for = Some(work_id.clone());
            // Only check staleness when a plan AND a branch_head exist.
            if let (Some(branch), Some(cached_head)) = (&work.branch, &work.test_plan_branch_head) {
                if let Some(local_path) = issue
                    .coord_repo
                    .as_ref()
                    .and_then(|r| self.data.pipeline_repo_paths.get(r.as_str()))
                {
                    let repo_dir = std::path::Path::new(local_path.as_str());
                    if let Some(live_head) = read_git_branch_head(repo_dir, branch) {
                        if &live_head != cached_head {
                            // Branch advanced — refresh the plan.
                            self.test_plan_pending.insert(work_id.clone());
                            self.command_runner
                                .spawn_queued(&["test-plan", &work_id, "--refresh"]);
                            return;
                        }
                    }
                }
            }
        }

        // ── Spawn when plan is missing ─────────────────────────────────────
        if work.test_plan.is_none() && !self.test_plan_pending.contains(&work_id) {
            self.test_plan_pending.insert(work_id.clone());
            self.command_runner.spawn_queued(&["test-plan", &work_id]);
        }
    }

    /// #349: Run the test-plan step at *step_idx* for the selected pipeline
    /// issue's latest work assignment.  Spawns a background thread that
    /// executes the step's shell command and sends the exit code back via an
    /// mpsc channel, which is polled by `poll_test_step_jobs`.
    ///
    /// No-ops when:
    /// - No work assignment or test plan is available.
    /// - `step_idx` is out of range for the plan.
    /// - A job for `(work_id, step_idx)` is already in flight.
    pub(crate) fn run_test_plan_step(&mut self, step_idx: usize) {
        let Some(sel_idx) = self.pipeline_sel else {
            return;
        };
        let Some(issue) = self.pipeline_issues.get(sel_idx).cloned() else {
            return;
        };
        let local_repo = issue.coord_repo.as_deref();
        let work = self
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
            })
            .cloned();
        let Some(work) = work else {
            return;
        };
        let Some(ref steps) = work.test_plan else {
            return;
        };
        let Some(step) = steps.get(step_idx) else {
            return;
        };
        let work_id = work.id.clone();
        let key = (work_id.clone(), step_idx);

        // Don't re-run an already in-flight step.
        if self.test_step_jobs.contains_key(&key) {
            return;
        }

        // "verify" steps have no command — just mark checked (exit 0).
        if step.kind == "verify" {
            self.test_step_results.insert(key, 0);
            return;
        }

        let Some(ref cmd_str) = step.cmd else {
            // No command to run — mark as checked.
            self.test_step_results.insert(key, 0);
            return;
        };

        let cmd_owned = cmd_str.clone();
        let (tx, rx) = std::sync::mpsc::channel::<(i32, String)>();
        std::thread::spawn(move || {
            use std::process::{Command, Stdio};
            // Capture both stdout and stderr so the output can be displayed
            // inline in the test stage panel.  Bounded to 64 KiB combined to
            // prevent unbounded memory growth from verbose commands.
            const MAX_OUTPUT: usize = 64 * 1024;
            let result = Command::new("sh")
                .arg("-c")
                .arg(&cmd_owned)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .output();
            let (exit_code, output_str) = match result {
                Ok(out) => {
                    let code = out.status.code().unwrap_or(-1);
                    let mut combined = String::new();
                    // Stdout first, then stderr, with a separator when both are non-empty.
                    let stdout = String::from_utf8_lossy(&out.stdout);
                    let stderr = String::from_utf8_lossy(&out.stderr);
                    if !stdout.is_empty() {
                        combined.push_str(&stdout);
                    }
                    if !stderr.is_empty() {
                        if !combined.is_empty() {
                            combined.push_str("\n── stderr ──\n");
                        }
                        combined.push_str(&stderr);
                    }
                    // Truncate to MAX_OUTPUT bytes (safe: truncate at char boundary).
                    if combined.len() > MAX_OUTPUT {
                        let truncated: String = combined
                            .chars()
                            .take(
                                combined
                                    .char_indices()
                                    .take_while(|(b, _)| *b < MAX_OUTPUT)
                                    .count(),
                            )
                            .collect();
                        combined = truncated;
                        combined.push_str("\n… (output truncated)");
                    }
                    (code, combined)
                }
                Err(e) => (-1, format!("failed to spawn: {e}")),
            };
            let _ = tx.send((exit_code, output_str));
        });

        self.test_step_jobs.insert(
            key,
            TestStepJob {
                work_id,
                step_idx,
                rx,
            },
        );
        self.push_toast(
            &format!("Step {}", step_idx + 1),
            &format!("Running: {}", cmd_str.chars().take(80).collect::<String>()),
            ToastSeverity::Info,
        );
    }

    /// #349: Drain completed test-plan step jobs.  Returns `true` when at
    /// least one job finished (the panel needs to be redrawn).
    pub(crate) fn poll_test_step_jobs(&mut self) -> bool {
        if self.test_step_jobs.is_empty() {
            return false;
        }
        use std::sync::mpsc::TryRecvError;
        let mut done: Vec<(String, usize, i32, String)> = Vec::new();
        for (key, job) in self.test_step_jobs.iter() {
            match job.rx.try_recv() {
                Ok((exit_code, output)) => done.push((key.0.clone(), key.1, exit_code, output)),
                Err(TryRecvError::Disconnected) => {
                    done.push((key.0.clone(), key.1, -1, String::new()))
                }
                Err(TryRecvError::Empty) => {}
            }
        }
        if done.is_empty() {
            return false;
        }
        for (work_id, step_idx, exit_code, output) in done {
            self.test_step_jobs.remove(&(work_id.clone(), step_idx));
            self.test_step_results
                .insert((work_id.clone(), step_idx), exit_code);
            if !output.is_empty() {
                self.test_step_output.insert((work_id, step_idx), output);
            }
        }
        true
    }

    /// Dispatch the action button (`[Go]` or `[Retry]`) attached to a
    /// specific stage index. Branches on the stage's current status: a
    /// Failed stage gets retry semantics; everything else gets fresh
    /// dispatch.  Falls back to a status message for stages we don't
    /// know how to dispatch.
    pub(crate) fn dispatch_pipeline_stage(&mut self, stage_idx: usize) -> bool {
        // Per-issue stages must match what `build_pipeline_widget`
        // rendered — otherwise a click on the Plan stage (visible only
        // when the issue has a plan assignment) would resolve to the
        // wrong stage_name.
        let Some(sel) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(sel).cloned() else {
            return false;
        };
        let stage_name = match self
            .pipeline_stage_names_for_issue(&issue)
            .get(stage_idx)
            .cloned()
        {
            Some(s) => s,
            None => return false,
        };
        // Failed → retry the failed assignment (re-dispatch via `coord retry`).
        // Stale → fall through to fresh dispatch: the previous attempt SUCCEEDED
        //         against an older revision, so there is no failed-row to retry;
        //         we want a brand-new assignment against the new upstream.
        //         (Routing Stale into the retry path produces a misleading
        //         "no failed assignment found" error.)
        let stage_status = self.stage_status_for(&issue, &stage_name);
        let is_retry = stage_status == StageStatus::Failed;
        match stage_name.as_str() {
            "plan" => {
                if is_retry {
                    self.retry_pipeline_assignment(&issue, "plan")
                } else {
                    self.dispatch_pipeline_plan()
                }
            }
            "work" => {
                if is_retry {
                    self.retry_pipeline_assignment(&issue, "work")
                } else {
                    self.dispatch_pipeline_work()
                }
            }
            "review" => {
                if is_retry {
                    self.retry_pipeline_assignment(&issue, "review")
                } else {
                    self.dispatch_pipeline_review()
                }
            }
            // #2402: a blocked (or not-yet-started) Merge stage's [Go] now
            // means "re-evaluate now" instead of the "stage 'merge' not
            // dispatchable from TUI" no-op this used to fall into (`merge`
            // was excluded from `is_dispatchable_stage` per #738 — this
            // reverses only the per-issue Go affordance, not #738's
            // reasoning that merge is otherwise driven from the Merge Queue
            // panel). Reuses `dispatch_merge_automated_for_selected_pipeline_
            // issue` (the same `coord merge --order <aid>` the "Start
            // (automated) > Merge" menu item already runs) rather than a
            // second command path: whether the entry has never been queued
            // or is sitting BLOCKED on a stale gate, a fresh `--order` re-run
            // re-evaluates every gate against the current board/CI state.
            // `is_retry` is deliberately not consulted here — there is only
            // one merge command to (re)run, not a distinct retry variant.
            "merge" => self.dispatch_merge_automated_for_selected_pipeline_issue(),
            other => {
                self.pipeline_status = Some((
                    format!("stage '{}' not dispatchable from TUI", other),
                    Instant::now(),
                ));
                false
            }
        }
    }

    /// Dispatch the action button on whichever stage currently owns it.
    /// Used by the Enter key handler where we don't have a stage index
    /// from a click — falls through to the first stage with an attached
    /// `[Go]` or `[Retry]`.
    pub(crate) fn dispatch_pipeline_active_go(&mut self) -> bool {
        let widget = match self.build_pipeline_widget() {
            Some(w) => w,
            None => return false,
        };
        let stage_idx = widget.stages.iter().position(|s| s.action.is_some());
        match stage_idx {
            Some(i) => self.dispatch_pipeline_stage(i),
            None => false,
        }
    }

    /// Retry the latest failed `<stage>` assignment for `issue` via
    /// `coord retry <assignment_id>`.  No-op with a status message when
    /// no matching failed assignment is found.
    pub(crate) fn retry_pipeline_assignment(&mut self, issue: &PipelineIssue, stage: &str) -> bool {
        let Some(coord_repo) = issue.coord_repo.clone() else {
            self.pipeline_status = Some((
                format!(
                    "no local repo mapping for {} — add it to coordinator.yml",
                    issue.repo_slug
                ),
                Instant::now(),
            ));
            return false;
        };
        let assignment_id = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number && a.repo == coord_repo)
            .filter(|a| {
                let t = a.assignment_type.as_deref().unwrap_or("work");
                if stage == "work" && !self.issue_has_plan_assignment(issue) {
                    // Same legacy fold as `assignments_for_stage` — only
                    // applies when the per-issue strip has no Plan stage.
                    t == "work" || t == "plan"
                } else {
                    t == stage
                }
            })
            .find(|a| a.status == "failed")
            .map(|a| a.id.clone());
        let Some(id) = assignment_id else {
            self.pipeline_status = Some((
                format!("no failed {} assignment found for #{}", stage, issue.number),
                Instant::now(),
            ));
            return false;
        };
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["retry", &id]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("retry dispatched for {} #{}", stage, issue.number),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "retry runs after current command",
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

    /// Dispatch the Work stage.
    ///
    /// If a completed Plan assignment exists for this issue, runs
    /// `coord approve-plan <plan_id>` — which uses the plan output as the
    /// briefing for the new work assignment.  Otherwise falls back to a
    /// fresh `coord assign <machine> <repo> <issue>`.
    ///
    /// #685: always arms the test-mode choice dialog first; the dialog's
    /// confirm path calls `dispatch_pipeline_work_with_mode` to do the
    /// actual dispatch after `coord set-test-mode` runs.
    pub(crate) fn dispatch_pipeline_work(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            self.pipeline_status = Some((
                format!(
                    "no local repo mapping for {} — add it to coordinator.yml",
                    issue.repo_slug
                ),
                Instant::now(),
            ));
            return false;
        };

        // For the approve-plan path we still need a machine to be reachable.
        let machine_name = if self.find_done_plan_assignment_id(&issue, &coord_repo).is_none() {
            let Some(machine) = self.best_machine_for(&coord_repo) else {
                self.pipeline_status = Some((
                    format!(
                        "no reachable machine for {} — queued (issue #{})",
                        coord_repo, issue.number
                    ),
                    Instant::now(),
                ));
                return false;
            };
            Some(machine.name.clone())
        } else {
            None // approve-plan resolves its own machine
        };

        // Inject session-level model override when the user configured one for
        // this machine in Settings → Dispatch → Per-Machine Model Overrides.
        let model_str = machine_name.as_ref().and_then(|mn| {
            self.settings
                .machine_model
                .get(mn)
                .map(|p| p.as_str().to_string())
        });

        // Read the current test-mode label (if any) to pre-select the default.
        let current_mode = issue
            .all_labels
            .iter()
            .find(|l| l.starts_with("test-mode:"))
            .map(|l| l.trim_start_matches("test-mode:").to_string());

        // #685: arm the test-mode choice dialog.  The dialog's confirm path
        // will call `dispatch_pipeline_work_with_mode` with the chosen mode.
        self.pending_test_mode_choice = Some(PendingTestModeChoice {
            coord_repo,
            issue_num: issue.number,
            action: TestModeChoiceAction::DispatchWork,
            current_mode,
            machine_name,
            model_override: model_str,
        });
        true
    }

    /// #685: called by the test-mode dialog confirm path.  Dispatches the
    /// headless Work assignment after `coord set-test-mode` has run.
    pub(crate) fn dispatch_pipeline_work_with_mode(
        &mut self,
        coord_repo: &str,
        issue_num: u64,
        machine_name: Option<&str>,
        model_override: Option<&str>,
    ) -> bool {
        use crate::commands::SpawnQueuedOutcome;

        // Resolve the issue from pipeline_issues to check for a done plan.
        let issue_clone = self
            .pipeline_issues
            .iter()
            .find(|i| i.number == issue_num && i.coord_repo.as_deref() == Some(coord_repo))
            .cloned();

        // If a done plan exists for this issue, approve it.
        if let Some(ref issue) = issue_clone {
            if let Some(plan_id) = self.find_done_plan_assignment_id(issue, coord_repo) {
                let outcome = self.command_runner.spawn_queued(&["approve-plan", &plan_id]);
                match outcome {
                    SpawnQueuedOutcome::Started => {
                        self.pipeline_status = Some((
                            format!(
                                "approving plan {} → dispatching work for #{}",
                                &plan_id[..plan_id.len().min(8)],
                                issue_num
                            ),
                            Instant::now(),
                        ));
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
                return matches!(
                    outcome,
                    SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
                );
            }
        }

        let Some(mn) = machine_name else {
            self.pipeline_status = Some((
                format!("no machine for {} (issue #{})", coord_repo, issue_num),
                Instant::now(),
            ));
            return false;
        };

        let issue_str = issue_num.to_string();
        let mut cmd: Vec<String> =
            vec!["assign".into(), mn.to_string(), coord_repo.to_string(), issue_str];
        if let Some(m) = model_override {
            cmd.push("--model".into());
            cmd.push(m.to_string());
        }
        let cmd_refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
        let outcome = self.command_runner.spawn_queued(&cmd_refs);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("dispatched #{} → {}", issue_num, mn),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "assign runs after current command",
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

    /// Dispatch the Plan stage: `coord assign --plan-only <machine> <repo> <issue>`.
    pub(crate) fn dispatch_pipeline_plan(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            self.pipeline_status = Some((
                format!(
                    "no local repo mapping for {} — add it to coordinator.yml",
                    issue.repo_slug
                ),
                Instant::now(),
            ));
            return false;
        };
        let Some(machine) = self.best_machine_for(&coord_repo) else {
            self.pipeline_status = Some((
                format!(
                    "no reachable machine for {} — queued (issue #{})",
                    coord_repo, issue.number
                ),
                Instant::now(),
            ));
            return false;
        };
        let machine_name = machine.name.clone();
        let issue_str = issue.number.to_string();
        // Inject session-level model override when configured for this machine.
        let model_str = self
            .settings
            .machine_model
            .get(&machine_name)
            .map(|p| p.as_str().to_string());
        let mut cmd: Vec<String> = vec![
            "assign".into(),
            "--plan-only".into(),
            machine_name.clone(),
            coord_repo.clone(),
            issue_str.clone(),
        ];
        if let Some(ref m) = model_str {
            cmd.push("--model".into());
            cmd.push(m.clone());
        }
        let cmd_refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&cmd_refs);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("plan dispatched for #{} → {}", issue.number, machine_name),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "plan assign runs after current command",
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

    /// Find the most recent done plan-typed assignment for this issue.
    /// Used by Work [Go] to decide between `approve-plan` and a fresh
    /// `coord assign`.
    pub(crate) fn find_done_plan_assignment_id(
        &self,
        issue: &PipelineIssue,
        coord_repo: &str,
    ) -> Option<String> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number && a.repo == coord_repo)
            .filter(|a| a.assignment_type.as_deref() == Some("plan"))
            .find(|a| a.status == "done")
            .map(|a| a.id.clone())
    }

    /// Dispatch the Review stage: `coord notify`.
    ///
    /// `coord notify` polls every machine for completion and auto-dispatches
    /// a review when a worker has finished — this is a session-wide poll,
    /// not scoped to a single issue, but in practice it's the right call:
    /// the worker we want a review for has just finished, and notify is
    /// idempotent for already-reviewed work.
    pub(crate) fn dispatch_pipeline_review(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["notify"]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("notify dispatched for #{}", issue.number),
                    Instant::now(),
                ));
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
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// #486: refresh the Pipeline's data source.
    ///
    /// The Pipeline now reads from the local DB cache (`data.open_issues`,
    /// rebuilt into `pipeline_issues` in `apply_pending_data`), the SAME source
    /// the Board uses.  So "refresh" means: kick a background `coord sync` (gh
    /// `issue list` — core API, 5000/hr, consistent; throttled) to update the
    /// cache, then reload `data` from the DB so the rebuilt pipeline reflects
    /// it.  No live `gh search` (the old 30/min, eventually-consistent, several-
    /// seconds path) — the pipeline now renders instantly from already-loaded
    /// data and never silently goes stale on a Search-API rate-limit.
    pub(crate) fn maybe_kick_pipeline_loader(&mut self) {
        self.kick_issue_sync();
        self.refresh();
    }

    /// #1344: rebuild `pipeline_ci_checks` from the just-applied `/board`
    /// payload's `merge_plan[].ci_summary` — the board daemon computes this
    /// once per tick from its tick-refreshed gate snapshot (#1336,
    /// `coord/gate_snapshot.py`) and ships it on the wire, so the TUI no
    /// longer needs its own perpetual per-PR `gh pr checks` poll loop
    /// (the old `maybe_kick_ci_check_loaders`/`poll_ci_check_loaders`
    /// machinery this replaces — sustained high CPU across the fleet, one
    /// `gh` subprocess loop per running `coord-tui` instance).
    ///
    /// Called from `apply_pending_data` on every successful board refresh.
    /// Returns `true` when at least one summary changed (caller redraws).
    pub(crate) fn sync_ci_checks_from_board(&mut self) -> bool {
        // Snapshot into owned data first — `self.push_toast` below needs
        // `&mut self`, which can't coexist with a live borrow of
        // `self.data.merge_plan`.
        let updates: Vec<((String, i64), CiCheckSummary)> = self
            .data
            .merge_plan
            .iter()
            .filter_map(|pm| {
                let pr = pm.pr_number?;
                let ci = pm.ci_summary.as_ref()?;
                Some((
                    (pm.repo_github.clone(), pr),
                    CiCheckSummary {
                        passed: ci.passed,
                        failed: ci.failed,
                        running: ci.running,
                        failed_names: ci.failed_names.clone(),
                        first_failed_url: ci.first_failed_url.clone(),
                    },
                ))
            })
            .collect();

        let mut changed = false;
        for (key, summary) in updates {
            // Toast only on the *transition* into failure: the new summary
            // has failures and either there was no prior summary or the
            // prior summary was clean.  Without the transition guard we'd
            // re-toast on every tick while CI stays red.
            let prev_failed = self
                .pipeline_ci_checks
                .get(&key)
                .is_some_and(|s| s.has_failures());
            if summary.has_failures() && !prev_failed {
                let (repo_github, pr) = &key;
                let names = summary.failed_names.join(", ");
                let body = if names.is_empty() {
                    format!("{repo_github} #{pr}")
                } else {
                    format!("{repo_github} #{pr} — {names}")
                };
                self.push_toast("⚠ CI failed", &body, ToastSeverity::Warning);
            }
            self.pipeline_ci_checks.insert(key, summary);
            changed = true;
        }
        changed
    }

    /// #253: True when the currently-selected pipeline issue has a queued
    /// merge entry whose work assignment lacks an approved review on the
    /// board.  Drives the status-bar swap so the user sees the block before
    /// pressing `m`.
    ///
    /// Mirrors the Python `requires_review` + `has_approved_review` logic
    /// (coord/merge_queue.py) on the data the TUI loads from the local DB.
    /// Returns false when reviews aren't configured (no review-typed
    /// assignment ever appeared for this issue) so the hint doesn't appear
    /// in projects that don't use the review pipeline.
    pub(crate) fn merge_blocked_on_review_for_selected_issue(&self) -> bool {
        if self.active_view != SidebarView::Pipeline {
            return false;
        }
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return false;
        };
        // Need a queued merge entry that is not yet merged.
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
        let Some(entry) = entry else {
            return false;
        };
        if entry.state == "merged" {
            return false;
        }
        // Only consider issues whose pipeline includes a "review" stage —
        // a project without review gates shouldn't see this hint.
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "review") {
            return false;
        }
        // #292 (Defect 1/2): mirror coord/merge_queue.py::has_approved_review.
        // After a review bounce the entry may be keyed to the *original* work
        // assignment while the approved re-review is linked to the *fix* work
        // assignment.  Collect ALL work IDs for this issue and accept an
        // approval against any of them.  Seed with entry.assignment_id so
        // reviews are found even when the work row is pruned from the DB.
        let approved = self.issue_has_any_approved_review(issue, Some(&entry.assignment_id));
        !approved
    }

    /// Returns true when any review assignment for *issue* carries
    /// `review_verdict="approve"`.
    ///
    /// #292: used by `merge_blocked_on_review_for_selected_issue` and
    /// `pipeline_merge_state` to accept an approval on a *fix* work
    /// assignment even when the merge entry is still keyed to the original
    /// work assignment.  Mirrors `coord/merge_queue.py::has_approved_review`.
    ///
    /// *seed_work_id*: the queue entry's `assignment_id`, included in the
    /// set of candidate work IDs even when the corresponding assignment row
    /// has been pruned from `data.assignments` (old rows GC'd from the local
    /// DB).  Pass `None` when no queue entry is involved (the no-queue path
    /// in `pipeline_merge_state`).
    pub(crate) fn issue_has_any_approved_review(
        &self,
        issue: &PipelineIssue,
        seed_work_id: Option<&str>,
    ) -> bool {
        // #550: prefer the server-computed flag when present. Safe to trust
        // unconditionally (dropping `seed_work_id`): `self.data.merge_queue`
        // and `self.data.issue_stage_projection` are always parsed from the
        // SAME `/board` fetch (remote mode), so the entry the server used to
        // seed its computation is the identical one Rust would independently
        // find by `(repo_name, issue_number)` — including the no-queue-entry
        // case, where both sides seed with `None`. Empty/absent on the
        // local-SQLite read path (no daemon), where the local computation
        // below remains authoritative.
        if let Some(repo) = issue.coord_repo.as_deref() {
            if let Some(entry) = self
                .data
                .issue_stage_projection
                .iter()
                .find(|p| p.repo_name == repo && p.issue_number == issue.number)
            {
                return entry.has_approved_review;
            }
        }
        self.issue_has_any_approved_review_local(issue, seed_work_id)
    }

    /// Local fallback for [`issue_has_any_approved_review`] — the
    /// assignment-scanning computation `pipeline.rs` used exclusively before
    /// #550. Kept as the authoritative path for local-SQLite-mode coord-tui
    /// and as a safety net against older daemons.
    pub(crate) fn issue_has_any_approved_review_local(
        &self,
        issue: &PipelineIssue,
        seed_work_id: Option<&str>,
    ) -> bool {
        // Collect all work assignment IDs for this issue from the local DB.
        let mut work_ids: std::collections::HashSet<&str> = self
            .data
            .assignments
            .iter()
            .filter(|a| {
                a.issue_number == issue.number && a.assignment_type.as_deref() == Some("work")
            })
            .map(|a| a.id.as_str())
            .collect();

        // Also seed with the queue entry's assignment_id so reviews that
        // point to it are found even when the corresponding work assignment
        // row has been pruned from data.assignments.
        if let Some(id) = seed_work_id {
            work_ids.insert(id);
        }

        // #331: self-approval / PR-comment-fallback — review_verdict='approve'
        // may be stamped directly on a work assignment when no separate review
        // worker was dispatched (coordinator manual approval) or when GitHub
        // rejected `gh pr review` as a self-review (findings posted to the
        // issue comment instead, verdict still persisted to the DB).
        //
        // Treat review_verdict='approve' on any work assignment in work_ids as
        // approved — this matches the intent of coord/merge_queue.py's
        // has_approved_review: the DB verdict is the source of truth regardless
        // of whether a formal GitHub PR review exists.
        if self.data.assignments.iter().any(|a| {
            work_ids.contains(a.id.as_str()) && a.review_verdict.as_deref() == Some("approve")
        }) {
            return true;
        }

        if work_ids.is_empty() {
            return false;
        }

        self.data.assignments.iter().any(|a| {
            a.assignment_type.as_deref() == Some("review")
                && a.issue_number == issue.number
                && a.review_of_assignment_id
                    .as_deref()
                    .map(|id| work_ids.contains(id))
                    .unwrap_or(false)
                && a.review_verdict.as_deref() == Some("approve")
        })
    }

    /// Classification of whether a Merge action on the currently-selected
    /// Pipeline row will actually do anything useful.  Drives both the
    /// dispatch path (so silent no-ops are replaced with actionable
    /// toasts) and the Merge button's `enabled` state on the panel
    /// toolbar.
    pub(crate) fn pipeline_merge_state(&self) -> PipelineMergeState {
        if self.active_view != SidebarView::Pipeline {
            return PipelineMergeState::NotApplicable;
        }
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return PipelineMergeState::NotApplicable;
        };
        let Some(entry) = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug)
        else {
            // #292 (Defect 2): no queue entry yet — this is normal when
            // notify/auto_loop haven't had a chance to create the entry.
            // If work is done and review is approved, let `coord merge`
            // handle the auto-enqueue rather than showing a misleading
            // "no PR queued — work hasn't pushed a branch" toast.
            let stages = self.pipeline_stage_names();
            if stages.iter().any(|s| s == "review")
                && self.issue_has_any_approved_review(issue, None)
            {
                return PipelineMergeState::Ready {
                    issue: issue.number,
                    repo: issue.coord_repo.clone().unwrap_or_default(),
                    repo_slug: issue.repo_slug.clone(),
                };
            }
            return PipelineMergeState::NoQueue {
                issue: issue.number,
            };
        };
        if entry.state == "merged" {
            return PipelineMergeState::Merged {
                issue: issue.number,
            };
        }
        // #919: an entry already parked `conflict`/`human_required`/
        // `skipped` from a previous merge attempt must never read as Ready
        // again just because the review verdict looks approved and no CI
        // failure has been polled yet — neither of those checks is what
        // discovered the problem in the first place. `entry.error` carries
        // the gate's own message (real GitHub conflict output, or the
        // human-required/skipped reason), so surface that directly rather
        // than a generic "blocked". `skipped` is included alongside
        // conflict/human_required because the server maps all three to
        // PLAN_NEEDS_ATTENTION (`coord/merge_queue.py::_state_to_plan_status`)
        // and treats `skipped` as terminal alongside `merged`
        // (`_RESOLVE_TERMINAL_STATES`) — it will not silently self-resolve.
        if matches!(entry.state.as_str(), "conflict" | "human_required" | "skipped") {
            return PipelineMergeState::BlockedOnConflict {
                issue: issue.number,
                reason: entry
                    .error
                    .clone()
                    .unwrap_or_else(|| "merge conflict — needs manual resolution".to_string()),
            };
        }
        // #919: prefer the server-computed merge plan when present — it
        // already verifies PR mergeability and review/branch freshness
        // (`coord.merge_queue._entry_gate_status`: stale-approval SHA/
        // patch-id walk, CI failed/pending/stale, the #1877 no-checks-means-
        // conflict heuristic, …), which is exactly the "board verdict alone
        // is not enough" gap the checks below can't close on their own.
        // Absent in local-SQLite mode (no daemon) or against a daemon older
        // than #776 — the narrower checks below remain the fallback there.
        if let Some(plan_entry) =
            self.merge_plan_entry_for_issue(&issue.repo_slug, issue.number)
        {
            // #919 review: NEEDS_ATTENTION (conflict/human_required/skipped,
            // per `_state_to_plan_status`) is just as much "not ready" as
            // BLOCKED — checking only BLOCKED let a NEEDS_ATTENTION plan
            // entry fall through to the checks below and read Ready.
            if matches!(plan_entry.status.as_str(), "BLOCKED" | "NEEDS_ATTENTION") {
                return PipelineMergeState::BlockedOnConflict {
                    issue: issue.number,
                    reason: plan_entry
                        .reason
                        .clone()
                        .unwrap_or_else(|| "not mergeable".to_string()),
                };
            }
        }
        // Review gate — only meaningful when the pipeline has a Review
        // stage; otherwise downstream `coord merge` won't enforce it.
        // #292 (Defect 1/2): use issue_has_any_approved_review so a fix-work
        // approval is found even when the entry is keyed to the original work.
        // Seed with entry.assignment_id so reviews are found even if the work
        // row itself has been pruned from data.assignments.
        let stages = self.pipeline_stage_names();
        if stages.iter().any(|s| s == "review") {
            let approved = self.issue_has_any_approved_review(issue, Some(&entry.assignment_id));
            if !approved {
                // Surface the most informative verdict we can find.
                // Prefer "request-changes" over a bare "pending" so the
                // toast tells the user what happened.
                let verdict = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| {
                        a.assignment_type.as_deref() == Some("review")
                            && a.issue_number == issue.number
                    })
                    .filter_map(|a| a.review_verdict.as_deref())
                    .find(|&v| v == "request-changes")
                    .unwrap_or("pending");
                return PipelineMergeState::BlockedOnReview {
                    issue: issue.number,
                    verdict: verdict.to_string(),
                };
            }
        }
        // CI gate — only meaningful when a summary has been fetched
        // and we've actually seen failures.  Pending checks fall
        // through to Ready (coord merge will block server-side).
        if let Some(summary) = self.ci_summary_for_selected_issue() {
            if summary.has_failures() {
                return PipelineMergeState::BlockedOnCi {
                    issue: issue.number,
                    repo: issue.coord_repo.clone().unwrap_or_default(),
                };
            }
        }
        PipelineMergeState::Ready {
            issue: issue.number,
            repo: issue.coord_repo.clone().unwrap_or_default(),
            repo_slug: issue.repo_slug.clone(),
        }
    }

    /// #272-followup: route a Merge action (m keybind OR toolbar:merge
    /// button) through the state classifier so silent no-ops are
    /// replaced with actionable toasts.  Returns `true` when something
    /// happened (dispatched, opened a prompt, or surfaced a toast) so
    /// the caller can request a redraw.
    pub(crate) fn dispatch_pipeline_merge_for_selected_issue(&mut self) -> bool {
        use crate::commands::SpawnQueuedOutcome;
        match self.pipeline_merge_state() {
            PipelineMergeState::NotApplicable => {
                // Outside the Pipeline view, fall back to the unscoped
                // "merge whatever's ready" behaviour (matches the
                // pre-classifier `m` keybind on the Board / Machines
                // views).
                match self.command_runner.spawn_queued(&["merge"]) {
                    SpawnQueuedOutcome::Queued => {
                        self.push_toast(
                            "⏳ Queued",
                            "merge runs after current command",
                            ToastSeverity::Info,
                        );
                    }
                    SpawnQueuedOutcome::Deduped | SpawnQueuedOutcome::Started => {}
                }
                true
            }
            PipelineMergeState::NoQueue { issue } => {
                self.push_toast(
                    "Merge",
                    &format!("#{issue}: no PR queued yet — work hasn't pushed a branch."),
                    ToastSeverity::Warning,
                );
                true
            }
            PipelineMergeState::Merged { issue } => {
                self.push_toast(
                    "Merge",
                    &format!("#{issue} is already merged."),
                    ToastSeverity::Info,
                );
                true
            }
            PipelineMergeState::BlockedOnReview { issue, verdict } => {
                let summary = match verdict.as_str() {
                    "request-changes" => "review requested changes",
                    "pending" => "no review verdict yet",
                    other => other,
                };
                self.push_toast(
                    "Merge blocked",
                    &format!(
                        "#{issue}: {summary}. Press M to skip review, or R to re-dispatch the reviewer."
                    ),
                    ToastSeverity::Warning,
                );
                true
            }
            PipelineMergeState::BlockedOnCi { issue, repo } => {
                // Same confirm prompt the standalone CI-failed `m`
                // keybind opens; the user has to type y to bypass.
                let _ = issue;
                self.pending_force_merge = Some(repo);
                true
            }
            PipelineMergeState::BlockedOnConflict { issue, reason } => {
                // #919: never a one-click dispatch — a real conflict or a
                // gate the merge plan already marked BLOCKED needs a human
                // to rebase/resolve/re-review, not a retry of the same
                // merge that already failed (or would fail) this way.
                self.push_toast(
                    "Merge blocked",
                    &format!("#{issue}: {reason}"),
                    ToastSeverity::Warning,
                );
                true
            }
            PipelineMergeState::Ready { issue, repo, repo_slug } => {
                if repo.is_empty() {
                    // No coord_repo mapping — fall back to unscoped
                    // merge so power users with cross-repo work can
                    // still drive it from the TUI.
                    match self.command_runner.spawn_queued(&["merge"]) {
                        SpawnQueuedOutcome::Started => {
                            // #347: no coord_repo so we can't key the inflight
                            // set (merge_stage_status_for keys on repo_slug),
                            // but at least show a status-bar toast so there's
                            // some feedback.
                            self.pipeline_status = Some((
                                format!("merge dispatched (#{issue})"),
                                Instant::now(),
                            ));
                        }
                        SpawnQueuedOutcome::Queued => {
                            self.push_toast(
                                "⏳ Queued",
                                "merge runs after current command",
                                ToastSeverity::Info,
                            );
                        }
                        SpawnQueuedOutcome::Deduped => {}
                    }
                } else {
                    match self
                        .command_runner
                        .spawn_queued(&["merge", "--repo", &repo])
                    {
                        SpawnQueuedOutcome::Started => {
                            // #347: mirror the optimistic update that
                            // dispatch_pipeline_merge (per-stage [Go]) does —
                            // set the inflight flag so merge_stage_status_for
                            // returns Active immediately, and surface a toast
                            // via pipeline_status, all within the same frame as
                            // the user action.
                            self.pipeline_inflight_merges
                                .insert((repo_slug, issue));
                            self.pipeline_status = Some((
                                format!("merge dispatched for {} (#{})", repo, issue),
                                Instant::now(),
                            ));
                        }
                        SpawnQueuedOutcome::Queued => {
                            self.push_toast(
                                "⏳ Queued",
                                "merge runs after current command",
                                ToastSeverity::Info,
                            );
                        }
                        SpawnQueuedOutcome::Deduped => {}
                    }
                }
                true
            }
        }
    }

    /// Find the most-recent review assignment id for the selected Pipeline
    /// row whose verdict is `request-changes`.  Used by the Fix action
    /// (action bar + right-click menu + F keybind) to identify which
    /// review's findings the dispatched fix worker should address.
    pub(crate) fn selected_pipeline_review_id_for_bounce(&self) -> Option<String> {
        if self.active_view != SidebarView::Pipeline {
            return None;
        }
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
        // #582: a merge_queue row only exists once the review gate has
        // passed — issues with request-changes reviews that haven't
        // reached the merge stage yet (or never got a queue row) have no
        // entry here. Fall back to the same direct-`assignments` lookup
        // `selected_row_has_request_changes_for` uses for menu visibility,
        // so the action doesn't unconditionally fail on rows the menu
        // correctly showed as actionable.
        let work_id = match entry {
            Some(entry) => entry.assignment_id.clone(),
            None => self
                .data
                .assignments
                .iter()
                .filter(|a| {
                    a.issue_number == issue.number && a.assignment_type.as_deref() == Some("work")
                })
                .max_by(|a, b| {
                    a.dispatched_at
                        .partial_cmp(&b.dispatched_at)
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|a| a.id.clone())?,
        };
        // Most-recent review (highest dispatched_at) paired with this
        // work and carrying a request-changes verdict.
        self.data
            .assignments
            .iter()
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .filter(|a| a.review_of_assignment_id.as_deref() == Some(&work_id))
            .filter(|a| a.review_verdict.as_deref() == Some("request-changes"))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|a| a.id.clone())
    }

    /// Dispatch `coord bounce <review-id>` for the selected Pipeline row.
    /// Returns `true` when a command was actually spawned; toasts and
    /// returns `false` when the row isn't actionable (no
    /// request-changes verdict, no review pairing, etc.).
    pub(crate) fn dispatch_bounce_for_selected_pipeline_row(&mut self) -> bool {
        let Some(review_id) = self.selected_pipeline_review_id_for_bounce() else {
            self.push_toast(
                "Bounce",
                "No request-changes review found for this row — nothing to address.",
                ToastSeverity::Warning,
            );
            return false;
        };
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["bounce", &review_id]);
        match outcome {
            SpawnQueuedOutcome::Deduped => {
                // Same bounce already running or queued — nothing to add.
                false
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Bounce",
                    &format!(
                        "Dispatching fix worker for review {}\u{2026} Work will go Active in 1-3s once the subprocess completes.",
                        &review_id[..review_id.len().min(8)],
                    ),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Bounce",
                    &format!(
                        "Bounce queued for review {}\u{2026} Will dispatch once the current command completes.",
                        &review_id[..review_id.len().min(8)],
                    ),
                    ToastSeverity::Info,
                );
                true
            }
        }
    }

    /// Look up the CI summary for the currently-selected pipeline issue's
    /// merge queue entry.  Returns `None` when no PR is queued for the
    /// selected issue, or when no summary has been fetched yet.
    pub(crate) fn ci_summary_for_selected_issue(&self) -> Option<&CiCheckSummary> {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug)?;
        let pr = entry.pr_number?;
        self.pipeline_ci_checks
            .get(&(entry.repo_github.clone(), pr))
    }

    /// Drain the in-flight poll channel without blocking. Returns `true`
    /// when results were received and the issue list/sidebar changed.
    /// #486 Leg 4: drain the background local+remote session fetch.  When it
    /// completes it REPLACES the local-only startup snapshot so the reattach
    /// detection in `launch_interactive_session_for_selected_issue` can target
    /// sessions running on remote fleet machines.  Returns true on update.
    /// Maximum number of discovery sweeps an optimistic `"pending-"` entry may
    /// survive without being covered by a real session before it is evicted.
    /// Two sweeps ≈ 2× the remote-session poll interval (typically ~10 s each),
    /// so a genuinely-starting session has plenty of time to appear while a
    /// never-actually-started phantom is dropped within ~20 s. (#935 Part A)
    pub(crate) const PENDING_SESSION_SWEEP_BUDGET: u8 = 2;

    pub(crate) fn poll_remote_sessions(&mut self) -> bool {
        let Some(rx) = self.pending_remote_sessions.as_ref() else {
            return false;
        };
        match rx.try_recv() {
            Ok(sessions) => {
                // #559: merge rather than replace so that an optimistic entry
                // (assignment_id starts with "pending-") added on a fresh
                // launch isn't clobbered by a discovery sweep that ran before
                // the new session was discoverable.  Keep any pending entry
                // whose (repo_name, issue_number) pair is NOT already covered
                // by the real discovery result; drop it once the real session
                // appears (to avoid stale phantom entries accumulating).
                //
                // #935 Part A: increment the sweep counter each time a pending
                // entry survives uncovered.  Once it exceeds
                // `PENDING_SESSION_SWEEP_BUDGET` the entry is evicted so that
                // a phantom "pending-" that never becomes a real session doesn't
                // keep the Pipeline card pinned to "Live" indefinitely.
                let covered: std::collections::HashSet<(Option<String>, Option<u64>)> = sessions
                    .iter()
                    .map(|s| (s.repo_name.clone(), s.issue_number))
                    .collect();
                let surviving_pending: Vec<LiveTmuxSession> = self
                    .live_tmux_sessions
                    .drain(..)
                    .filter_map(|mut s| {
                        if !s.assignment_id.starts_with("pending-") {
                            return None; // real entries are replaced by discovery
                        }
                        if covered.contains(&(s.repo_name.clone(), s.issue_number)) {
                            return None; // real session appeared → drop optimistic
                        }
                        s.pending_sweep_count =
                            s.pending_sweep_count.saturating_add(1);
                        if s.pending_sweep_count > Self::PENDING_SESSION_SWEEP_BUDGET {
                            return None; // budget exhausted → evict phantom
                        }
                        Some(s)
                    })
                    .collect();
                self.live_tmux_sessions = surviving_pending;
                self.live_tmux_sessions.extend(sessions);
                self.pending_remote_sessions = None;
                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.pending_remote_sessions = None;
                false
            }
        }
    }

    /// #954: maximum number of discovery sweeps an optimistic `pending`
    /// `FleetTerminal` may survive uncovered by a real result before it's
    /// evicted. Mirrors `PENDING_SESSION_SWEEP_BUDGET`.
    pub(crate) const PENDING_TERMINAL_SWEEP_BUDGET: u8 = 2;

    /// #953: drain the background local+remote `coord terminal list` sweep.
    ///
    /// #954: unlike the #953-era doc comment this superseded, fleet
    /// terminals now CAN carry an optimistic `pending` entry (inserted by
    /// `create_and_attach_terminal` on a fresh `n` → create), so a landed
    /// result merges rather than blindly replacing — mirrors
    /// `poll_remote_sessions`'s `"pending-"` merge, keyed on
    /// `(machine, name)` instead of `(repo_name, issue_number)`. Returns
    /// `true` on update.
    pub(crate) fn poll_remote_terminals(&mut self) -> bool {
        let Some(rx) = self.pending_remote_terminals.as_ref() else {
            return false;
        };
        match rx.try_recv() {
            Ok(terminals) => {
                let covered: std::collections::HashSet<(String, String)> = terminals
                    .iter()
                    .map(|t| (t.machine.clone(), t.name.clone()))
                    .collect();
                let surviving_pending: Vec<FleetTerminal> = self
                    .fleet_terminals
                    .drain(..)
                    .filter_map(|mut t| {
                        if !t.pending {
                            return None; // real entries are replaced by discovery
                        }
                        if covered.contains(&(t.machine.clone(), t.name.clone())) {
                            return None; // real terminal appeared → drop optimistic
                        }
                        t.pending_sweep_count = t.pending_sweep_count.saturating_add(1);
                        if t.pending_sweep_count > Self::PENDING_TERMINAL_SWEEP_BUDGET {
                            return None; // budget exhausted → evict phantom
                        }
                        Some(t)
                    })
                    .collect();
                self.fleet_terminals = surviving_pending;
                self.fleet_terminals.extend(terminals);
                self.pending_remote_terminals = None;
                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.pending_remote_terminals = None;
                false
            }
        }
    }

    /// #1563: drain the background paused-machine sweep armed by
    /// `refresh()` (`spawn_paused_machines_fetch` — local file read or
    /// daemon `GET /pause`, whichever applies). Replaces `paused_machines`
    /// wholesale rather than merging: unlike the optimistic-pending merges
    /// above, there's no local-only speculative entry to preserve here —
    /// the landed result IS the full authoritative set. Returns `true` on
    /// update.
    pub(crate) fn poll_paused_machines(&mut self) -> bool {
        let Some(rx) = self.pending_paused_machines.as_ref() else {
            return false;
        };
        match rx.try_recv() {
            Ok(fresh) => {
                self.pending_paused_machines = None;
                // #1862: `quiet` rides alongside `paused` in the same
                // fetch/channel (`data::PausedFetch`) — compare both so a
                // machine flipping from hand-paused to quiet-paused (or
                // vice versa) with the same net `paused` membership still
                // triggers a redraw of the sidebar badge.
                // #2101: `cordoned` rides in the same fetch for the same
                // reason — a machine flipping from hand-paused to cordoned
                // with unchanged `paused` membership must still redraw.
                // #2147: `quiet_hours` too — a machine's schedule can
                // change (an operator sets/clears a window, or edits one)
                // with `paused`/`quiet`/`cordoned` membership all unchanged
                // (the new window doesn't cover *now* either), and the
                // sidebar badge/dialog pre-fill must still pick that up.
                if fresh.paused != self.paused_machines
                    || fresh.quiet != self.quiet_paused_machines
                    || fresh.cordoned != self.cordoned_machines
                    || fresh.quiet_hours != self.quiet_hours_windows
                {
                    self.paused_machines = fresh.paused;
                    self.quiet_paused_machines = fresh.quiet;
                    self.cordoned_machines = fresh.cordoned;
                    self.quiet_hours_windows = fresh.quiet_hours;
                    return true;
                }
                false
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.pending_paused_machines = None;
                false
            }
        }
    }

    // ── #2284 (ms-65 §3): Pipeline document tabs ─────────────────────────
    //
    // Pipeline's own independent `PanelScope::Pipeline` tab set — the exact
    // Board pattern (`mod.rs`'s "#2282 (ms-65 §2): Board document tabs"
    // section), scoped to `doc_tabs.group(PanelScope::Pipeline)` and to
    // Pipeline's own issue cache/sidebar instead of Board's. Deliberately
    // does NOT wire close/`Ctrl-W`/`Ctrl-Tab` (contract §4, #2283) — that
    // issue's sealed slice only ever exercised the Board strip, and this
    // issue's contract (§3) is scoped to independence + reveal, not
    // close/navigate. `DocTabGroup::close` is scope-agnostic, so a future
    // slice can wire it in without touching the model.

    /// The Pipeline panel's document-tab set.
    ///
    /// `pub(crate)`: unlike Board's `mod.rs`-resident equivalent (private but
    /// reachable from every descendant module because `mod.rs` *is* the
    /// `app` module), this lives in the `app::pipeline` submodule, so
    /// `render.rs`/`events.rs` (sibling submodules) need explicit crate
    /// visibility to call it.
    pub(crate) fn pipeline_doc_tabs(&self) -> &doc_tabs::DocTabGroup {
        self.doc_tabs.group(PanelScope::Pipeline)
    }

    /// The active Pipeline document's key, or `None` when no tab is open.
    pub(crate) fn pipeline_doc_active_key(&self) -> Option<&DocKey> {
        self.pipeline_doc_tabs().active_key()
    }

    /// Look one issue up in `pipeline_issues` by its doc key — `repo_slug`
    /// (the GitHub `owner/name` form `capture_pipeline_selection_id` and
    /// `rebuild_pipeline_sidebar`'s `prev_sel_override` already key
    /// selection-restore by), not `coord_repo`. Pipeline doc keys use this
    /// convention throughout this section so every tab-model access agrees
    /// with the sidebar's own restore path.
    pub(crate) fn pipeline_issue_for(&self, repo: &str, number: u64) -> Option<&PipelineIssue> {
        self.pipeline_issues
            .iter()
            .find(|pi| pi.repo_slug == repo && pi.number == number)
    }

    /// Open `key` in the Pipeline tab strip — the Pipeline-scope counterpart
    /// of `mod.rs`'s `open_board_doc_tab`; see that method's doc comment for
    /// the `pin` semantics (contract §2e rules 1/2/4 for `false`, rule 3 for
    /// `true`). Reveals the newly-active document in the Pipeline sidebar
    /// (§2f, applied per-scope per #2284's fourth acceptance criterion) when
    /// activation actually moved.
    ///
    /// #2285 (§5): checkpoint/restore around the mutation, exactly as
    /// `open_board_doc_tab` does — see `mod.rs`'s "#2285 (ms-65 §5): per-tab
    /// detail sub-state" section for why the restore has to follow the reveal
    /// (`reveal_pipeline_active_doc` re-seeds `pipeline_focused_stage` and
    /// `pipeline_stage_content_scroll`, and the restored values must win).
    pub(crate) fn open_pipeline_doc_tab(&mut self, key: DocKey, pin: bool) {
        let before = self.pipeline_doc_active_key().cloned();
        self.checkpoint_detail_sub_state(PanelScope::Pipeline);
        {
            let group = self.doc_tabs.group_mut(PanelScope::Pipeline);
            if pin {
                group.pin(key);
            } else {
                group.open_preview(key);
            }
        }
        if self.pipeline_doc_active_key().cloned() != before {
            self.reveal_pipeline_active_doc();
            self.restore_detail_sub_state(PanelScope::Pipeline);
        }
        self.persist_doc_tabs();
    }

    /// Activate the Pipeline tab at strip index `idx` (a click on the strip
    /// itself). Returns `true` when the click landed on a real tab. Reveals
    /// unconditionally, mirroring `mod.rs`'s `activate_board_doc_tab` —
    /// re-activating the already-active tab is still a valid "take me back"
    /// gesture.
    pub(crate) fn activate_pipeline_doc_tab(&mut self, idx: usize) -> bool {
        if idx >= self.pipeline_doc_tabs().tabs().len() {
            return false;
        }
        self.checkpoint_detail_sub_state(PanelScope::Pipeline);
        self.doc_tabs.group_mut(PanelScope::Pipeline).activate_index(idx);
        self.reveal_pipeline_active_doc();
        self.restore_detail_sub_state(PanelScope::Pipeline);
        self.persist_doc_tabs();
        true
    }

    /// Close the Pipeline tab at strip index `idx`. Not exercised by any
    /// keyboard chord in this slice (see the section note above) — reachable
    /// only via a click on the strip's `×`, which `events.rs`'s Pipeline
    /// `mouse_main_click` arm routes here the same way the Board arm routes
    /// to `close_board_doc_tab`.
    pub(crate) fn close_pipeline_doc_tab(&mut self, idx: usize) -> bool {
        let before = self.pipeline_doc_active_key().cloned();
        if !self.doc_tabs.group_mut(PanelScope::Pipeline).close(idx) {
            return false;
        }
        if self.pipeline_doc_active_key().cloned() != before {
            self.reveal_pipeline_active_doc();
            self.restore_detail_sub_state(PanelScope::Pipeline);
        }
        self.persist_doc_tabs();
        true
    }

    /// Height, in main-axis units, of one Pipeline sidebar row. Mirrors
    /// `mod.rs`'s `board_row_height` exactly — same quadraui
    /// `sidebar_system::body_measure` rule (`Decoration::Header` rows measure
    /// `lh * 1.2`, everything else `lh * 1.4`, both rounded) — Pipeline and
    /// Board share one `SidebarSystem` implementation, so the measurement
    /// rule is identical; only the row data backing it differs.
    fn pipeline_row_height(lh: f32, is_header: bool) -> f32 {
        if is_header {
            (lh * 1.2).round()
        } else {
            (lh * 1.4).round()
        }
    }

    /// Content height of Pipeline sidebar section `s` (excluding its own
    /// header row), as quadraui's `ScrollMode::WholePanel` sizing computes
    /// it. Mirrors `board_section_content_height`.
    fn pipeline_section_content_height(&self, s: usize, lh: f32) -> f32 {
        if self.pipeline_sidebar.is_collapsed(s) {
            return 0.0;
        }
        if let Some(form) = self.pipeline_sidebar.form(s) {
            return form.fields.len() as f32 * Self::pipeline_row_height(lh, false);
        }
        self.pipeline_section_rows
            .get(s)
            .map(|rows| {
                rows.iter()
                    .map(|(_, is_header)| Self::pipeline_row_height(lh, *is_header))
                    .sum()
            })
            .unwrap_or(0.0)
    }

    /// #2452 (closes the Pipeline/Board parity gap noted in that issue):
    /// scroll the Pipeline sidebar so the currently-selected row is inside
    /// the viewport — the Pipeline counterpart of `scroll_board_selection_
    /// into_view`, which `reveal_board_active_doc` already relies on.
    /// Pipeline's sidebar runs in `ScrollMode::WholePanel` exactly like
    /// Board's, so the same reconstruction (row heights + section content
    /// heights, backed by the geometry stashed at paint time) applies
    /// unchanged — see that method's doc comment for why this can't just
    /// call a `SidebarSystem::reveal` API (it doesn't exist yet).
    ///
    /// Deliberately does NOT replicate Board's `board_reveal_hidden_count`
    /// windowing (the `⋮ N more above` marker-row collapse that keeps the
    /// FILTER box's scroll position pinned at 0): Pipeline groups multiple
    /// repos inside a single lifecycle section (repo is a sub-header, not
    /// its own section, unlike Board), so windowing would have to track a
    /// hidden-row count per SECTION rather than per repo, against a
    /// 3-level tree instead of Board's 2-level one. A plain panel scroll
    /// already satisfies this issue's bar (the target row ends up on
    /// screen); trimming the scroll distance with a marker row is a
    /// well-scoped follow-up, not a correctness gap.
    fn scroll_pipeline_selection_into_view(&mut self) {
        let Some((rect, lh, header_size)) = self.last_pipeline_sidebar_geom.get() else {
            return;
        };
        if rect.height <= 0.0 || lh <= 0.0 {
            return;
        }
        let Some(section) = self.pipeline_sidebar.active_section() else {
            return;
        };
        let Some(path) = self.pipeline_sidebar.selected_path(section).cloned() else {
            return;
        };

        // Walk the section stack down to the target section's body. Hidden
        // sections are dropped from the view entirely (`build_view` skips
        // them), so they cost nothing.
        let mut y = 0.0_f32;
        for s in 0..section {
            if !self.pipeline_sidebar.is_section_visible(s) {
                continue;
            }
            y += header_size + self.pipeline_section_content_height(s, lh);
        }
        if !self.pipeline_sidebar.is_section_visible(section) {
            return;
        }
        y += header_size;

        // …then across the rows above the target inside that section.
        let Some(rows) = self.pipeline_section_rows.get(section) else {
            return;
        };
        let mut row_h = None;
        for (row_path, is_header) in rows {
            let h = Self::pipeline_row_height(lh, *is_header);
            if *row_path == path {
                row_h = Some(h);
                break;
            }
            y += h;
        }
        let Some(row_h) = row_h else {
            return;
        };

        let scroll = self.pipeline_sidebar.panel_scroll();
        let new = if y < scroll {
            y
        } else if y + row_h > scroll + rect.height {
            y + row_h - rect.height
        } else {
            return;
        };
        self.pipeline_sidebar.set_panel_scroll(new.max(0.0));
    }

    /// Contract §2f, applied per-scope (#2284 AC 4): select the active
    /// Pipeline document's row in the **Pipeline** sidebar, never the
    /// Board's — mirrors `jump_to_pipeline`'s reveal-and-expand body
    /// (`dialogs.rs`), minus the `switch_active_view` call (the caller is
    /// already on the Pipeline panel by construction: this only runs from
    /// Pipeline-scope tab mutations) and the `pipeline_jump_target`
    /// visibility gate (the issue is already known to be a live
    /// `PipelineIssue` — no need to re-derive it from a coord-repo lookup).
    ///
    /// `rebuild_pipeline_sidebar`'s `prev_sel_override` is what actually
    /// moves the sidebar selection and syncs `pipeline_sel` — expanding the
    /// target's New/In-progress milestone (and repo, for In-progress) group
    /// first is what stops that restore from landing on a row that doesn't
    /// exist in the tree yet (mirrors §2f's Board-side milestone-expand
    /// step, `expand_board_milestone_for`).
    fn reveal_pipeline_active_doc(&mut self) {
        let Some((repo_slug, number)) = self.pipeline_doc_active_key().cloned() else {
            return;
        };
        let Some(idx) = self
            .pipeline_issues
            .iter()
            .position(|pi| pi.repo_slug == repo_slug && pi.number == number)
        else {
            return;
        };
        let pi = &self.pipeline_issues[idx];
        let lc_key = self.pipeline_lifecycle_section(pi);
        let repo_key = Self::pipeline_repo_key(pi).to_string();
        let mil_key = match self.pipeline_issue_milestone(pi) {
            Some((n, _)) => n.to_string(),
            None => "no-milestone".to_string(),
        };
        if lc_key == "new" {
            self.pipeline_milestone_expanded
                .insert(("new".to_string(), repo_key, mil_key), true);
        } else if lc_key == "in-progress" {
            self.pipeline_lifecycle_expanded
                .insert(("in-progress".to_string(), repo_key.clone()), true);
            self.pipeline_milestone_expanded
                .insert(("in-progress".to_string(), repo_key, mil_key), true);
        }
        self.rebuild_pipeline_sidebar(Some((repo_slug, number)));
        // #2449/#2452: `rebuild_pipeline_sidebar` itself now forces
        // `pipeline_sel` onto the active Pipeline doc tab's issue whenever
        // one is open, independent of whether it found a tree row for it —
        // see the "#2452" note at its own tail. Every detail-pane render
        // function and per-issue action keys off `pipeline_sel`, not the doc
        // tab directly, so that guarantee is what keeps them showing this
        // issue's own content even when the active Pipeline search filter
        // hides its row.
        self.pipeline_focused_stage = self.default_focused_stage_for_selected_issue();
        self.pipeline_stage_content_scroll = 0;
        // #2452: `set_selected_path` alone says nothing about scroll
        // position — a filtered-in row the rebuild above just selected can
        // still be scrolled past. Bring it on screen, closing the Pipeline/
        // Board parity gap (Board's `reveal_board_active_doc` already does
        // this via `scroll_board_selection_into_view`). No-ops when the
        // tree has no selection (filtered-out target) or the row is already
        // visible.
        self.scroll_pipeline_selection_into_view();
    }
}

// ─── `run_command_with_timeout` (#2501 review) ─────────────────────────────
//
// Exercises the generic timeout/kill machinery directly, against a synthetic
// long-running command, rather than a real (and un-hangable-on-demand) git
// remote — see the function's own doc comment for why it's generic over
// `program` in the first place.
#[cfg(test)]
mod timeout_tests {
    use super::*;

    /// A shell one-liner that sleeps for `secs` — the stand-in for a remote
    /// that accepts the connection and then never answers.
    fn sleep_cmd(secs: u64) -> (&'static str, Vec<String>) {
        ("sh", vec!["-c".to_string(), format!("sleep {secs}")])
    }

    #[test]
    fn returns_output_when_the_command_finishes_within_the_deadline() {
        let out = run_command_with_timeout(
            "sh",
            std::path::Path::new("."),
            &["-c", "echo hello"],
            Duration::from_secs(5),
        )
        .expect("a fast command must not be treated as a timeout");
        assert!(out.status.success());
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "hello");
    }

    #[test]
    fn kills_and_reports_timeout_when_the_command_outlives_the_deadline() {
        let (program, args) = sleep_cmd(30);
        let args_ref: Vec<&str> = args.iter().map(String::as_str).collect();
        let started = Instant::now();
        let result = run_command_with_timeout(
            program,
            std::path::Path::new("."),
            &args_ref,
            Duration::from_millis(150),
        );
        let elapsed = started.elapsed();
        let err = result.expect_err("a 30s sleep must not be allowed to finish");
        assert!(
            err.contains("timed out"),
            "expected a timeout error, got: {err}"
        );
        assert!(
            elapsed < Duration::from_secs(5),
            "the sleeping child must actually be killed, not just given up on \
             while it keeps running in the background; took {elapsed:?}"
        );
    }

    #[test]
    fn reports_spawn_failure_for_a_nonexistent_program() {
        let err = run_command_with_timeout(
            "coord-tui-test-definitely-not-a-real-binary-2501",
            std::path::Path::new("."),
            &[],
            Duration::from_secs(5),
        )
        .expect_err("a nonexistent program cannot spawn");
        assert!(
            err.contains("could not launch"),
            "expected a launch-failure reason, got: {err}"
        );
    }
}

// ─── `percent_encode_file_url_path_str` (#2501 review) ─────────────────────
//
// Operates on literal strings rather than a real `PathBuf`, so the Windows
// drive-letter branch is reachable from every host OS this suite runs on —
// see the function's own doc comment for why.
#[cfg(test)]
mod file_url_tests {
    use super::*;

    #[test]
    fn unix_absolute_path_is_unchanged_but_encoded() {
        let out = percent_encode_file_url_path_str("/home/john/mocks dir");
        assert_eq!(out, "/home/john/mocks%20dir");
    }

    #[test]
    fn windows_backslash_drive_path_becomes_canonical_file_url_form() {
        let out = percent_encode_file_url_path_str(r"C:\Users\john\mocks");
        assert_eq!(
            out, "/C:/Users/john/mocks",
            "must produce the RFC 8089 form (leading slash, forward \
             slashes, unescaped drive-letter colon) so `file://` + this is \
             a URL an opener recognises"
        );
    }

    #[test]
    fn windows_forward_slash_drive_path_still_gets_the_leading_slash() {
        // Already-forward-slash Windows paths (e.g. from a path built with
        // `/` throughout) still need the leading `/` before the drive
        // letter — `file://C:/...` (two slashes) is not the same URL as
        // `file:///C:/...` (three).
        let out = percent_encode_file_url_path_str("C:/Users/john/mocks");
        assert_eq!(out, "/C:/Users/john/mocks");
    }

    #[test]
    fn bare_drive_root_is_recognised_as_a_windows_path() {
        let out = percent_encode_file_url_path_str(r"C:\");
        assert_eq!(out, "/C:/");
    }

    #[test]
    fn windows_path_with_spaces_is_still_percent_encoded() {
        let out = percent_encode_file_url_path_str(r"C:\Users\john\Gate A mocks");
        assert_eq!(out, "/C:/Users/john/Gate%20A%20mocks");
    }

    #[test]
    fn a_lone_drive_letter_and_colon_with_no_third_char_counts_as_windows() {
        // `C:` alone (len 2) — the drive-relative-root edge case explicitly
        // covered by `s.as_bytes().get(2) == None`.
        let out = percent_encode_file_url_path_str("C:");
        assert_eq!(out, "/C:");
    }

    #[test]
    fn a_unix_path_that_merely_contains_a_colon_is_not_mistaken_for_a_drive_path() {
        // `bin:extra` at the START of the string could only be a Windows
        // drive path if the first character were a single ASCII letter —
        // here it's a multi-character segment, so this must NOT be treated
        // as `<letter>:` and must NOT gain a leading slash or have its
        // (already valid on Unix) colon touched differently than before.
        let out = percent_encode_file_url_path_str("bin:extra/thing");
        assert_eq!(out, "bin:extra/thing", "no spurious leading slash or rewriting");
    }

    #[test]
    fn a_relative_looking_two_char_prefix_without_letter_first_is_not_a_drive_path() {
        // Starts with a digit, not a letter — must not match the Windows
        // drive-path heuristic even though byte[1] is `:`.
        let out = percent_encode_file_url_path_str("1:\\notadrive");
        assert_eq!(
            out, "1:%5Cnotadrive",
            "non-letter-prefixed `X:` must not gain the drive-path leading \
             slash or backslash-to-slash rewrite (the literal backslash is \
             still percent-encoded; `:` is unescaped everywhere, drive path \
             or not — see the function doc comment)"
        );
    }
}
