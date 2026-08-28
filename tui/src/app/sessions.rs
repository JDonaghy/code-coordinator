//! Interactive session management extracted from `app/mod.rs` (#744).
//!
//! Covers session launch (Work / Plan / Review / Fix / Test / Merge / Chat),
//! reattach, kill, fleet machine picker, briefing helpers, and the
//! [`InteractiveLaunchMode`] enum + command-builder functions.
//!
//! **Import pattern:** `use super::*` (rather than explicit imports) is
//! intentional for impl-block submodules.  These methods operate on
//! `CoordApp` and need the full parent namespace — all quadraui types,
//! app-field types, and bindings re-exported from other extracted modules.
//! Pure-function submodules (`format.rs`, `data.rs`) use explicit imports
//! because their dependency surface is small and stable.
#[allow(unused_imports)]
use super::*;

// ─── Interactive session management ──────────────────────────────────────────

impl CoordApp {

    /// Resolve the coord-local repo name + per-issue terminal key for the
    /// currently-selected Pipeline row (#467).
    ///
    /// Reads the SELECTED `pipeline_issues` row directly rather than looking
    /// an issue up by number: issue numbers are not unique across repos
    /// (e.g. vimcode #207 vs claude-coordinator #207), so a number lookup
    /// could resolve the wrong repo and dispatch the wrong issue (#480).
    /// Returns `(coord_repo_name, (repo_slug, number))`.
    pub(crate) fn selected_issue_repo_and_key(&self) -> Option<(String, (String, u64))> {
        // #675 BUG 5: When on the Board panel, use the Board selection so that
        // 'Chat about issue' always refers to the board-selected issue — not
        // whatever pipeline_sel still points to from a previous Pipeline click.
        // Mirror the guard order in `selected_issue_key()`.
        if self.active_view == SidebarView::Board {
            let (coord_repo, num) = self.board_selected_issue()?;
            let slug = self
                .data
                .pipeline_repos
                .iter()
                .find(|(name, _)| *name == coord_repo)
                .map(|(_, s)| s.clone())
                .unwrap_or_else(|| coord_repo.clone());
            return Some((coord_repo, (slug, num)));
        }
        // For all other views (Pipeline, Terminal, etc.) use the pipeline
        // selection as the primary source.
        if let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) {
            let repo = match issue.coord_repo.as_deref() {
                Some(cr) if !cr.is_empty() => cr.to_string(),
                _ => issue
                    .repo_slug
                    .rsplit('/')
                    .next()
                    .filter(|s| !s.is_empty())?
                    .to_string(),
            };
            return Some((repo, (issue.repo_slug.clone(), issue.number)));
        }
        None
    }

    /// #539: Return the `id` of the most-recent completed `type="work"`
    /// assignment that has a non-empty branch for the currently-selected
    /// pipeline issue, or `None` when no such assignment exists.
    ///
    /// Used to gate the "Start review (interactive)" context-menu action and
    /// to supply the `--review-of <work_aid>` argument when the action fires.
    pub(crate) fn selected_completed_work_aid(&self) -> Option<String> {
        let (repo, issue_key) = self.selected_issue_repo_and_key()?;
        self.completed_work_aid_for(&repo, issue_key.1)
    }

    /// #266: true when the selected pipeline issue has real pipeline execution
    /// worth preserving — a *workable* assignment (work / review / smoke /
    /// conflict-fix / fix-*) that is `done`, `merged`, or `running`.  Gates
    /// "Drop to backlog" off for such rows so completed or in-flight work is
    /// never silently orphaned (#618).  Rows whose only assignments are scoping
    /// chats or *failed* attempts have nothing to preserve and stay droppable.
    ///
    /// #1553: matched on [`Assignment::effective_issue_number`] so an
    /// oracle-loop child with a live (or completed) acceptance slice counts
    /// as having progress. Before this, that slice was booked under the
    /// milestone's tracking issue, so a child with six sessions running for
    /// it was still offered "Drop to backlog".
    pub(crate) fn selected_issue_has_work_progress(&self) -> bool {
        let Some((repo, issue_key)) = self.selected_issue_repo_and_key() else {
            return false;
        };
        let issue_num = issue_key.1;
        self.data.assignments.iter().any(|a| {
            a.effective_issue_number() == issue_num
                && a.repo == repo
                && a.assignment_type
                    .as_deref()
                    .map(is_workable_type)
                    .unwrap_or(true)
                && matches!(a.status.as_str(), "done" | "merged" | "running")
        })
    }

    /// Leg 2 (#517): the most-recent `done` `type="work"` assignment id with a
    /// non-empty branch for `(coord_repo, issue_num)`, or `None`.  Generalises
    /// [`selected_completed_work_aid`] to an arbitrary issue so the auto-advance
    /// detector can check issues other than the selected row.
    ///
    /// #1059: also matches `type="mock-author"` (Gate A, #930) — mirrors
    /// Python's `coord.models.WORK_LIKE_TYPES`. A Gate-A mock-author
    /// assignment is dispatched against the milestone's own tracking-epic
    /// issue number and flows through the same Work → Test → Review → Merge
    /// pipeline as an ordinary work assignment; without this, Testing/
    /// Review/Merge stayed permanently disabled for an epic row once its
    /// Gate-A worker completed, since the strict `"work"` match never found
    /// it.
    ///
    /// #1223: also matches `type="test-author"` (the per-issue JIT
    /// acceptance-authoring slice, #1084/#1173) — but unlike "work"/
    /// "mock-author", a test-author assignment is recorded under the
    /// milestone's tracking-issue number (`issue_number`), not the member
    /// issue's own number, so it correlates back via `for_issue_number`
    /// instead (set at dispatch by `coord.test_author.dispatch_test_author`).
    /// Without this branch, `issue_num` (a member issue) would never match a
    /// test-author row's `issue_number` (the tracking issue), leaving Start
    /// review / Start merge / Fix permanently disabled for a member issue
    /// once its JIT authoring slice finished — exactly the #1059 gap this
    /// mirrors, one level down (per-issue instead of per-milestone).
    pub(crate) fn completed_work_aid_for(&self, coord_repo: &str, issue_num: u64) -> Option<String> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.repo == coord_repo)
            .filter(|a| match a.assignment_type.as_deref().unwrap_or("work") {
                "work" | "mock-author" => a.issue_number == issue_num,
                "test-author" => a.for_issue_number == Some(issue_num),
                _ => false,
            })
            .filter(|a| a.status == "done")
            .filter(|a| a.branch.as_deref().map(|b| !b.is_empty()).unwrap_or(false))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|a| a.id.clone())
    }

    /// Leg 2 (#517): all `done` `type="work"` assignment ids with a branch for
    /// `(coord_repo, issue_num)` — the arm-time snapshot used to edge-trigger
    /// the auto-advance prompt only on a *new* completion.
    pub(crate) fn done_work_aids_for(
        &self,
        coord_repo: &str,
        issue_num: u64,
    ) -> std::collections::HashSet<String> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue_num)
            .filter(|a| a.repo == coord_repo)
            .filter(|a| a.assignment_type.as_deref().unwrap_or("work") == "work")
            .filter(|a| a.status == "done")
            .filter(|a| a.branch.as_deref().map(|b| !b.is_empty()).unwrap_or(false))
            .map(|a| a.id.clone())
            .collect()
    }

    /// Leg 2/3 (#517): true when some `type="review"` assignment already targets
    /// the given work aid (`review_of_assignment_id == work_aid`).  Gates the
    /// auto-advance prompt to fire once per work completion — and, crucially,
    /// lets it fire AGAIN after a fix (the fix is a new work aid with no review
    /// yet), driving the incremental re-review loop.
    pub(crate) fn work_has_review(&self, coord_repo: &str, issue_num: u64, work_aid: &str) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue_num
                && a.repo == coord_repo
                && a.assignment_type.as_deref() == Some("review")
                && a.review_of_assignment_id.as_deref() == Some(work_aid)
        })
    }

    /// Leg 3 (#517): review assignment ids for `(coord_repo, issue_num)` that
    /// already carry a verdict — the arm-time snapshot so verdict-routing only
    /// fires on a freshly-reported verdict.
    pub(crate) fn verdicted_review_ids_for(
        &self,
        coord_repo: &str,
        issue_num: u64,
    ) -> std::collections::HashSet<String> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue_num && a.repo == coord_repo)
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .filter(|a| {
                a.review_verdict
                    .as_deref()
                    .map(|v| !v.is_empty())
                    .unwrap_or(false)
            })
            .map(|a| a.id.clone())
            .collect()
    }

    /// Leg 3 (#517): the most-recent review for `(coord_repo, issue_num)` with a
    /// verdict NOT in `prior`, as `(review_aid, verdict)` — a newly-reported
    /// verdict to route on.
    pub(crate) fn latest_new_verdict_for(
        &self,
        coord_repo: &str,
        issue_num: u64,
        prior: &std::collections::HashSet<String>,
    ) -> Option<(String, String)> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue_num && a.repo == coord_repo)
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .filter(|a| !prior.contains(&a.id))
            .filter_map(|a| {
                let v = a.review_verdict.as_deref().filter(|v| !v.is_empty())?;
                Some((a, v.to_string()))
            })
            .max_by(|(a, _), (b, _)| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|(a, v)| (a.id.clone(), v))
    }

    /// Leg 3 (#517): the most-recent `type="review"` assignment id for the
    /// SELECTED issue whose verdict was request-changes (the one a `--fix-of`
    /// session addresses), or `None`.
    pub(crate) fn selected_request_changes_review_aid(&self) -> Option<String> {
        let (repo, issue_key) = self.selected_issue_repo_and_key()?;
        self.request_changes_review_aid_for(&repo, issue_key.1)
    }

    pub(crate) fn request_changes_review_aid_for(
        &self,
        coord_repo: &str,
        issue_num: u64,
    ) -> Option<String> {
        // #602: only the LATEST review counts.  A request-changes review that a
        // LATER review approved — or that the issue has since moved past to the
        // Test gate — is RESOLVED; returning its stale aid would brief a fix
        // worker with the old reviewer findings (the worker "fixes the
        // reviewer's concerns", finds them already done, and the operator's
        // actual test feedback never reaches it — the fail→fix misroute).  So
        // take the most-recent review for this issue and surface it ONLY when
        // ITS verdict is still request-changes; otherwise return None so the
        // fail→fix path falls through to the failed WORK id (→ test_reason).
        let latest = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue_num && a.repo == coord_repo)
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })?;
        if latest.review_verdict.as_deref() == Some("request-changes") {
            Some(latest.id.clone())
        } else {
            None
        }
    }

    /// #648: true when launching a `--fix-of` for this issue would brief the fix
    /// worker BLIND — i.e. the review the fix actually targets (the LATEST
    /// request-changes review, the same id `request_changes_review_aid_for`
    /// resolves and `--fix-of` consumes) has no findings.
    ///
    /// Gates ONLY on that latest review.  The old gate used `.any(...
    /// review_findings.is_none())` across ALL request-changes reviews, so a
    /// stale OLDER empty review (e.g. the first review's capture missed and was
    /// recovered only on a re-review) forced the #587 manual-entry dialog even
    /// when the current review already carried full findings — and confirming
    /// that dialog overwrote the good findings via `coord set-review-findings`.
    pub(crate) fn fix_review_needs_findings_capture(&self, coord_repo: &str, issue_num: u64) -> bool {
        match self.request_changes_review_aid_for(coord_repo, issue_num) {
            Some(aid) => !self.review_assignment_has_findings(&aid),
            None => false,
        }
    }

    /// #803: compute the model tier that will be used for the next interactive
    /// `--fix-of` session for the given issue.
    ///
    /// Mirrors Python's `_fix_model_for_iteration(cfg, next_iteration)`.
    /// Returns `None` when the models config snapshot is absent from
    /// `board_meta` (pre-#803 coordinator; the fix will use whatever default
    /// `coord assign` resolves, same as before this feature).
    pub(crate) fn fix_model_for_issue(&self, coord_repo: &str, issue_num: u64) -> Option<String> {
        let models = self.data.pipeline_models.as_ref()?;
        // The latest work assignment for this issue carries the current
        // review_iteration.  next_iteration = review_iteration + 1.
        let latest_work = self
            .data
            .assignments
            .iter()
            .filter(|a| {
                a.repo == coord_repo
                    && a.issue_number == issue_num
                    && a.assignment_type.as_deref() == Some("work")
            })
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })?;
        let next_iteration = latest_work.review_iteration + 1;
        fix_model_for_iteration(models, next_iteration)
    }

    /// True when the embedded Terminal pane for issue `key` is showing a still-
    /// LIVE (not exited) interactive session — the operator hasn't `/exit`ed
    /// yet.  All three board-driven stage detectors gate on this so a freshly-
    /// recorded board state (work done / review verdict / test verdict) never
    /// pops its confirm prompt over — and, on confirm, REPLACES the PTY of — a
    /// session the operator is still attending (#602).  `key` is the issue_key
    /// `detail_terminal_sessions`, `armed_for_*`, and the launch path all share.
    pub(crate) fn session_pane_live(&self, key: &(String, u64)) -> bool {
        self.detail_terminal_sessions
            .get(key)
            .is_some_and(|s| !s.is_exited())
    }

    /// Leg 2 (#517): scan armed interactive-work sessions for one the board now
    /// shows finished (a new done-with-branch work aid, not yet tested) and, if
    /// found, raise the "start testing?" confirm prompt.  Test precedes Review
    /// now, so a completed work/fix advances to the smoke-test stage first.
    /// Strictly board-driven — never reads the session TTY (ToS §3.7 / #437).
    /// Returns `true` when a prompt was raised.
    pub(crate) fn detect_completed_interactive_work(&mut self) -> bool {
        // One stage prompt at a time; don't stack over an open one.
        if self.stage_prompt_open() || self.armed_for_auto_review.is_empty() {
            return false;
        }
        let fire = self.armed_for_auto_review.iter().find_map(|(key, armed)| {
            // #602: don't preempt a still-attended session.  A fix/work agent
            // reports `done` (git push / report-result) while the claude session
            // is still wrapping up, so the completion lands BEFORE the pane
            // exits — firing now would pop "start review?" over the live pane.
            // Defer (leave the arm in place) until the operator has /exit'ed.
            // #722: extend to remote fleet sessions — `issue_has_live_session_for_repo`
            // covers tmux sessions on any machine, not just the embedded pane, and
            // filters by repo to avoid false positives in multi-repo setups.
            if self.session_pane_live(key) || self.issue_has_live_session_for_repo(armed.issue_num, &armed.coord_repo) {
                return None;
            }
            let aid = self.completed_work_aid_for(&armed.coord_repo, armed.issue_num)?;
            // Only a genuinely new completion (the just-launched work/fix), and
            // only when that work hasn't already progressed past the test stage.
            if armed.prior_done_ids.contains(&aid) {
                return None;
            }
            // Test-before-Review reorder: don't re-offer testing for work that
            // already carries a test verdict, nor for work already handed to a
            // review (a review can only exist post-test now).  A freshly-pushed
            // fix has neither, so the re-test → re-review loop still fires.
            if self.test_state_for_aid(&aid).is_some() {
                return None;
            }
            if self.work_has_review(&armed.coord_repo, armed.issue_num, &aid) {
                return None;
            }
            Some(key.clone())
        });
        if let Some(key) = fire {
            if let Some(armed) = self.armed_for_auto_review.remove(&key) {
                // Drop terminal focus so the confirm prompt owns Enter rather
                // than the live shell that ran `coord assign`.
                self.detail_terminal_focused = false;
                // Test-before-Review reorder: a finished work/fix advances to
                // the smoke-test stage first (was: straight to review).
                self.pending_stage_launch = Some(PendingStageLaunch {
                    coord_repo: armed.coord_repo,
                    repo_slug: armed.repo_slug,
                    issue_num: armed.issue_num,
                    kind: StageLaunchKind::Test,
                });
                return true;
            }
        }
        false
    }

    /// Leg 2 (#517): the operator confirmed the auto-advance prompt — select the
    /// issue's pipeline row, open its Terminal tab, and launch the interactive
    /// review (reuses the manual `--review-of` launch path).
    pub(crate) fn confirm_auto_review(&mut self) {
        let Some(p) = self.pending_auto_review.as_ref() else {
            return;
        };
        // #722: belt-and-suspenders — never launch the next stage while a
        // remote tmux session for this issue is still live.  The detector
        // already defers, but this guards the race where a session appears
        // after the offer was raised, or where the live-blocking dialog drove
        // an Enter key-press.  Keep the prompt up so it fires automatically
        // once the session closes.
        if self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo) {
            let issue_num = p.issue_num;
            self.push_toast(
                "Start review",
                &format!(
                    "A live session for #{issue_num} is still open — reattach \
                     (right-click → Reattach to live session) and type /exit, \
                     then the review offer will re-appear automatically.",
                ),
                ToastSeverity::Warning,
            );
            return;
        }
        let Some(p) = self.pending_auto_review.take() else {
            return;
        };
        let idx = self
            .pipeline_issues
            .iter()
            .position(|iss| iss.repo_slug == p.repo_slug && iss.number == p.issue_num);
        let Some(idx) = idx else {
            self.push_toast(
                "Start review",
                &format!(
                    "Could not find {} #{} in the pipeline to start its review.",
                    p.coord_repo, p.issue_num,
                ),
                ToastSeverity::Warning,
            );
            return;
        };
        self.pipeline_sel = Some(idx);
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Pipeline);
        self.pipeline_detail_tab = PipelineDetailTab::Terminal;
        self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Review);
    }

    /// Leg 2/3 (#517): true when any auto-advance stage prompt is already up.
    /// The board-driven detectors check this so only one fires per tick.
    pub(crate) fn stage_prompt_open(&self) -> bool {
        self.pending_auto_review.is_some()
            || self.pending_stage_launch.is_some()
            || self.pending_rework.is_some()
            || self.pending_test_fix.is_some()
            || self.pending_merge.is_some()
            || self.pending_test_mode_choice.is_some()
            || self.pending_fix_force_confirm.is_some()
    }

    /// True when the Pipeline detail Terminal tab is showing a LIVE (not
    /// exited) session for the selected issue.  Guards the global Esc/q quit
    /// so an accidental keystroke can't kill the TUI out from under a running
    /// interactive `claude` session.
    pub(crate) fn terminal_tab_has_live_session(&self) -> bool {
        if self.active_view != SidebarView::Pipeline
            || self.pipeline_detail_tab != PipelineDetailTab::Terminal
        {
            return false;
        }
        let Some(key) = self.selected_issue_key() else {
            return false;
        };
        self.detail_terminal_sessions
            .get(&key)
            .map(|s| !s.is_exited())
            .unwrap_or(false)
    }

    /// True when review assignment `aid` already carries agent-reported
    /// findings in the DB — a non-empty `body` in the `review_findings` JSON
    /// that `coord report-result` persists when the review session ends.
    /// When true the #587 manual-entry rework dialog is redundant (the fix
    /// worker is briefed from these findings), so `detect_review_verdict`
    /// skips prompting and the operator can just `/exit` the review session.
    pub(crate) fn review_assignment_has_findings(&self, aid: &str) -> bool {
        self.data
            .assignments
            .iter()
            .find(|a| a.id == aid)
            .and_then(|a| a.review_findings.as_deref())
            .and_then(|raw| serde_json::from_str::<serde_json::Value>(raw).ok())
            .and_then(|v| v.get("body").and_then(|b| b.as_str()).map(str::to_string))
            .map(|body| !body.trim().is_empty())
            .unwrap_or(false)
    }

    /// Leg 3 (#517): scan armed interactive reviews for a freshly-reported
    /// verdict and route it.  request-changes → raise the rework confirm
    /// prompt; approve → offer the interactive merge agent (the branch was
    /// already smoke-tested — Test precedes Review).  Strictly board-driven
    /// (verdict comes from `coord report-result`, never the session TTY).
    /// Returns `true` when it raised a prompt or toast.
    pub(crate) fn detect_review_verdict(&mut self) -> bool {
        if self.stage_prompt_open() || self.armed_for_verdict.is_empty() {
            return false;
        }
        let found = self.armed_for_verdict.iter().find_map(|(key, armed)| {
            // #602: wait until the operator has `/exit`ed the review session
            // pane before routing — don't preempt a still-attended session (the
            // dialog used to pop mid-review).  No local pane (e.g. a remote
            // review) routes as soon as the verdict lands.
            // #722: extend to remote fleet sessions; filter by repo to avoid
            // false positives in multi-repo setups.
            if self.session_pane_live(key) || self.issue_has_live_session_for_repo(armed.issue_num, &armed.coord_repo) {
                return None;
            }
            let (review_aid, verdict) = self.latest_new_verdict_for(
                &armed.coord_repo,
                armed.issue_num,
                &armed.prior_verdicted_ids,
            )?;
            Some((key.clone(), review_aid, verdict))
        });
        let Some((key, review_aid, verdict)) = found else {
            return false;
        };
        let Some(armed) = self.armed_for_verdict.remove(&key) else {
            return false;
        };
        self.detail_terminal_focused = false;
        if verdict == "request-changes" {
            if self.review_assignment_has_findings(&review_aid) {
                // The review agent already wrote its findings to the DB
                // (`coord report-result`), so there's nothing for the operator
                // to type.  Offer a one-key "start fix" launch (no findings
                // input) instead of the #587 manual-entry dialog.
                self.pending_stage_launch = Some(PendingStageLaunch {
                    coord_repo: armed.coord_repo,
                    repo_slug: armed.repo_slug,
                    issue_num: armed.issue_num,
                    kind: StageLaunchKind::Fix,
                });
            } else {
                // No agent-reported findings → keep the #587 safety net so the
                // fix worker isn't briefed blind: prompt the operator to type
                // what the reviewer flagged.
                self.pending_rework = Some(PendingRework {
                    coord_repo: armed.coord_repo,
                    repo_slug: armed.repo_slug,
                    issue_num: armed.issue_num,
                    findings: String::new(),
                });
            }
        } else {
            // approve (incl. approved-with-nits): the branch was already
            // smoke-tested before this review (Test precedes Review now), so
            // offer the interactive merge agent directly — the guided rebase →
            // merge flow (leg 3c).
            self.pending_merge = Some(PendingMerge {
                coord_repo: armed.coord_repo,
                repo_slug: armed.repo_slug,
                issue_num: armed.issue_num,
            });
        }
        true
    }

    /// Leg 3 (#517 / #587): the operator confirmed the rework prompt — select
    /// the issue's row, open its Terminal tab, and launch the interactive
    /// `--fix-of` session (continues the reviewed branch, briefed with the
    /// findings, incremental re-review on completion).
    ///
    /// #587: validates that findings are non-empty before proceeding; if
    /// empty, shows a warning toast and keeps the dialog open.  When findings
    /// are provided, persists them via `coord set-review-findings` so the fix
    /// worker's `_load_review_findings` DB cache hit gives it concrete feedback.
    pub(crate) fn confirm_rework(&mut self) {
        // #587: findings are required — block confirm when empty.
        let findings = self.pending_rework
            .as_ref()
            .map(|p| p.findings.trim().to_string())
            .unwrap_or_default();
        if findings.is_empty() {
            self.push_toast(
                "Findings required",
                "Type what the reviewer flagged above — \
                 the fix worker needs this to know what to address.",
                ToastSeverity::Warning,
            );
            return;  // Keep the dialog open
        }

        let Some(p) = self.pending_rework.take() else {
            return;
        };

        // #587: persist findings via `coord set-review-findings` so the DB
        // cache is warm before the fix worker reads `_load_review_findings`.
        // Run asynchronously via CommandRunner — the DB write completes in
        // milliseconds, long before the fix session initialises.
        let review_aid = self.request_changes_review_aid_for(&p.coord_repo, p.issue_num);
        if let Some(ref aid) = review_aid {
            let _ = self.command_runner.spawn_queued(&[
                "set-review-findings",
                aid,
                "--findings",
                &findings,
            ]);
        }

        let idx = self
            .pipeline_issues
            .iter()
            .position(|iss| iss.repo_slug == p.repo_slug && iss.number == p.issue_num);
        let Some(idx) = idx else {
            self.push_toast(
                "Start fix",
                &format!(
                    "Could not find {} #{} in the pipeline to start its fix.",
                    p.coord_repo, p.issue_num,
                ),
                ToastSeverity::Warning,
            );
            return;
        };
        self.pipeline_sel = Some(idx);
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Pipeline);
        self.pipeline_detail_tab = PipelineDetailTab::Terminal;
        // #587: bypass the secondary "no findings captured" gate — we just
        // wrote the findings above so there is no need to re-prompt.
        self.rework_bypass = true;
        self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Fix);
        self.rework_bypass = false;
    }

    /// The operator confirmed the post-review stage offer raised (after the
    /// review session exited) by `detect_review_verdict` — select the issue's
    /// pipeline row, open its Terminal tab, and launch the next interactive
    /// stage: a `--fix-of` (request-changes, findings already in the DB) or a
    /// `--smoke-of` testing session (approved).
    pub(crate) fn confirm_stage_launch(&mut self) {
        let Some(p) = self.pending_stage_launch.as_ref() else {
            return;
        };
        // #722: same live-session gate as confirm_auto_review, filtered by repo
        // to avoid false positives in multi-repo setups.
        if self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo) {
            let issue_num = p.issue_num;
            self.push_toast(
                "Start stage",
                &format!(
                    "A live session for #{issue_num} is still open — reattach \
                     (right-click → Reattach to live session) and type /exit first.",
                ),
                ToastSeverity::Warning,
            );
            return;
        }
        let Some(p) = self.pending_stage_launch.take() else {
            return;
        };
        let idx = self
            .pipeline_issues
            .iter()
            .position(|iss| iss.repo_slug == p.repo_slug && iss.number == p.issue_num);
        let Some(idx) = idx else {
            self.push_toast(
                "Start stage",
                &format!(
                    "Could not find {} #{} in the pipeline to start its next stage.",
                    p.coord_repo, p.issue_num,
                ),
                ToastSeverity::Warning,
            );
            return;
        };
        self.pipeline_sel = Some(idx);
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Pipeline);
        self.pipeline_detail_tab = PipelineDetailTab::Terminal;
        match p.kind {
            StageLaunchKind::Fix => {
                // Findings are already in the DB (that's why this offer, not the
                // #587 capture dialog, was raised) — bypass the secondary
                // no-findings gate and launch the fix directly.
                self.rework_bypass = true;
                self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Fix);
                self.rework_bypass = false;
            }
            StageLaunchKind::Test => {
                self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Test);
            }
        }
    }

    /// #685: arm the test-mode choice dialog in SetOnly mode (right-click flip).
    pub(crate) fn arm_set_test_mode_for_selected(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            return false;
        };
        let current_mode = issue
            .all_labels
            .iter()
            .find(|l| l.starts_with("test-mode:"))
            .map(|l| l.trim_start_matches("test-mode:").to_string());
        self.pending_test_mode_choice = Some(PendingTestModeChoice {
            coord_repo,
            issue_num: issue.number,
            action: TestModeChoiceAction::SetOnly,
            current_mode,
            machine_name: None,
            model_override: None,
        });
        true
    }

    /// #266: Drop the selected pipeline row back to Backlog by spawning
    /// `coord backlog <repo> <issue>`, which strips the `status:ready` /
    /// `status:refining` label.  Wired to the "Drop to backlog" context-menu
    /// action, enabled only for New and In-progress *idle* rows.
    pub(crate) fn drop_selected_to_backlog(&mut self) -> bool {
        use crate::commands::SpawnQueuedOutcome;
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            return false;
        };
        let num_str = issue.number.to_string();
        // Pipeline membership is the `coord` label, so dropping a card to
        // Backlog must REMOVE it — stripping `status:*` alone (the Board's
        // `coord backlog`) leaves the card in Pipeline:New.  `coord untrack`
        // removes `coord` + any `status:*`, so the issue leaves the Pipeline
        // and lands in the Board's Backlog.  Inverse of `track` (Send to
        // Pipeline).
        let outcome = self
            .command_runner
            .spawn_queued(&["untrack", &coord_repo, &num_str]);
        match outcome {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Drop to backlog",
                    &format!(
                        "#{}: queued — will run after the current command.",
                        issue.number
                    ),
                    ToastSeverity::Info,
                );
                self.maybe_kick_pipeline_loader();
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Drop to backlog",
                    &format!("#{}: removing from Pipeline → Backlog…", issue.number),
                    ToastSeverity::Info,
                );
                // Refresh so the card disappears without waiting for the
                // auto-refresh (mirrors Send to Pipeline).
                self.maybe_kick_pipeline_loader();
            }
        }
        true
    }

    /// #1500: "Mark ready" — stage the selected New row into
    /// In-progress:`ready` by setting `status:queued`, with no dispatch. On
    /// an epic row this fans out to the epic itself plus every non-Done
    /// child (`epic_child_kept_in_progress` — the same "still worth
    /// showing under In-progress" filter the nested-child renderer already
    /// applies), so the whole epic renders together with its remaining
    /// children badged `ready`.
    pub(crate) fn mark_selected_ready(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            return false;
        };
        let mut targets = vec![issue.number];
        if self.epic_children_for(&issue).is_some() {
            let rows = self.epic_child_rows_for(&coord_repo, issue.number);
            targets.extend(
                rows.iter()
                    .filter(|r| Self::epic_child_kept_in_progress(r))
                    .map(|r| r.number),
            );
        }
        let handled = self.apply_staging_label_change(&coord_repo, &targets, "queue", "Mark ready");
        if handled {
            self.maybe_kick_pipeline_loader();
        }
        handled
    }

    /// #1500: "Unmark ready" — the reverse of "Mark ready": strips
    /// `status:queued` from the selected row, returning it to New. On an
    /// epic row this also strips the label from every child that currently
    /// carries it (not just the non-Done ones — clearing the marker
    /// everywhere it was set is what makes "returns to New" actually true;
    /// a `status:queued` child left behind would keep the epic classified
    /// as In-progress via `epic_lifecycle_section`'s own-or-child check).
    pub(crate) fn unmark_selected_ready(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            return false;
        };
        let mut targets = vec![issue.number];
        if let Some(children) = self.epic_children_for(&issue) {
            targets.extend(self.queued_child_numbers(&coord_repo, children));
        }
        let handled =
            self.apply_staging_label_change(&coord_repo, &targets, "unqueue", "Unmark ready");
        if handled {
            self.maybe_kick_pipeline_loader();
        }
        handled
    }

    /// #1500: shared batch label-change core for "Mark ready" / "Unmark
    /// ready". `issue_numbers` is the epic + non-Done-children fan-out (or
    /// a single-element slice for a plain issue).
    ///
    /// Prefers the direct `apply_issue_labels_remote` POST seam (#1012) —
    /// issued once per target — over N serial `coord <subcommand>`
    /// subprocesses, so a multi-issue epic batch completes as one
    /// operation instead of trickling through the command queue. Emits
    /// exactly ONE toast summarizing the whole batch and explicitly calls
    /// out any target that failed rather than silently applying a partial
    /// batch. Falls back to queuing one `coord <subcommand>` subprocess per
    /// target when no board service is configured.
    ///
    /// `subcommand` must be one `label_change_for_subcommand` recognizes
    /// (`"queue"` / `"unqueue"`) — the add/remove label sets come from that
    /// shared table rather than being passed in separately, so this can
    /// never drift from the Python `coord queue`/`coord unqueue` label sets
    /// acceptance-tested against it.
    fn apply_staging_label_change(
        &mut self,
        coord_repo: &str,
        issue_numbers: &[u64],
        subcommand: &'static str,
        toast_title: &str,
    ) -> bool {
        let Some(&first) = issue_numbers.first() else {
            return false;
        };
        let Some((add, remove)) = label_change_for_subcommand(subcommand) else {
            return false;
        };
        if let Some((url, token)) = resolve_board_service() {
            let repo_github = self
                .data
                .pipeline_repos
                .iter()
                .find(|(name, _)| name == coord_repo)
                .map(|(_, gh)| gh.as_str());
            let mut changed = 0usize;
            let mut failed: Vec<(u64, String)> = Vec::new();
            for &num in issue_numbers {
                match apply_issue_labels_remote(
                    &url,
                    token.as_deref(),
                    coord_repo,
                    num,
                    add,
                    remove,
                    repo_github,
                ) {
                    Ok((_labels, did_change)) => {
                        if did_change {
                            changed += 1;
                        }
                    }
                    Err(e) => failed.push((num, e)),
                }
            }
            let total = issue_numbers.len();
            if failed.is_empty() {
                let body = if total == 1 {
                    format!(
                        "#{}: {}.",
                        first,
                        if changed > 0 {
                            "updated"
                        } else {
                            "no change (already set)"
                        }
                    )
                } else {
                    format!(
                        "#{} + {} child issue(s): {} updated, {} already set.",
                        first,
                        total - 1,
                        changed,
                        total - changed
                    )
                };
                self.push_toast(toast_title, &body, ToastSeverity::Success);
            } else {
                let failed_list = failed
                    .iter()
                    .map(|(n, e)| format!("#{} ({})", n, e))
                    .collect::<Vec<_>>()
                    .join(", ");
                let body = format!(
                    "{}/{} updated; {} failed: {}",
                    changed,
                    total,
                    failed.len(),
                    failed_list
                );
                self.push_toast(toast_title, &body, ToastSeverity::Error);
            }
        } else {
            for &num in issue_numbers {
                let num_str = num.to_string();
                let _ = self.command_runner.spawn_queued(&[subcommand, coord_repo, &num_str]);
            }
            let body = if issue_numbers.len() == 1 {
                format!("#{}: queued `coord {}`…", first, subcommand)
            } else {
                format!(
                    "#{} + {} child issue(s): queued `coord {}`…",
                    first,
                    issue_numbers.len() - 1,
                    subcommand
                )
            };
            self.push_toast(toast_title, &body, ToastSeverity::Info);
        }
        true
    }

    /// #685: confirm the test-mode choice dialog.
    ///
    /// 1. Queues `coord set-test-mode <repo_slug> <issue> <mode>` to persist
    ///    the label on the GitHub issue.
    /// 2. For `DispatchWork`, queues the dispatch command so that it runs
    ///    automatically after the label update completes.
    pub(crate) fn confirm_test_mode_choice(&mut self, choice: PendingTestModeChoice, mode: &str) {
        use crate::commands::SpawnQueuedOutcome;

        // Step 1: persist the label.
        // coord set-test-mode expects the LOCAL coordinator repo name (from
        // coordinator.yml), not the GitHub slug — use coord_repo, not repo_slug.
        let set_mode_cmd = vec![
            "set-test-mode".to_string(),
            choice.coord_repo.clone(),
            choice.issue_num.to_string(),
            mode.to_string(),
        ];
        let set_cmd_refs: Vec<&str> = set_mode_cmd.iter().map(|s| s.as_str()).collect();
        let label_outcome = self.command_runner.spawn_queued(&set_cmd_refs);

        // Step 2: dispatch work if requested (queues after the label update).
        let dispatched = match choice.action {
            TestModeChoiceAction::DispatchWork => {
                self.dispatch_pipeline_work_with_mode(
                    &choice.coord_repo.clone(),
                    choice.issue_num,
                    choice.machine_name.as_deref(),
                    choice.model_override.as_deref(),
                )
            }
            TestModeChoiceAction::SetOnly => {
                // Label-only flip (right-click "Set test mode") — no dispatch.
                true
            }
        };

        let mode_verb = if mode == "smoke" { "smoke (pause for testing)" } else { "auto (fully automated)" };
        if dispatched || matches!(choice.action, TestModeChoiceAction::SetOnly) {
            match label_outcome {
                SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued => {
                    self.pipeline_status = Some((
                        format!(
                            "#{} test mode → {} (label updating…)",
                            choice.issue_num, mode_verb,
                        ),
                        Instant::now(),
                    ));
                }
                SpawnQueuedOutcome::Deduped => {}
            }
        }
    }

    /// #685: scan for completed headless (non-interactive) work assignments
    /// whose issue carries the `test-mode:smoke` label and no test verdict yet.
    ///
    /// When found, raises a `PendingStageLaunch { kind: Test }` so the TUI
    /// offers the interactive smoke agent (same UX as interactive Work → Test).
    /// Tracks already-offered IDs in `offered_smoke_for_headless_work` so the
    /// offer fires exactly once per work assignment.
    pub(crate) fn detect_headless_smoke_work_done(&mut self) -> bool {
        if self.stage_prompt_open() {
            return false;
        }
        // Build a quick lookup: coord_repo → label list for pipeline issues.
        // We match by (repo_slug, issue_number) to check the test-mode label.
        let issue_label_map: std::collections::HashMap<(String, u64), Vec<String>> = self
            .pipeline_issues
            .iter()
            .map(|iss| ((iss.repo_slug.clone(), iss.number), iss.all_labels.clone()))
            .collect();

        let fire = self.data.assignments.iter().find_map(|a| {
            // Must be a completed (done) non-interactive work assignment.
            if a.status != "done" {
                return None;
            }
            if a.assignment_type.as_deref().unwrap_or("work") != "work" {
                return None;
            }
            if a.is_interactive {
                return None; // interactive work is handled by detect_completed_interactive_work
            }
            let aid = &a.id;
            if self.offered_smoke_for_headless_work.contains(aid) {
                return None; // already offered
            }
            if a.test_state.is_some() {
                return None; // test verdict already recorded
            }
            // Check that this issue has the test-mode:smoke label.
            // We match by (repo_slug, issue_number).
            let repo_slug = self
                .data
                .pipeline_repos
                .iter()
                .find(|(coord_name, _)| coord_name == &a.repo)
                .map(|(_, slug)| slug.clone());
            let slug = repo_slug.as_deref().unwrap_or(&a.repo);
            let labels = issue_label_map.get(&(slug.to_string(), a.issue_number));
            let is_smoke_mode = labels
                .map(|lbls| lbls.iter().any(|l| l == "test-mode:smoke"))
                .unwrap_or(false);
            if !is_smoke_mode {
                return None;
            }
            // Don't fire over a live pane (same guard as detect_completed_interactive_work).
            if self.session_pane_live(&(slug.to_string(), a.issue_number)) {
                return None;
            }
            // Check no review has been dispatched yet.
            let coord_repo = self
                .data
                .pipeline_repos
                .iter()
                .find(|(_, rs)| rs == slug)
                .map(|(name, _)| name.clone())
                .unwrap_or_else(|| a.repo.clone());
            if self.work_has_review(&coord_repo, a.issue_number, aid) {
                return None;
            }
            Some((aid.clone(), coord_repo, slug.to_string(), a.issue_number))
        });

        if let Some((aid, coord_repo, repo_slug, issue_num)) = fire {
            self.offered_smoke_for_headless_work.insert(aid);
            self.detail_terminal_focused = false;
            self.pending_stage_launch = Some(PendingStageLaunch {
                coord_repo,
                repo_slug,
                issue_num,
                kind: StageLaunchKind::Test,
            });
            return true;
        }
        false
    }

    /// #486 Leg 4: fleet machines that can run `repo`, ordered local-first,
    /// then reachable, then by name.  Drives the machine picker: a machine
    /// qualifies when the repo is in its configured `repos` list (the static
    /// coordinator.yml mirror — populated even for currently-unreachable
    /// machines, so a momentarily-down agent is still offered).
    pub(crate) fn fleet_machines_for_repo(&self, repo: &str) -> Vec<MachinePickEntry> {
        let local = self.data.local_machine.clone();
        let mut v: Vec<MachinePickEntry> = self
            .data
            .machines
            .iter()
            .filter(|m| m.repos.iter().any(|r| r == repo))
            .map(|m| MachinePickEntry {
                name: m.name.clone(),
                host: m.host.clone(),
                reachable: m.reachable,
                is_local: m.name == local,
            })
            .collect();
        v.sort_by(|a, b| {
            b.is_local
                .cmp(&a.is_local)
                .then(b.reachable.cmp(&a.reachable))
                .then_with(|| a.name.cmp(&b.name))
        });
        v
    }

    /// #467/#486: front door for an interactive launch from a board card.
    ///
    /// ALL four modes (Work / Plan / Review / Fix) can target any fleet
    /// machine: when more than one machine can run the issue's repo, arm the
    /// machine picker; the operator's pick then dispatches over ssh+tmux via
    /// `coord assign <machine> --interactive …`.  Remote Work/Plan/Fix run in a
    /// remote worktree whose commits are pushed back on exit
    /// (`finalize_remote_interactive_exit`, #486d); remote Review is read-only.
    /// With a single capable machine there is no choice, so it launches
    /// directly (local TTY when that machine is this one).
    /// Assignment id of a live interactive tmux session for the
    /// currently-selected issue (matched precisely by repo + issue number),
    /// if one exists.  When present, the launch action reattaches rather than
    /// starting fresh — see [`Self::launch_interactive_session_for_selected_issue`].
    pub(crate) fn selected_issue_live_session_id(&self) -> Option<String> {
        let (repo, issue_key) = self.selected_issue_repo_and_key()?;
        let issue_num = issue_key.1;
        // #514: only reattachable while the board still considers the session
        // running — a finalized assignment means the session was torn down, so a
        // stale live_tmux_sessions entry must NOT drive a dead reattach.
        //
        // #601 follow-up: there are often SEVERAL discovered sessions for one
        // issue (lingering done smoke/review tmux sessions alongside the live
        // one). Pick the matching session whose board assignment is still
        // running — NOT merely the first match (`.find` then-check), which could
        // be a finalized session, leaving this `None` so the reattach
        // short-circuit dies and the launch falls through to the machine picker.
        self.live_tmux_sessions
            .iter()
            .filter(|s| {
                s.issue_number == Some(issue_num)
                    && s.repo_name.as_deref() == Some(repo.as_str())
            })
            .map(|s| s.assignment_id.clone())
            .find(|aid| self.session_assignment_is_running(aid))
    }

    /// Assignment id of **any** discovered tmux session for the selected issue
    /// (matched precisely by repo + issue number), preferring a running session
    /// over a zombie (finalized-assignment-but-live-tmux) session.
    ///
    /// Used by the dedicated "Reattach to live session" menu action for the
    /// zombie case (#727): when the board assignment is already `done`/`failed`
    /// but the tmux session is still alive (the human left without finalising),
    /// the classifier shows the row as **In-progress:Live** and the right-click
    /// menu must offer Reattach.  `selected_issue_live_session_id` returns `None`
    /// in that case (running-only), so this wider lookup is the correct resolver.
    ///
    /// Falls through to `selected_issue_live_session_id` when a running session
    /// exists, so the caller never needs to choose between the two.
    pub(crate) fn selected_issue_any_session_id(&self) -> Option<String> {
        let (repo, issue_key) = self.selected_issue_repo_and_key()?;
        let issue_num = issue_key.1;
        let matching: Vec<_> = self
            .live_tmux_sessions
            .iter()
            .filter(|s| {
                s.issue_number == Some(issue_num)
                    && s.repo_name.as_deref() == Some(repo.as_str())
            })
            .collect();
        // Prefer a running session (same semantics as selected_issue_live_session_id).
        matching
            .iter()
            .find(|s| self.session_assignment_is_running(&s.assignment_id))
            .or_else(|| matching.first())
            .map(|s| s.assignment_id.clone())
    }

    /// True when the session identified by `assignment_id` is still
    /// *reattachable* — the board has its assignment as `running`.  A
    /// finalized assignment (done/merged/advisory/failed) means the tmux
    /// session was torn down on finalize, so a lingering `live_tmux_sessions`
    /// entry (the discovery sweep refreshes only periodically) must not drive
    /// a `coord reattach` against a dead session → "session not alive" (#514).
    pub(crate) fn session_assignment_is_running(&self, assignment_id: &str) -> bool {
        self.data
            .assignments
            .iter()
            .any(|a| a.id == assignment_id && a.status == "running")
    }

    /// The board assignment type for `aid` ("work" / "review" / "smoke" /
    /// "merge" / …), or `None` if the assignment isn't on the in-memory board.
    pub(crate) fn assignment_type_of(&self, aid: &str) -> Option<&str> {
        self.data
            .assignments
            .iter()
            .find(|a| a.id == aid)
            .and_then(|a| a.assignment_type.as_deref())
    }

    /// The assignment id of a still-running tmux session for `(issue_num, repo)`
    /// that this launch `mode` may reattach to — gated on the running session's
    /// assignment TYPE matching the type this mode produces (Work/Plan/Fix →
    /// "work", Review → "review", Test → "smoke", Merge → "merge").
    ///
    /// The type gate is the fix for the "Start fix dropped me back in a review"
    /// kink: without it the reattach matched ANY running session for the issue,
    /// so clicking "Start fix" while a review session was still running ran
    /// `coord reattach <review_aid>` (back into the review + its verdict prompt)
    /// instead of dispatching a fresh `--fix-of`.  Troubleshoot never reattaches
    /// (#569: always a fresh diagnostic).
    pub(crate) fn reattachable_session_aid(
        &self,
        issue_num: u64,
        repo: &str,
        mode: InteractiveLaunchMode,
    ) -> Option<String> {
        if matches!(
            mode,
            InteractiveLaunchMode::Troubleshoot
                | InteractiveLaunchMode::Chat
                | InteractiveLaunchMode::Audit
        ) {
            return None;
        }
        let want_type = interactive_mode_assignment_type(mode);
        self.live_tmux_sessions
            .iter()
            .filter(|s| {
                s.issue_number == Some(issue_num) && s.repo_name.as_deref() == Some(repo)
            })
            .map(|s| s.assignment_id.clone())
            .find(|aid| {
                self.session_assignment_is_running(aid)
                    && self.assignment_type_of(aid) == Some(want_type)
            })
    }

    /// Whether `issue_number` has a *reattachable* live session (a discovered
    /// tmux session whose board assignment is still running).  Used only to
    /// relabel the right-click menu item ("Reattach" vs "Start"); the actual
    /// reattach decision matches repo + issue precisely via
    /// [`Self::selected_issue_live_session_id`].
    pub(crate) fn issue_has_live_session(&self, issue_number: u64) -> bool {
        self.live_tmux_sessions
            .iter()
            .filter(|s| s.issue_number == Some(issue_number))
            .any(|s| self.session_assignment_is_running(&s.assignment_id))
    }

    /// Whether `issue_number` has *any* discovered tmux session — regardless of
    /// board assignment status.  Used to detect "zombie" sessions: a tmux session
    /// that is still alive (and classified as **In-progress:Live** by
    /// `issue_session_is_live`) even though the board assignment has already
    /// been finalized (`done`/`failed`) or is absent.
    ///
    /// Mirrors the liveness predicate used by the **Live classifier**
    /// (`issue_session_is_live`), so the menu gate and the classifier can never
    /// disagree about whether a session exists.  The reattach *target* is resolved
    /// repo-precisely by [`Self::selected_issue_any_session_id`].
    pub(crate) fn issue_has_any_discovered_session(&self, issue_number: u64) -> bool {
        self.live_tmux_sessions
            .iter()
            .any(|s| s.issue_number == Some(issue_number))
    }

    /// Like [`Self::issue_has_any_discovered_session`] but also matches
    /// `repo_name`, so a zombie session for repo-b/#N does not falsely
    /// surface a "Reattach" item on repo-a/#N's right-click menu.
    pub(crate) fn issue_has_any_discovered_session_for_repo(
        &self,
        issue_number: u64,
        repo_name: &str,
    ) -> bool {
        self.live_tmux_sessions.iter().any(|s| {
            s.issue_number == Some(issue_number)
                && s.repo_name.as_deref() == Some(repo_name)
        })
    }

    /// Like [`Self::issue_has_live_session`] but also matches `repo_name`, so
    /// a live session for repo-b/#10 does not falsely block repo-a/#10's
    /// stage-advance.  Use this for all stage-advance gate paths where the
    /// repo is known.
    pub(crate) fn issue_has_live_session_for_repo(&self, issue_number: u64, repo_name: &str) -> bool {
        self.live_tmux_sessions
            .iter()
            .filter(|s| {
                s.issue_number == Some(issue_number)
                    && s.repo_name.as_deref() == Some(repo_name)
            })
            .any(|s| self.session_assignment_is_running(&s.assignment_id))
    }

    /// #722: when a running remote tmux session is blocking a stage-advance
    /// offer, substitute this dialog for the normal "Start …" dialog.  The
    /// operator must reattach and `/exit` the session first; the detector will
    /// re-fire automatically on the next tick once the session closes (the
    /// armed entry is not consumed while deferred).  Returns `None` when no
    /// live session is blocking — callers fall through to the normal dialog.
    pub(crate) fn live_session_blocking_dialog(&self, issue_num: u64, repo_name: &str) -> Option<Dialog> {
        if !self.issue_has_live_session_for_repo(issue_num, repo_name) {
            return None;
        }
        Some(Dialog {
            table: None,
            id: WidgetId::new("dialog:reattach-first"),
            title: StyledText::plain(format!(
                "Live session still running — #{issue_num}",
            )),
            body: vec![
                StyledText::plain(format!(
                    "An interactive session for #{issue_num} is still running.",
                )),
                StyledText::plain(
                    "Reattach to live session (right-click → Reattach to live \
                     session), type /exit, and the stage offer will re-appear \
                     automatically once the session closes."
                        .to_string(),
                ),
            ],
            buttons: vec![DialogButton {
                id: WidgetId::new("close"),
                label: "Esc  Dismiss".into(),
                is_default: true,
                is_cancel: true,
                tint: None,
            }],
            severity: Some(DialogSeverity::Warning),
            vertical_buttons: false,
            input: None,
        })
    }

    /// #628 (scope A pt.2): count live interactive sessions on a machine, so the
    /// Machines tab reflects them.  `active_count` only counts board `running`
    /// rows, which interactive sessions never create — so the tab read "idle"
    /// while a machine hosted live sessions.  We recover the machine by joining
    /// each live session to its assignment (by id) rather than threading a new
    /// field through ~20 `LiveTmuxSession` constructors.
    pub(crate) fn live_session_count_for_machine(&self, machine_name: &str) -> usize {
        self.live_tmux_sessions
            .iter()
            .filter(|s| {
                self.data
                    .assignments
                    .iter()
                    .any(|a| a.id == s.assignment_id && a.machine == machine_name)
            })
            .count()
    }

    /// #628: assemble the "all current data" briefing for a Chat about issue
    /// session — the issue's title + body, then the same board snapshot the
    /// troubleshooter gets (assignments / merge_queue / CI / stage statuses).
    /// The session fetches the discussion itself (`gh issue view --comments`)
    /// when useful, and may edit the issue via `coord issue edit` / `coord ready`.
    ///
    /// #1939: `p.body` is a verbatim copy of `OpenIssue.body`
    /// (`mod.rs:5523/5592`), which the `/board` wire now truncates to a
    /// machine-parsed residue for any open, non-epic issue
    /// (`board_wire.bound_issue_row`). A diagnostic session's briefing —
    /// described below as "the current data we have on this issue" — must
    /// not silently ship that placeholder instead of the real description,
    /// so this recovers the full text the same way the Board/Pipeline Issue
    /// tabs do (`issue_detail_cache`, #2497), falling back to a one-shot
    /// BLOCKING `GET /issue/{repo}/{number}` when no hydrated cache entry
    /// exists yet — launching an interactive session already spawns a PTY
    /// and writes a temp file, so the extra latency here is an acceptable
    /// one-time cost for a briefing that must be correct, not a per-frame
    /// render path where blocking would be a regression.
    pub(crate) fn chat_briefing(&self, coord_repo: &str, issue_num: u64) -> String {
        let issue = self.pipeline_issues.iter().find(|p| {
            p.number == issue_num
                && p.coord_repo.as_deref().map(|r| r == coord_repo).unwrap_or(false)
        });
        let title = issue.map(|p| p.title.as_str()).unwrap_or("(title not available)");
        let raw_body = issue
            .map(|p| p.body.as_str())
            .filter(|b| !b.trim().is_empty());
        let truncated = issue.map(|p| p.body_truncated).unwrap_or(false);
        let hydrated_body: Option<String> = if truncated {
            self.issue_detail_cache
                .get(&(coord_repo.to_string(), issue_num))
                .and_then(|e| e.full.clone())
                .or_else(|| {
                    let (url, token) = resolve_board_service()?;
                    fetch_issue_body_blocking(&url, token.as_deref(), coord_repo, issue_num)
                })
        } else {
            None
        };
        let body = hydrated_body
            .as_deref()
            .or(raw_body)
            .unwrap_or("(no body recorded)");
        let board = self.troubleshoot_briefing(coord_repo, issue_num);
        format!(
            "# Chat about {coord_repo} #{issue_num}: {title}\n\n\
             ## Issue body\n{body}\n\n\
             ## Board snapshot\n{board}\n\n\
             ---\nThis is the current data we have on this issue. Fetch the \
             discussion with `gh issue view {issue_num} --comments` (read-only) \
             if useful.\n\n\
             ## Rules for this session\n\
             - **Diagnostic-only** — you have no worktree and no committed work.\n\
             - **Do NOT run `coord report-result --status done` or `--status blocked`.**\n\
               Claiming done without committed work leaves a false-good-to-go box on\n\
               the pipeline and masks the real problem (#676).\n\
             - For a stalled item: start with `coord diagnose {coord_repo} {issue_num}`\n\
               (auto-recovers phantoms, orphaned worktrees, dropped findings).\n\
               Confirm any `--reset` with the operator before running it.\n\
             - To actually fix something, surface a plan — the operator will dispatch\n\
               a proper Work session.\n"
        )
    }

    /// #569: Build a diagnostic snapshot briefing for a Troubleshoot interactive
    /// session.  Assembles the current board state — all assignments for the
    /// issue, the merge_queue entry, CI check summary, and per-stage statuses —
    /// into a structured prompt that a human-attended Claude session can use to
    /// pinpoint the stall and either unstick it or advise the exact next step.
    pub(crate) fn troubleshoot_briefing(&self, coord_repo: &str, issue_num: u64) -> String {
        // Locate the PipelineIssue for stage-status helpers and display info.
        let issue = self.pipeline_issues.iter().find(|p| {
            p.number == issue_num
                && p.coord_repo
                    .as_deref()
                    .map(|r| r == coord_repo)
                    .unwrap_or(false)
        });
        let issue_title = issue
            .map(|p| p.title.as_str())
            .unwrap_or("(title not available)");
        let repo_slug = issue
            .map(|p| p.repo_slug.as_str())
            .unwrap_or(coord_repo);

        // All assignments for this issue (all types, any status), newest last.
        let mut assignments: Vec<&Assignment> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue_num && a.repo == coord_repo)
            .collect();
        assignments.sort_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut asgn_lines = Vec::new();
        for a in &assignments {
            let atype = a.assignment_type.as_deref().unwrap_or("work");
            let branch = a.branch.as_deref().unwrap_or("(none)");
            let verdict = a.review_verdict.as_deref().unwrap_or("-");
            let test = a.test_state.as_deref().unwrap_or("-");
            asgn_lines.push(format!(
                "  {id}  type={typ}  status={st}  branch={br}  verdict={vd}  test={ts}",
                id = a.id,
                typ = atype,
                st = a.status,
                br = branch,
                vd = verdict,
                ts = test,
            ));
        }
        let assignments_text = if asgn_lines.is_empty() {
            "  (none)".to_string()
        } else {
            asgn_lines.join("\n")
        };

        // Merge queue entry for this issue.
        //
        // When `issue` is Some, `repo_slug` is the full GitHub slug (e.g.
        // "owner/api") so the exact match works.  When `issue` is None,
        // `repo_slug` falls back to `coord_repo` (the short coordinator name,
        // e.g. "api") which won't match `repo_github` ("owner/api") — also
        // accept a suffix match so the MQ entry is still found in that edge
        // case.
        let mq_entry = self.data.merge_queue.iter().find(|m| {
            m.issue_number == Some(issue_num)
                && (m.repo_github == repo_slug
                    || (issue.is_none()
                        && m.repo_github
                            .ends_with(&format!("/{}", coord_repo))))
        });
        let mq_text = match mq_entry {
            None => "  No merge_queue entry found.".to_string(),
            Some(m) => {
                let pr_s = m
                    .pr_number
                    .map(|n| n.to_string())
                    .unwrap_or_else(|| "(none)".to_string());
                let tb_s = m.target_branch.as_deref().unwrap_or("(none)");
                let err_s = m.error.as_deref().unwrap_or("(none)");
                format!(
                    "  state={st}  pr_number={pr}  target_branch={tb}  error={err}",
                    st = m.state,
                    pr = pr_s,
                    tb = tb_s,
                    err = err_s,
                )
            }
        };

        // CI check summary (if fetched for this PR).
        let ci_text = mq_entry
            .and_then(|m| m.pr_number.map(|pr| (m.repo_github.as_str(), pr)))
            .and_then(|(slug, pr)| {
                self.pipeline_ci_checks.get(&(slug.to_string(), pr))
            })
            .map(|ci| {
                let failed = if ci.failed_names.is_empty() {
                    "none".to_string()
                } else {
                    ci.failed_names.join(", ")
                };
                format!(
                    "  summary: {}  failed checks: {}",
                    ci.terse(),
                    failed,
                )
            })
            .unwrap_or_else(|| "  (not yet fetched -- run: gh pr checks)".to_string());

        // Per-stage statuses from the TUI board model.
        let stage_text = if let Some(iss) = issue {
            let stages = self.pipeline_stage_names_for_issue(iss);
            stages
                .iter()
                .map(|s| {
                    let status = self.stage_status_for(iss, s);
                    format!("  {}: {:?}", s, status)
                })
                .collect::<Vec<_>>()
                .join("\n")
        } else {
            "  (issue not found in current pipeline view)".to_string()
        };

        // PR number for reference commands.
        let pr_num_str = mq_entry
            .and_then(|m| m.pr_number)
            .map(|n| n.to_string())
            .unwrap_or_else(|| "(none)".to_string());

        format!(
            "You are a coordinator troubleshooter. Diagnose and unstick a stalled \
            pipeline item.\n\
            \n\
            ISSUE: #{n} in repo {repo} ({slug})\n\
            Title: {title}\n\
            \n\
            STAGE STATUSES (TUI board state):\n{stages}\n\
            \n\
            ASSIGNMENTS (oldest to newest):\n{asgns}\n\
            \n\
            MERGE QUEUE:\n{mq}\n\
            \n\
            CI CHECKS:\n{ci}\n\
            \n\
            CRITICAL RULES FOR THIS SESSION (#676):\n\
            - This is a READ-ONLY diagnostic session — no committed work.\n\
            - Do NOT run `coord report-result --status done` or `--status blocked`.\n\
              You have no committed work to back a success claim; doing so leaves\n\
              a false status on the pipeline that masks the real problem.\n\
            - For actual fixes, surface a plan — the operator will dispatch a Work session.\n\
            \n\
            TASK: Diagnose the specific stall. ALWAYS START HERE:\n\
            0. Run the real doctor first: coord diagnose {repo} {n}\n   \
               (auto-recovers phantoms, orphaned worktrees #618, dropped findings).\n   \
               If it reports needs_reset=true, confirm with the operator before --reset.\n\
            Then check each pattern in order:\n\
            1. Test gate not recorded (smoke test required but no verdict)\n   \
               -> coord test --passed <aid>\n\
            2. Verdict keyed to wrong assignment after a bounce (#567)\n   \
               -> check if latest review assignment id matches what the merge gate checks\n\
            3. NULL-branch remote rework (#557): finalize went DB-only, branch col empty\n   \
               -> check branch field in assignments table above\n\
            4. Lingering worktree blocks next fix (#560)\n   \
               -> ssh <machine> ls ~/.coord/worktrees/\n\
            5. Coordinator running stale code from wrong base branch (#561)\n   \
               -> git -C ~/src/claude-coordinator branch --show-current\n\
            6. Review = request-changes, no fix dispatched yet\n   \
               -> launch an interactive fix session from the TUI right-click menu\n\
            7. Stale merge_queue.error\n   \
               -> re-run: coord merge --repo {repo} --dry-run\n\
            8. Headless review fired on interactive work (#555)\n   \
               -> check assignment_type on the review row\n\
            9. Misleading green review box (#473)\n   \
               -> cross-check review_verdict in DB: sqlite3 ~/.coord/coord.db\n\
            10. in-flight/ready liveness misclassification (#559)\n   \
               -> confirm assignment status matches what TUI shows\n\
            11. CI failing or pending (#240)\n   \
               -> gh pr checks --repo {slug} {pr}\n\
            12. PR not mergeable or has conflicts\n   \
               -> gh pr view {pr} --repo {slug}; rebase if needed\n\
            13. status:ready limbo (#359)\n   \
               -> coord backlog {repo} {n}\n\
            \n\
            TOOLS:\n\
            - coord diagnose {repo} {n}  ← START HERE\n\
            - sqlite3 ~/.coord/coord.db\n\
            - gh pr view {pr} --repo {slug}\n\
            - gh pr checks --repo {slug} {pr}\n\
            - coord merge --repo {repo} --dry-run\n\
            - docs/ARCHITECTURE.md (section: When a merge isn't happening)\n\
            \n\
            Start with: coord diagnose {repo} {n}\n\
            Then coord merge --repo {repo} --dry-run\n\
            Then inspect the merge_queue error and assignment statuses above.\n\
            \n\
            For read-only diagnostics: act immediately.\n\
            For mutating recovery (recording verdicts, freeing worktrees, merging):\n\
            surface the plan and confirm with me before acting.\n\
            \n\
            WATCHER NOTE: This session is armed for auto-review (same as Work/Plan/Fix).\n\
            If you commit changes during diagnosis, an automated review will be dispatched\n\
            automatically. This is intentional — Troubleshoot may implement a fix — but\n\
            be aware: committing triggers the review pipeline.",
            n = issue_num,
            repo = coord_repo,
            slug = repo_slug,
            title = issue_title,
            stages = stage_text,
            asgns = assignments_text,
            mq = mq_text,
            ci = ci_text,
            pr = pr_num_str,
        )
    }

    pub(crate) fn launch_interactive_session_for_selected_issue(&mut self, mode: InteractiveLaunchMode) {
        // Resolve the repo up front so we can decide which machines qualify.
        let Some((repo, _key)) = self.selected_issue_repo_and_key() else {
            self.pipeline_status = Some((
                "Cannot resolve repo for this issue — interactive session not launched"
                    .to_string(),
                Instant::now(),
            ));
            return;
        };

        // #569: Troubleshoot is a LOCAL-ONLY read-only diagnostic — it reads
        // the coordinator's own board/DB and the live checkout, so there is no
        // machine choice (and never a reattach).  Always launch on the local
        // machine and skip both the machine picker and the reattach
        // short-circuit below.  (A picker here would offer remote machines that
        // `coord assign --troubleshoot` rejects with "local-only".)  #628: Chat
        // is the same — local-only, no completed-work target.  #885: Audit is
        // the same again — local-only, read-only, and the target IS the
        // selected issue (no work_aid to resolve).
        if matches!(
            mode,
            InteractiveLaunchMode::Troubleshoot
                | InteractiveLaunchMode::Chat
                | InteractiveLaunchMode::Audit
        ) {
            let machine = self.data.local_machine.clone();
            self.launch_interactive_session_on_machine(mode, machine, None, false);
            return;
        }

        // #587: secondary safety net — when Fix mode is triggered directly
        // (keyboard / right-click menu, NOT via the rework dialog's confirm
        // which sets `rework_bypass`), check whether the latest
        // request-changes review still has NULL review_findings in the
        // in-memory board.  If so, redirect to the rework dialog (which
        // collects findings before launching) instead of going straight to
        // the fix.  This prevents "blind fix" workers when the operator
        // dismissed the rework dialog without entering findings.
        //
        // Guard: only applies when there is a request-changes review
        // WITHOUT findings AND no failed-test work row (so the test-fail
        // fix path, `confirm_test_fix`, is never blocked by a pending
        // review findings issue on the same issue).
        if matches!(mode, InteractiveLaunchMode::Fix) && !self.rework_bypass {
            if let Some((issue_key_repo, issue_key)) = self.selected_issue_repo_and_key() {
                let issue_num = issue_key.1;
                let repo_slug = issue_key.0.clone();
                // #648: gate only when the review the fix is actually briefed
                // from — the LATEST request-changes review — lacks findings.
                // (The old `.any(... is_none())` across ALL request-changes
                // reviews mis-fired when an older review was empty.)
                let needs_findings =
                    self.fix_review_needs_findings_capture(&issue_key_repo, issue_num);
                // Skip the gate when the fix is for a FAILED TEST (not a review) —
                // the test-fail path is already briefed via the failure reason,
                // not the review findings.
                let is_test_fail_fix = self.test_failed_work_aid_for(&issue_key_repo, issue_num)
                    .is_some();
                if needs_findings && !is_test_fail_fix && self.pending_rework.is_none() {
                    self.pending_rework = Some(PendingRework {
                        coord_repo: repo.clone(),
                        repo_slug,
                        issue_num,
                        findings: String::new(),
                    });
                    self.pipeline_status = Some((
                        "⚠ No findings captured for this review — enter them in the \
                         dialog before starting the fix"
                            .to_string(),
                        Instant::now(),
                    ));
                    return;
                }
            }
        }

        // #486 Leg 4 UX: reattach short-circuit.  If a live interactive session
        // already exists for this issue, attach to it directly and SKIP the
        // machine picker — the machine choice is meaningless for a reattach
        // (`coord reattach` targets the existing session wherever it runs).
        // `launch_interactive_session_on_machine` detects the same live session
        // and runs `coord reattach` instead of a fresh `coord assign`; the
        // machine arg is ignored on that path, so pass the local one.
        if self.selected_issue_live_session_id().is_some() {
            self.pipeline_status = Some((
                "Reattaching to the live interactive session for this issue…".to_string(),
                Instant::now(),
            ));
            let machine = self.data.local_machine.clone();
            self.launch_interactive_session_on_machine(mode, machine, None, false);
            return;
        }

        let candidates = self.fleet_machines_for_repo(&repo);
        if candidates.len() >= 2 {
            self.pipeline_status = Some((
                format!(
                    "Pick a machine for the interactive {} (1–{} / Esc)…",
                    interactive_mode_verb(mode),
                    candidates.len(),
                ),
                Instant::now(),
            ));
            self.pending_machine_picker =
                Some(PendingMachinePicker { mode, machines: candidates });
            return;
        }
        // 0 or 1 capable machine: no choice — launch directly, preferring the
        // single candidate, falling back to the local machine.
        let machine = candidates
            .first()
            .map(|m| m.name.clone())
            .unwrap_or_else(|| self.data.local_machine.clone());
        self.launch_interactive_session_on_machine(mode, machine, None, false);
    }

    /// Reattach to a live session by id, opening the standalone Terminal panel
    /// and running `coord reattach <aid>` in it.  Does not require a selected
    /// pipeline issue — the standalone terminal is always available.
    pub(crate) fn reattach_session_by_aid(&mut self, aid: &str) {
        let cfg = self
            .command_runner
            .config_path
            .as_ref()
            .map(|p| format!("--config {} ", shell_quote_arg(&p.to_string_lossy())))
            .unwrap_or_default();
        let cmd = format!("coord reattach {}{}\r", cfg, shell_quote_arg(aid));

        // #955: a selected Terminal-tree leaf takes over the main pane
        // (routes to `fleet_terminal_sessions` instead of `terminal_session`
        // — see `standalone_pty_session_mut`), which would hide the claude
        // session this call is about to attach. Clear the tree selection so
        // the bare-shell pane (the one this function writes into) is what
        // the operator actually sees.
        self.terminal_tree_selected = None;

        // Switch to the standalone Terminal panel and send the command.
        // #1029 bug A: `switch_active_view` (not a raw field write) keeps
        // quadraui's ActivityBar highlight + sidebar header in sync too.
        self.switch_active_view(SidebarView::Terminal);
        // Lazily spawn the standalone terminal if not already alive.
        // The session spawns on the first drive_terminal_pane() call after
        // the dims become available; if it's already live, just send the cmd.
        if let Some(ref mut sess) = self.terminal_session {
            sess.send_str(&cmd);
        } else {
            // No session yet — write the command to a temp file and pick it
            // up on spawn, or just prime a note.  In practice the terminal
            // spawns within one render tick; the operator can type the reattach
            // command manually if they switch away before it appears.
            // Best-effort: try to spawn now if we have dims cached.
            let cwd = std::env::current_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("/"));
            let shell = quadraui::terminal_engine::default_shell();
            if let Ok(mut sess) =
                quadraui::terminal_engine::TerminalSession::spawn(80, 24, &shell, &cwd, 10_000)
            {
                sess.send_str(&cmd);
                self.terminal_session = Some(sess);
                self.terminal_spawn_error = None;
            }
            // If spawn fails the operator sees an error banner on the Terminal tab.
        }
    }

    /// #1029: launch (or reattach to) an interactive milestone-chat session
    /// for an EXISTING milestone's tracking issue — the "Open milestone
    /// chat" / "Add sub-issue via chat…" entries on the Milestone DAG
    /// panel's context menu.  Unlike the per-pipeline-issue interactive
    /// launchers (`launch_interactive_session_for_selected_issue` and
    /// friends), the target here is a `MilestoneHeader` context-menu target,
    /// not "the selected pipeline issue" — so this fires into the
    /// STANDALONE Terminal pane (`self.terminal_session`), mirroring
    /// `launch_merge_queue_interactive`'s shape, rather than the pipeline
    /// card's per-issue detail-terminal machinery (which is tied to the
    /// Work → Test → Review → Merge stage-advance flow that milestone chat
    /// is not part of).
    ///
    /// Reattach: if a `type="milestone-chat"` session is already running for
    /// `(repo, tracking_issue)`, delegate straight to
    /// [`Self::reattach_session_by_aid`] — zero new reattach code, and the
    /// operator lands back in the SAME conversation instead of a duplicate
    /// (#1029 bug #4). Otherwise build a fresh `coord assign --interactive
    /// --milestone-chat-of …` line via [`build_milestone_chat_launch_cmd`]
    /// and spawn/reuse the standalone terminal exactly as
    /// `reattach_session_by_aid` does.
    pub(crate) fn launch_milestone_chat_session(
        &mut self,
        repo: String,
        tracking_issue: u64,
        add_child: Option<u64>,
    ) {
        // #1029 bug B: remember where the operator was so Esc (once PTY
        // focus is released) can return them there instead of stranding
        // them in the Terminal view with no way back but manual
        // navigation. Captured *before* any view switch, but applied
        // *after* (see `bookmark_origin` below) — because `switch_active_view`
        // now unconditionally clears `terminal_return_view` (iter-2 fix), so
        // setting it beforehand would be wiped by the switch into Terminal.
        // A no-op if we're already parked in Terminal (e.g. re-triggering
        // from a context menu reachable from there) — nothing to bookmark.
        let bookmark_origin = (self.active_view != SidebarView::Terminal).then_some(self.active_view);

        if let Some(aid) =
            self.reattachable_session_aid(tracking_issue, &repo, InteractiveLaunchMode::MilestoneChat)
        {
            // `reattach_session_by_aid` switches into Terminal (clearing the
            // bookmark); re-apply the origin afterwards so both the "already
            // running" and "fresh spawn" paths return correctly on Esc.
            // (Reattach is also used from the live-sessions overlay, which has
            // no origin — hence we set it here, not inside reattach.)
            self.reattach_session_by_aid(&aid);
            self.terminal_return_view = bookmark_origin;
            return;
        }

        let cfg_path = self
            .command_runner
            .config_path
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned());
        let machine = self.data.local_machine.clone();
        let launch_line = build_milestone_chat_launch_cmd(
            cfg_path.as_deref(),
            &machine,
            &repo,
            tracking_issue,
            add_child,
        );

        // #955: a selected Terminal-tree leaf takes over the main pane — clear
        // the selection so the bare-shell pane this writes into is visible
        // (mirrors `reattach_session_by_aid` / `launch_merge_queue_interactive`).
        self.terminal_tree_selected = None;
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Terminal);
        // #1029 bug B (iter-2): re-apply the origin bookmark *after* the
        // switch, which cleared it.
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
                        "Milestone chat",
                        &format!("Failed to spawn terminal: {}", e),
                        ToastSeverity::Error,
                    );
                    return;
                }
            }
        }
        self.terminal_focused = true;
        self.push_toast(
            "Milestone chat",
            &format!("Launching interactive milestone chat for {} #{} …", repo, tracking_issue),
            ToastSeverity::Info,
        );
    }

    /// #955: attach (or reuse a warm) local PTY running `coord terminal
    /// attach <machine:name>` for the fleet terminal identified by `key =
    /// (machine, name)`.  Mirrors `reattach_session_by_aid`'s pattern for
    /// claude sessions: spawn a bare local shell, then feed it the attach
    /// command as literal keystrokes — `tmux attach`/`ssh -t … tmux attach`
    /// (#952) then takes over the PTY exactly as if the operator had typed
    /// the command themselves.
    ///
    /// No-op when a session (or a recorded spawn error) already exists for
    /// `key` — `drive_terminal_pane` calls this every tick while the leaf
    /// is selected, and re-attaching on every tick would both be wasteful
    /// and would spam the shell with duplicate `coord terminal attach`
    /// invocations.  Also a no-op until `terminal_pending_dims` has been
    /// populated by a render pass (mirrors the standalone terminal's lazy
    /// spawn) — retried on the next tick once dims are known.
    pub(crate) fn ensure_fleet_terminal_attached(&mut self, key: &(String, String)) {
        if self.fleet_terminal_sessions.contains_key(key)
            || self.fleet_terminal_spawn_errors.contains_key(key)
        {
            return;
        }
        let Some((cols, rows)) = self.terminal_pending_dims.get() else {
            return;
        };
        let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"));
        let shell = quadraui::terminal_engine::default_shell();
        match quadraui::terminal_engine::TerminalSession::spawn(
            cols.max(20),
            rows.max(5),
            &shell,
            &cwd,
            10_000, // 10 000-line scrollback
        ) {
            Ok(mut sess) => {
                let cfg = self
                    .command_runner
                    .config_path
                    .as_ref()
                    .map(|p| format!("--config {} ", shell_quote_arg(&p.to_string_lossy())))
                    .unwrap_or_default();
                let target = format!("{}:{}", key.0, key.1);
                let cmd = format!(
                    "coord terminal attach {}{}\r",
                    cfg,
                    shell_quote_arg(&target)
                );
                sess.send_str(&cmd);
                // #955: wrap in `FleetTerminalSession` so a later cache
                // eviction (leaf switched away, discovery reshuffle, or
                // the whole TUI quitting) cleanly detaches the tmux
                // client instead of letting the PTY's EOF-on-drop write
                // kill the remote session — see that type's doc comment.
                let ssh_host = self.resolve_fleet_terminal_ssh_host(&key.0);
                self.fleet_terminal_sessions.insert(
                    key.clone(),
                    FleetTerminalSession::new(sess, ssh_host, &key.1),
                );
            }
            Err(e) => {
                self.fleet_terminal_spawn_errors
                    .insert(key.clone(), e.to_string());
            }
        }
    }

    /// #955: resolve `machine` (a `self.data.machines` name) to the ssh
    /// host a [`FleetTerminalSession`] should detach through — `None` for
    /// the local machine (bare `tmux detach-client`), `Some(host)` for a
    /// remote one (`ssh <host> tmux detach-client`). Same lookup
    /// `kill_session_by_aid` below uses for the analogous kill path.
    pub(crate) fn resolve_fleet_terminal_ssh_host(&self, machine: &str) -> Option<String> {
        let is_local = machine == self.data.local_machine || machine.is_empty();
        if is_local {
            return None;
        }
        Some(
            self.data
                .machines
                .iter()
                .find(|mm| mm.name == machine)
                .map(|mm| mm.host.clone())
                .unwrap_or_else(|| machine.to_string()),
        )
    }

    /// Kill a live tmux session by assignment id.  Runs `tmux kill-session -t
    /// coord-<aid>` locally, or `ssh <machine> tmux kill-session …` for remote
    /// machines.  The entry is immediately removed from `live_tmux_sessions`.
    /// A worktree-prune toast is shown because `git worktree remove` is a
    /// separate step the user should do — ballooning scope to do it here is
    /// noted and deferred per the issue spec.
    pub(crate) fn kill_session_by_aid(&mut self, aid: &str, machine: Option<&str>) {
        let session_name = format!("coord-{}", aid);
        let local = self.data.local_machine.clone();
        let is_local = machine
            .map(|m| m == local || m.is_empty())
            .unwrap_or(true);

        let status = if is_local {
            std::process::Command::new("tmux")
                .args(["kill-session", "-t", &session_name])
                .status()
                .ok()
        } else if let Some(m) = machine {
            // Resolve the machine's ssh host from the config machines list.
            let host = self
                .data
                .machines
                .iter()
                .find(|mm| mm.name == m)
                .map(|mm| mm.host.as_str())
                .unwrap_or(m);
            std::process::Command::new("ssh")
                .args(["-o", "ConnectTimeout=5", host, "tmux", "kill-session", "-t", &session_name])
                .status()
                .ok()
        } else {
            None
        };

        // Remove from the discovered list immediately regardless of success —
        // if the session was already dead the removal is still correct.
        self.live_tmux_sessions.retain(|s| s.assignment_id != aid);

        let msg = if status.map(|s| s.success()).unwrap_or(false) {
            format!("Killed session coord-{}.  Prune worktree: git worktree remove ~/.coord/worktrees/{}", aid, aid)
        } else {
            format!(
                "Kill attempted for coord-{} (may already be gone).  Prune: git worktree remove ~/.coord/worktrees/{}",
                aid, aid
            )
        };
        self.push_toast("Session killed", &msg, ToastSeverity::Info);
    }

    /// Reattach to the live interactive session for the selected issue,
    /// whatever its type (work / review / test / merge).  This is the dedicated
    /// "Reattach to live session" action: unlike the type-gated Start launchers
    /// (#569 keeps "Start fix" from reattaching into a running review), it
    /// reattaches to ANY running session for this issue by passing the resolved
    /// aid explicitly to `launch_interactive_session_on_machine`, which then
    /// runs `coord reattach <aid>` instead of a fresh (claim-blocked)
    /// `coord assign`.
    pub(crate) fn reattach_to_selected_issue_live_session(&mut self) {
        // #727: use the wider resolver so zombie sessions (tmux alive but board
        // assignment already done/failed) are also reachable from the menu.
        // `selected_issue_any_session_id` prefers a running session when one
        // exists, falling back to any discovered session (the zombie case).
        let Some(aid) = self.selected_issue_any_session_id() else {
            self.pipeline_status =
                Some(("No live session to reattach to.".to_string(), Instant::now()));
            return;
        };
        self.pipeline_status = Some((
            "Reattaching to the live interactive session for this issue…".to_string(),
            Instant::now(),
        ));
        // The machine arg is ignored on the reattach path — `coord reattach
        // <aid>` resolves the host from the board — so the local machine is a
        // fine placeholder.  Mode is likewise irrelevant: an explicit
        // `reattach_aid` (Some) short-circuits the type-gated lookup and runs
        // `coord reattach`.
        let machine = self.data.local_machine.clone();
        self.launch_interactive_session_on_machine(InteractiveLaunchMode::Work, machine, Some(aid), false);
    }

    // ── Leg 3c / A3 (#517, #581): test-verdict routing ─────────────────────

    /// The `test_state` of the assignment row with `id == aid`, or `None`.
    pub(crate) fn test_state_for_aid(&self, aid: &str) -> Option<String> {
        self.data
            .assignments
            .iter()
            .find(|a| a.id == aid)
            .and_then(|a| a.test_state.clone())
            .filter(|s| !s.is_empty())
    }

    /// #581: the most-recent `done` `type="work"` assignment id for the SELECTED
    /// issue whose Test gate FAILED (`test_state == "failed"`), or `None`.  This
    /// is the work id the interactive `--fix-of` test-fail front door consumes.
    pub(crate) fn selected_test_failed_work_aid(&self) -> Option<String> {
        let (repo, issue_key) = self.selected_issue_repo_and_key()?;
        self.test_failed_work_aid_for(&repo, issue_key.1)
    }

    /// #1223: also matches `type="test-author"`, correlated via
    /// `for_issue_number` rather than `issue_number` — see
    /// [`Self::completed_work_aid_for`]'s doc for why test-author needs the
    /// different correlation field.
    pub(crate) fn test_failed_work_aid_for(&self, coord_repo: &str, issue_num: u64) -> Option<String> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.repo == coord_repo)
            .filter(|a| match a.assignment_type.as_deref().unwrap_or("work") {
                "work" => a.issue_number == issue_num,
                "test-author" => a.for_issue_number == Some(issue_num),
                _ => false,
            })
            .filter(|a| a.test_state.as_deref() == Some("failed"))
            .filter(|a| a.branch.as_deref().map(|b| !b.is_empty()).unwrap_or(false))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|a| a.id.clone())
    }

    /// #1104: the id of the SELECTED issue's most-recent `type="work"`
    /// assignment when it (a) is the LATEST work/rework attempt on the issue
    /// (by `dispatched_at`) and (b) failed outright (`status == "failed"`,
    /// a worker-reported failure — NOT a test/review gate, which `Fix`
    /// already covers) while leaving a real branch behind. `None` otherwise.
    ///
    /// Deliberately keyed on the *latest* work row rather than "any failed
    /// row ever": a `--rework-of` that later succeeds becomes a NEW `type=
    /// "work"` row on the SAME branch (`_dispatch_rework_of`,
    /// `coord/commands/dispatch_workers.py`) with a later `dispatched_at`,
    /// so once a rework lands `done` it naturally becomes the latest row and
    /// this stops matching — "no subsequent successful work/rework on that
    /// branch" falls out of the ordering for free, no separate bookkeeping.
    ///
    /// Drives the "Continue" context-menu item (interactive-only, #1104) and
    /// supplies the `--rework-of <aid>` argument when it fires.
    pub(crate) fn selected_failed_work_aid_with_branch(&self) -> Option<String> {
        let (repo, issue_key) = self.selected_issue_repo_and_key()?;
        self.failed_work_aid_with_branch_for(&repo, issue_key.1)
    }

    /// #1223: also matches `type="test-author"`, correlated via
    /// `for_issue_number` rather than `issue_number` — see
    /// [`Self::completed_work_aid_for`]'s doc for why test-author needs the
    /// different correlation field.
    pub(crate) fn failed_work_aid_with_branch_for(
        &self,
        coord_repo: &str,
        issue_num: u64,
    ) -> Option<String> {
        let latest = self
            .data
            .assignments
            .iter()
            .filter(|a| a.repo == coord_repo)
            .filter(|a| match a.assignment_type.as_deref().unwrap_or("work") {
                "work" => a.issue_number == issue_num,
                "test-author" => a.for_issue_number == Some(issue_num),
                _ => false,
            })
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })?;
        if latest.status == "failed" && latest.branch.as_deref().map(|b| !b.is_empty()).unwrap_or(false) {
            Some(latest.id.clone())
        } else {
            None
        }
    }

    /// Leg 3c / A3 (#517): scan armed interactive testing sessions for a
    /// freshly-recorded test verdict on the WORK row and route it (Test
    /// precedes Review): `failed` → fail→fix confirm prompt (same action a
    /// request-changes review takes); `passed`/`skipped` → start-review confirm
    /// prompt.  Strictly board-driven — the verdict comes from `coord test`
    /// (written to the DB), never the session TTY.  Returns `true` when it
    /// raised a prompt.
    pub(crate) fn detect_test_verdict(&mut self) -> bool {
        if self.stage_prompt_open() || self.armed_for_test_verdict.is_empty() {
            return false;
        }
        let found = self.armed_for_test_verdict.iter().find_map(|(key, armed)| {
            let current = self.test_state_for_aid(&armed.work_aid);
            // Edge-trigger: a terminal verdict that differs from the arm-time
            // snapshot (so re-arming over an already-tested row doesn't re-fire).
            match current.as_deref() {
                Some(v @ ("failed" | "passed" | "skipped"))
                    if Some(v) != armed.prior_test_state.as_deref() =>
                {
                    Some((key.clone(), v.to_string()))
                }
                _ => None,
            }
        });
        let Some((key, verdict)) = found else {
            return false;
        };
        // #602: don't preempt a still-attended testing session.  The verdict is
        // usually recorded with `coord test --fail|--passed` from *inside* the
        // interactive test session, so it lands while the operator is still in
        // the pane (claude running, or the shell prompt after `/exit`).  Firing
        // now would pop the fail→fix / pass→merge prompt over that live pane and
        // — worse — confirming it replaces the pane's PTY (the launch insert() is
        // keyed by this very issue_key).  So DEFER: leave the arm in place and
        // re-check each tick; it fires once the test pane is closed (its shell
        // has exited).  `key` IS the issue_key `detail_terminal_sessions` uses.
        // #722: also defer for remote fleet sessions; key == (coord_repo, issue_num).
        if self.session_pane_live(&key) || self.issue_has_live_session_for_repo(key.1, &key.0) {
            return false;
        }
        let Some(armed) = self.armed_for_test_verdict.remove(&key) else {
            return false;
        };
        // Drop terminal focus so the confirm prompt owns Enter, not the shell.
        self.detail_terminal_focused = false;
        if verdict == "failed" {
            // #603: preview the fix briefing (the failed WORK id is the fix target).
            let aid = armed.work_aid.clone();
            self.pending_test_fix = Some(PendingTestFix {
                coord_repo: armed.coord_repo,
                repo_slug: armed.repo_slug,
                issue_num: armed.issue_num,
            });
            self.start_fix_briefing_preview(Some(aid));
        } else {
            // passed / skipped → the smoke test cleared, so offer the
            // human-attended review (Test precedes Review now; was: merge).
            self.pending_auto_review = Some(PendingAutoReview {
                coord_repo: armed.coord_repo,
                repo_slug: armed.repo_slug,
                issue_num: armed.issue_num,
            });
        }
        true
    }

    /// #603: kick off (or, with `None`, clear) the async fix-briefing preview
    /// shown in the fail→fix / rework confirm dialog.  `aid` is the fix target
    /// (a test-failed WORK id, or a request-changes REVIEW id).
    pub(crate) fn start_fix_briefing_preview(&mut self, aid: Option<String>) {
        self.fix_briefing_rx = None;
        match aid {
            Some(aid) => {
                self.fix_briefing_preview =
                    Some("Resolving the fix briefing…".to_string());
                let cfg = self.command_runner.config_path.clone();
                self.fix_briefing_rx = Some(spawn_fix_briefing_fetch(aid, cfg));
            }
            None => self.fix_briefing_preview = None,
        }
    }

    /// #603: drain the in-flight `coord fix-briefing` fetch into the preview.
    pub(crate) fn poll_fix_briefing_preview(&mut self) -> bool {
        let recv = match self.fix_briefing_rx.as_ref() {
            Some(rx) => rx.try_recv(),
            None => return false,
        };
        match recv {
            Ok(text) => {
                self.fix_briefing_preview = Some(text);
                self.fix_briefing_rx = None;
                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.fix_briefing_rx = None;
                false
            }
        }
    }

    /// #603: the fix-briefing preview as capped dialog body lines.  The confirm
    /// dialog has no scroll, so cap to keep it on-screen (char-safe — the
    /// briefing contains multibyte glyphs like 📌/⚠️).
    pub(crate) fn fix_briefing_preview_lines(&self) -> Vec<StyledText> {
        let mut out = vec![
            StyledText::plain(String::new()),
            StyledText::plain("The fix worker will be briefed with:".to_string()),
        ];
        let text = self
            .fix_briefing_preview
            .as_deref()
            .unwrap_or("Resolving the fix briefing…");
        const MAX_LINES: usize = 16;
        const MAX_CHARS: usize = 1200;
        let capped: String = if text.chars().count() > MAX_CHARS {
            text.chars().take(MAX_CHARS).collect::<String>() + "…"
        } else {
            text.to_string()
        };
        for (n, line) in capped.lines().enumerate() {
            if n >= MAX_LINES {
                out.push(StyledText::plain("…".to_string()));
                break;
            }
            out.push(StyledText::plain(line.to_string()));
        }
        out
    }

    /// Leg 3c (#517, #581): the operator confirmed the fail→fix prompt — select
    /// the issue's row, open its Terminal tab, and launch the interactive
    /// `--fix-of` session.  The fix is keyed on the failed WORK id (the backend
    /// #581 test-fail fix front door accepts it directly).
    pub(crate) fn confirm_test_fix(&mut self) {
        let Some(p) = self.pending_test_fix.as_ref() else {
            return;
        };
        // #602: belt-and-suspenders — never replace a live attended pane.  The
        // offer is normally deferred until the test pane closes (see
        // detect_test_verdict), but guard the launch directly too: if a live
        // session for this issue is still open, keep the prompt up and tell the
        // operator to exit first rather than clobber their session.
        // #722: extend to remote fleet sessions; filter by repo to avoid
        // false positives in multi-repo setups.
        let issue_key = (p.repo_slug.clone(), p.issue_num);
        if self
            .detail_terminal_sessions
            .get(&issue_key)
            .is_some_and(|s| !s.is_exited())
            || self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo)
        {
            let issue_num = p.issue_num;
            self.push_toast(
                "Start fix",
                &format!(
                    "An interactive session for #{issue_num} is still open — type \
                     /exit (and close the shell) there, then press Enter again to \
                     start the fix.",
                ),
                ToastSeverity::Warning,
            );
            return;
        }
        let p = self
            .pending_test_fix
            .take()
            .expect("pending_test_fix checked Some above");
        let idx = self
            .pipeline_issues
            .iter()
            .position(|iss| iss.repo_slug == p.repo_slug && iss.number == p.issue_num);
        let Some(idx) = idx else {
            self.push_toast(
                "Start fix",
                &format!(
                    "Could not find {} #{} in the pipeline to start its fix.",
                    p.coord_repo, p.issue_num,
                ),
                ToastSeverity::Warning,
            );
            return;
        };
        self.pipeline_sel = Some(idx);
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Pipeline);
        self.pipeline_detail_tab = PipelineDetailTab::Terminal;
        self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Fix);
    }

    /// Leg 3c (#517, #306): the operator confirmed the approve→merge prompt —
    /// select the issue's row, open its Terminal tab, and launch the
    /// interactive `--merge-of` merge agent (proactive rebase + conflict
    /// resolution on the approved branch).
    pub(crate) fn confirm_merge(&mut self) {
        let Some(p) = self.pending_merge.as_ref() else {
            return;
        };
        // #602: never replace a live attended pane (same guard as confirm_test_fix).
        // #722: extend to remote fleet sessions; filter by repo to avoid
        // false positives in multi-repo setups.
        let issue_key = (p.repo_slug.clone(), p.issue_num);
        if self
            .detail_terminal_sessions
            .get(&issue_key)
            .is_some_and(|s| !s.is_exited())
            || self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo)
        {
            let issue_num = p.issue_num;
            self.push_toast(
                "Start merge",
                &format!(
                    "An interactive session for #{issue_num} is still open — type \
                     /exit (and close the shell) there, then press Enter again to \
                     start the merge.",
                ),
                ToastSeverity::Warning,
            );
            return;
        }
        let p = self
            .pending_merge
            .take()
            .expect("pending_merge checked Some above");
        let idx = self
            .pipeline_issues
            .iter()
            .position(|iss| iss.repo_slug == p.repo_slug && iss.number == p.issue_num);
        let Some(idx) = idx else {
            self.push_toast(
                "Start merge",
                &format!(
                    "Could not find {} #{} in the pipeline to start its merge.",
                    p.coord_repo, p.issue_num,
                ),
                ToastSeverity::Warning,
            );
            return;
        };
        self.pipeline_sel = Some(idx);
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Pipeline);
        self.pipeline_detail_tab = PipelineDetailTab::Terminal;
        self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Merge);
    }

    /// Launch a human-attended `claude` session for the selected pipeline
    /// issue on `machine` (#467; supersedes the ssh+tmux launcher previously
    /// built for #446).  When `machine` is the local machine the session runs
    /// on this TTY; when it is a remote fleet machine, `coord assign <machine>
    /// --interactive` drives the ssh+tmux dispatch (#486 Leg 4).
    ///
    /// Spawns a local shell session (`TerminalSession::spawn`) into the
    /// per-issue map and auto-runs `coord assign --interactive …` (the
    /// existing `interactive.py` seed path delivers the briefing).  The
    /// session stays strictly human-attended — the TUI never parses the
    /// session TTY for completion/verdict and never auto-advances the
    /// pipeline (Anthropic ToS §3.7 / #437).
    pub(crate) fn launch_interactive_session_on_machine(
        &mut self,
        mode: InteractiveLaunchMode,
        machine: String,
        reattach_aid: Option<String>,
        force: bool,
    ) {
        self.launch_interactive_session_on_machine_inner(mode, machine, reattach_aid, force, None)
    }

    /// #863 review fix: resolve the `(coord_repo, (repo_slug, issue_num))`
    /// pair an interactive launch should target — the PINNED preflight
    /// target when one is given, otherwise the live UI selection.
    ///
    /// Extracted out of `launch_interactive_session_on_machine_inner` so the
    /// pinning behaviour is directly unit-testable without having to drive a
    /// real terminal spawn (which would type a live `coord assign` command
    /// line into a real shell — not something a unit test should risk).
    pub(crate) fn resolve_launch_repo_and_key(
        &self,
        pinned_target: Option<&FixPreflightTarget>,
    ) -> Option<(String, (String, u64))> {
        match pinned_target {
            Some(t) => Some((t.coord_repo.clone(), (t.repo_slug.clone(), t.issue_num))),
            None => self.selected_issue_repo_and_key(),
        }
    }

    /// #863 review fix: resolve the work_aid a Fix launch should carry — the
    /// PINNED preflight target's aid when one is given (the exact id the cap
    /// preflight just checked), otherwise derived from the live selection
    /// (a request-changes review, falling back to a test-failed work row,
    /// per Leg 3 / #517 / #581). See [`resolve_launch_repo_and_key`] for why
    /// this is a separate testable method rather than inlined.
    pub(crate) fn resolve_fix_work_aid(&self, pinned_target: Option<&FixPreflightTarget>) -> Option<String> {
        pinned_target
            .map(|t| t.work_aid.clone())
            .or_else(|| self.selected_request_changes_review_aid())
            .or_else(|| self.selected_test_failed_work_aid())
    }

    /// #863: `launch_interactive_session_on_machine`'s actual body, plus
    /// `pinned_target` — `Some(..)` ONLY when this call is the follow-through
    /// from the `dispatch_fix_cap_preflight` completion handler in
    /// `run_periodic_work`.  That handler must skip the cap-preflight gate
    /// below (it just ran the preflight) AND resolve the repo/issue/work_aid
    /// from the preflighted target rather than the live UI selection (#863
    /// review fix — see [`FixPreflightTarget`]).  Every other caller goes
    /// through the public wrapper above with `pinned_target = None` so a
    /// FRESH Fix dispatch always gets gated and resolves from the current
    /// selection as before.  Without the `Some` distinction, a clean
    /// (non-forced) preflight success would re-enter the gate on the
    /// follow-through call and preflight forever; without pinning, an
    /// operator who changes the selection while the preflight runs would get
    /// the launch silently misdirected (see `FixPreflightTarget` doc).
    pub(crate) fn launch_interactive_session_on_machine_inner(
        &mut self,
        mode: InteractiveLaunchMode,
        machine: String,
        reattach_aid: Option<String>,
        force: bool,
        pinned_target: Option<FixPreflightTarget>,
    ) {
        let after_preflight = pinned_target.is_some();
        let Some((repo, issue_key)) = self.resolve_launch_repo_and_key(pinned_target.as_ref()) else {
            self.pipeline_status = Some((
                "Cannot resolve repo for this issue — interactive session not launched".to_string(),
                Instant::now(),
            ));
            return;
        };
        let issue_num = issue_key.1;

        // `coord assign` needs a MACHINE positional.  `machine` is chosen by
        // the caller: the local machine for Work/Plan (and single-machine
        // fleets), or the operator's pick for a remote Review/Fix (#486 Leg 4).
        if machine.is_empty() {
            self.pipeline_status = Some((
                "Cannot resolve the target machine (not in coordinator.yml) — interactive session not launched"
                    .to_string(),
                Instant::now(),
            ));
            return;
        }

        // #539: For Review mode, resolve the most-recent completed work
        // assignment id that has a branch.  If none exists, surface a toast
        // and bail — no point spawning a session with a broken command.
        let work_aid: Option<String> = match mode {
            InteractiveLaunchMode::Review => match self.selected_completed_work_aid() {
                Some(aid) => Some(aid),
                None => {
                    self.push_toast(
                        "Start review (interactive)",
                        "No completed work assignment with a branch found — cannot start review.",
                        ToastSeverity::Warning,
                    );
                    return;
                }
            },
            // Leg 3 (#517): Fix carries the request-changes REVIEW id, OR the
            // test-failed WORK id (#581).  Prefer a request-changes review; fall
            // back to a test-failed work row (the backend accepts either).
            //
            // #863 review fix: on the preflight follow-through, use the
            // PINNED work_aid straight from `FixPreflightTarget` — that's the
            // exact id the cap preflight just checked. Re-deriving from the
            // live selection here would defeat the repo/issue pinning above:
            // the terminal would open in the pinned issue's cwd while the
            // command line carried a work_aid resolved from whatever is
            // CURRENTLY selected.
            InteractiveLaunchMode::Fix => match self.resolve_fix_work_aid(pinned_target.as_ref()) {
                Some(aid) => Some(aid),
                None => {
                    self.push_toast(
                        "Start fix (interactive)",
                        "No request-changes review or failed test found for this issue — nothing to fix.",
                        ToastSeverity::Warning,
                    );
                    return;
                }
            },
            // #1104: Continue carries the FAILED WORK id (worker-reported
            // failure, no test/review gate — that's Fix's territory).
            InteractiveLaunchMode::Continue => match self.selected_failed_work_aid_with_branch() {
                Some(aid) => Some(aid),
                None => {
                    self.push_toast(
                        "Continue",
                        "No failed work assignment with a branch found for this issue — nothing to continue.",
                        ToastSeverity::Warning,
                    );
                    return;
                }
            },
            // Leg 3c / A3 (#517): Test + Merge both operate on the completed
            // work assignment's branch.
            InteractiveLaunchMode::Test => match self.selected_completed_work_aid() {
                Some(aid) => Some(aid),
                None => {
                    self.push_toast(
                        "Start testing (interactive)",
                        "No completed work assignment with a branch found — cannot start testing.",
                        ToastSeverity::Warning,
                    );
                    return;
                }
            },
            InteractiveLaunchMode::Merge => match self.selected_completed_work_aid() {
                Some(aid) => Some(aid),
                None => {
                    self.push_toast(
                        "Start merge (interactive)",
                        "No completed work assignment with a branch found — cannot start merge.",
                        ToastSeverity::Warning,
                    );
                    return;
                }
            },
            _ => None,
        };

        // Pass the TUI's resolved coordinator.yml path so `coord assign` finds
        // it regardless of the embedded terminal's cwd (the issue's repo dir).
        let cfg_path = self
            .command_runner
            .config_path
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned());

        let (cols, rows) = self.detail_terminal_pending_dims.get().unwrap_or((80, 24));
        let cwd = self.detail_terminal_cwd(&issue_key);
        let shell = quadraui::terminal_engine::default_shell();

        // #487/#514: reattach to the live tmux session for this issue (run
        // `coord reattach` instead of a fresh `coord assign`) when one exists and
        // the board still has it running.  Otherwise (finalized work whose tmux
        // session is gone, but the discovery sweep hasn't refreshed yet) fall
        // through to a fresh launch instead of a dead reattach ("session not
        // alive").
        //
        // #601 follow-up: there are often SEVERAL discovered sessions for one
        // issue (lingering done smoke/review sessions beside the live one), so
        // filter by (issue, repo) AND running, then take the first — NOT
        // `.find(match)` then-check, which could pick a finalized session and
        // fall through to a fresh `coord assign` (→ "already claimed: remote
        // branch exists"). Mirrors `selected_issue_live_session_id`.
        //
        // #569: Troubleshoot is *always* a fresh diagnostic launch — never a
        // reattach. A stalled item almost always still has a stuck/phantom
        // `running` assignment (that's *why* it's stalled), and reattaching to it
        // would hijack the diagnostic with the very session we're trying to
        // diagnose. Force a fresh launch for Troubleshoot.
        // Mode-aware: only reattach to a running session of the SAME type this
        // mode launches, so "Start fix" never reattaches into a running review.
        //
        // An EXPLICIT `reattach_aid` (the dedicated "Reattach to live session"
        // menu action) overrides the type-gate: that action is type-agnostic on
        // purpose, so reattaching to a running review/test/merge works (the
        // generic launch paths still pass `None` here → type-gated, #569).
        let maybe_live_session =
            reattach_aid.or_else(|| self.reattachable_session_aid(issue_num, &repo, mode));

        // #863: before opening a FRESH (non-reattach) Fix session, headlessly
        // preflight `coord assign --fix-of --dry-run` via `CommandRunner` so a
        // `pipeline.max_review_iterations` cap refusal (#862) surfaces as an
        // in-TUI "force another fix round?" confirm instead of a dead
        // terminal the operator has to retype with `--force` by hand.
        // `after_preflight` is true on the follow-through call from the
        // preflight's OWN completion handler (whether or not the operator
        // ended up forcing it) — skip the gate then, or a clean preflight
        // success would re-enter it forever.
        if mode == InteractiveLaunchMode::Fix && maybe_live_session.is_none() && !after_preflight {
            if let Some(aid) = work_aid.clone() {
                self.dispatch_fix_cap_preflight(
                    repo.clone(),
                    issue_key.0.clone(),
                    issue_num,
                    machine.clone(),
                    aid,
                    false,
                );
                return;
            }
        }

        match quadraui::terminal_engine::TerminalSession::spawn(
            cols.max(20),
            rows.max(5),
            &shell,
            &cwd,
            10_000,
        ) {
            Ok(mut sess) => {
                let launch_line = if let Some(assignment_id) = &maybe_live_session {
                    // Reattach path: a running session exists from a previous TUI
                    // run.  Run `coord reattach` which attaches and finalizes on exit.
                    // `--config` is a per-SUBCOMMAND option (the top-level `coord`
                    // group rejects it), so it must come AFTER `reattach`, not
                    // before it — `coord reattach --config <path> <aid>`.
                    let cfg = match cfg_path.as_deref() {
                        Some(p) if !p.is_empty() => {
                            format!("--config {} ", shell_quote_arg(p))
                        }
                        _ => String::new(),
                    };
                    format!("coord reattach {}{}\r", cfg, shell_quote_arg(assignment_id))
                } else if matches!(
                    mode,
                    InteractiveLaunchMode::Troubleshoot | InteractiveLaunchMode::Chat
                ) {
                    // #569/#628: write the multi-line briefing to a temp file and
                    // launch via `coord assign --troubleshoot|--chat --briefing-file`
                    // as a SINGLE physical line.  Inlining a multi-line --briefing
                    // would split on newlines and strand the embedded PTY shell at
                    // `quote>`; and both run with no claim / no worktree so they
                    // never conflict with the item's in-progress claim.
                    let is_chat = matches!(mode, InteractiveLaunchMode::Chat);
                    let (briefing, flag, fname) = if is_chat {
                        (
                            self.chat_briefing(&repo, issue_num),
                            "--chat",
                            format!("coord-chat-{issue_num}.md"),
                        )
                    } else {
                        (
                            self.troubleshoot_briefing(&repo, issue_num),
                            "--troubleshoot",
                            format!("coord-troubleshoot-{issue_num}.md"),
                        )
                    };
                    let briefing_path = std::env::temp_dir().join(fname);
                    // Best-effort write; if it fails, `coord assign` surfaces a
                    // clear "file not found" error in the terminal rather than
                    // silently launching with no briefing.
                    let _ = std::fs::write(&briefing_path, &briefing);
                    build_briefed_interactive_launch_cmd(
                        flag,
                        cfg_path.as_deref(),
                        &briefing_path.to_string_lossy(),
                        &machine,
                        &repo,
                        issue_num,
                    )
                } else if mode == InteractiveLaunchMode::Fix && force {
                    // #863: the operator confirmed past `pipeline.max_review_iterations`
                    // (#862) — append `--force` to the SAME `--fix-of` command line so
                    // the backend dispatches iteration N+1 anyway instead of the cap's
                    // usual `sys.exit(2)`.  Deliberately NOT routed through the shared
                    // `build_interactive_launch_cmd` (its signature is exercised by
                    // ~15 pure-function tests for every OTHER mode) — this mirrors its
                    // Fix arm exactly, plus `--force`.
                    let cfg = match cfg_path.as_deref() {
                        Some(p) if !p.is_empty() => format!("--config {} ", shell_quote_arg(p)),
                        _ => String::new(),
                    };
                    let aid = shell_quote_arg(work_aid.as_deref().unwrap_or(""));
                    let m = shell_quote_arg(&machine);
                    let r = shell_quote_arg(&repo);
                    format!(
                        "coord assign {}--interactive --fix-of {} --force {} {} {}\r",
                        cfg, aid, m, r, issue_num,
                    )
                } else {
                    // Fresh launch path.  Re-pressing the launch key while a
                    // previous interactive session is still alive replaces the PTY.
                    build_interactive_launch_cmd(
                        cfg_path.as_deref(),
                        &machine,
                        &repo,
                        issue_num,
                        mode,
                        work_aid.as_deref(),
                    )
                };
                sess.send_str(&launch_line);

                self.detail_terminal_sessions.insert(issue_key.clone(), sess);
                self.detail_terminal_spawn_errors.remove(&issue_key);

                // #559: on a fresh launch (not a reattach), optimistically add
                // a synthetic LiveTmuxSession so the pipeline card moves to
                // Active → Live immediately, without waiting for the next
                // remote-discovery sweep.  The "pending-" prefix lets
                // `poll_remote_sessions` merge it away once the real session
                // appears in a subsequent discovery result.
                if maybe_live_session.is_none() {
                    // Remove any stale pending entry for this (repo, issue)
                    // before adding the fresh one (e.g. user pressed launch
                    // twice without a discovery sweep in between).
                    self.live_tmux_sessions.retain(|s| {
                        !(s.assignment_id.starts_with("pending-")
                            && s.issue_number == Some(issue_num)
                            && s.repo_name.as_deref() == Some(repo.as_str()))
                    });
                    self.live_tmux_sessions.push(LiveTmuxSession {
                        assignment_id: format!("pending-{}-{}", repo, issue_num),
                        issue_number: Some(issue_num),
                        repo_name: Some(repo.clone()),
                        issue_title: None,
                        machine: None,
                        pane_dead: false,
                        pending_sweep_count: 0,
                        attached: false,
                    });
                }

                // Leg 2/3 (#517): arm the right board-driven watcher.  Test
                // precedes Review, so the stage chain is
                // Work → Test → Review → Merge (fail at any stage → Fix):
                //   Work/Plan/Fix/Troubleshoot → arm the completion watcher,
                //     which offers the next stage (Test) when it finishes
                //     (Troubleshoot may implement a fix, so arm it too).
                //   Test → arm test-verdict routing (pass → Review, fail → Fix).
                //   Review → arm review-verdict routing (approve → Merge,
                //     request-changes → Fix); disarm the completion watcher
                //     (a review is now in flight for this work).
                match mode {
                    InteractiveLaunchMode::Work
                    | InteractiveLaunchMode::Plan
                    | InteractiveLaunchMode::Fix
                    | InteractiveLaunchMode::Continue
                    | InteractiveLaunchMode::Troubleshoot
                    | InteractiveLaunchMode::Chat => {
                        let prior_done_ids = self.done_work_aids_for(&repo, issue_num);
                        self.armed_for_auto_review.insert(
                            issue_key.clone(),
                            ArmedAutoReview {
                                coord_repo: repo.clone(),
                                repo_slug: issue_key.0.clone(),
                                issue_num,
                                prior_done_ids,
                            },
                        );
                        // A Fix/Continue consumes the request-changes verdict
                        // or failed attempt that triggered it — stop
                        // verdict-routing from re-firing.
                        if matches!(
                            mode,
                            InteractiveLaunchMode::Fix | InteractiveLaunchMode::Continue
                        ) {
                            self.armed_for_verdict.remove(&issue_key);
                        }
                    }
                    InteractiveLaunchMode::Review => {
                        self.armed_for_auto_review.remove(&issue_key);
                        let prior_verdicted_ids =
                            self.verdicted_review_ids_for(&repo, issue_num);
                        self.armed_for_verdict.insert(
                            issue_key.clone(),
                            ArmedVerdict {
                                coord_repo: repo.clone(),
                                repo_slug: issue_key.0.clone(),
                                issue_num,
                                prior_verdicted_ids,
                            },
                        );
                    }
                    // Leg 3c / A3 (#517): arm the test-verdict watcher so a
                    // `coord test --passed|--fail` recorded by the testing agent
                    // routes to the fail→fix or pass→review confirm prompt.
                    InteractiveLaunchMode::Test => {
                        let work_aid = work_aid.clone().unwrap_or_default();
                        let prior_test_state = self.test_state_for_aid(&work_aid);
                        self.armed_for_test_verdict.insert(
                            issue_key.clone(),
                            ArmedTestVerdict {
                                coord_repo: repo.clone(),
                                repo_slug: issue_key.0.clone(),
                                issue_num,
                                work_aid,
                                prior_test_state,
                            },
                        );
                    }
                    // Leg 3c (#517): merge-prep ends with the operator merging
                    // manually (TUI Go / `coord merge`) — no auto-advance to arm.
                    InteractiveLaunchMode::Merge => {}
                    // #885: Audit is a standalone read-only analysis — it never
                    // feeds Work → Test → Review → Merge, so nothing to arm.
                    InteractiveLaunchMode::Audit => {}
                    // #1029: MilestoneChat never reaches this function (it has
                    // its own dedicated launcher, `launch_milestone_chat_session`)
                    // — arm included only for match exhaustiveness.
                    InteractiveLaunchMode::MilestoneChat => {}
                }

                let status_msg = if maybe_live_session.is_some() {
                    format!("Reattaching to running session for {} #{} …", repo, issue_num)
                } else {
                    let verb = match mode {
                        InteractiveLaunchMode::Work => "work",
                        InteractiveLaunchMode::Plan => "plan→work",
                        InteractiveLaunchMode::Review => "review",
                        InteractiveLaunchMode::Fix => "fix",
                        InteractiveLaunchMode::Continue => "continue",
                        InteractiveLaunchMode::Troubleshoot => "troubleshoot",
                        InteractiveLaunchMode::Test => "testing",
                        InteractiveLaunchMode::Merge => "merge",
                        InteractiveLaunchMode::Chat => "chat",
                        InteractiveLaunchMode::Audit => "audit",
                        // #1029: never reached — MilestoneChat has its own
                        // dedicated launcher; arm only for exhaustiveness.
                        InteractiveLaunchMode::MilestoneChat => "milestone chat",
                    };
                    format!(
                        "Launching interactive {} session for {} #{} …",
                        verb, repo, issue_num,
                    )
                };
                self.pipeline_status = Some((status_msg, Instant::now()));

                // Auto-focus so the user can type immediately.
                self.detail_terminal_focused = true;
            }
            Err(e) => {
                self.detail_terminal_spawn_errors.insert(issue_key, e.to_string());
            }
        }
    }

    /// #863: headlessly dispatch `coord assign --interactive --fix-of <aid>
    /// [--force] <machine> <repo> <issue> --dry-run` via `CommandRunner` and
    /// record `pending_fix_cap_preflight` so the `run_periodic_work`
    /// completion handler can route on the result.
    ///
    /// This is a probe, not the real dispatch: `_dispatch_fix_of`
    /// (coord/commands/dispatch_workers.py) runs the iteration-cap check
    /// BEFORE its `--dry-run` early-return, and never touches a TTY on that
    /// path, so it's safe to run as an ordinary background subprocess (no
    /// embedded PTY needed) purely to see whether the cap blocks it.
    ///
    /// #863 review fix: refuses to dispatch a SECOND preflight while one is
    /// already in flight — `pending_fix_cap_preflight` holds exactly one
    /// target, so a second call here (e.g. the operator navigates to a
    /// different issue and clicks Fix again before the first preflight's
    /// subprocess has returned) would silently overwrite it. The first
    /// preflight's eventual result would then fail to match
    /// `pending_fix_cap_preflight` (now pointing at the second target) and
    /// get dropped on the floor — no launch, no toast, no force-confirm, for
    /// an issue the operator is still waiting on. `confirm_fix_force_past_cap`
    /// re-enters this function too, but only ever after the prior preflight
    /// already cleared `pending_fix_cap_preflight` to `None`, so it's
    /// unaffected by this guard.
    pub(crate) fn dispatch_fix_cap_preflight(
        &mut self,
        coord_repo: String,
        repo_slug: String,
        issue_num: u64,
        machine: String,
        work_aid: String,
        force: bool,
    ) {
        if let Some(existing) = &self.pending_fix_cap_preflight {
            self.push_toast(
                "Fix preflight already running",
                &format!(
                    "Still checking the iteration cap for {} #{} — wait for it to finish \
                     before starting another Fix.",
                    existing.coord_repo, existing.issue_num,
                ),
                ToastSeverity::Warning,
            );
            return;
        }
        // #863 review fix: surface SOME visible feedback while the preflight
        // subprocess is in flight — previously nothing indicated a Fix click
        // had done anything until the (headless, background) command
        // finished, which read as "Fix did nothing" for however long the
        // subprocess takes.
        self.pipeline_status = Some((
            format!("Checking iteration cap for {} #{} …", coord_repo, issue_num),
            Instant::now(),
        ));
        let issue_str = issue_num.to_string();
        let mut argv: Vec<String> = vec![
            "assign".to_string(),
            "--interactive".to_string(),
            "--fix-of".to_string(),
            work_aid.clone(),
        ];
        if force {
            argv.push("--force".to_string());
        }
        argv.push(machine.clone());
        argv.push(coord_repo.clone());
        argv.push(issue_str);
        argv.push("--dry-run".to_string());
        let argv_refs: Vec<&str> = argv.iter().map(String::as_str).collect();
        self.command_runner.spawn_queued(&argv_refs);
        self.pending_fix_cap_preflight = Some(PendingFixCapPreflight {
            coord_repo,
            repo_slug,
            issue_num,
            machine,
            work_aid,
            force,
        });
    }

    /// #863: the operator confirmed the "Iteration cap (N) reached — force
    /// another fix round?" prompt raised by the preflight completion handler.
    /// Re-runs the SAME preflight with `--force` appended; on a clean exit the
    /// completion handler proceeds straight into the real (forced) launch.
    pub(crate) fn confirm_fix_force_past_cap(&mut self) {
        let Some(p) = self.pending_fix_force_confirm.take() else {
            return;
        };
        self.dispatch_fix_cap_preflight(
            p.coord_repo,
            p.repo_slug,
            p.issue_num,
            p.machine,
            p.work_aid,
            true,
        );
    }

    /// Render the Pipeline detail Terminal tab for the selected issue (#440).
    ///
    /// Called from `render_content` when `pipeline_detail_tab ==
    /// PipelineDetailTab::Terminal`.  Stashes the desired `(cols, rows)` in
    /// `detail_terminal_pending_dims` for `tick` to apply; reads the live
    /// `TerminalSession` snapshot and paints it, or shows a placeholder while
    /// the session is starting up (or a red error if spawn failed).
    pub(crate) fn render_detail_terminal_tab(&self, backend: &mut dyn Backend, rect: Rect) {
        let lh = backend.line_height().max(1.0);
        let cell_w = backend.char_width().max(1.0);
        // #790: reserve a 1-row copy-mode hint strip at the bottom, but only
        // in the Pipeline Terminal tab where host selection is wired.  The
        // Board Terminal tab is Chat-scoped (#675) with no selection path, so
        // it keeps the full height and shows no hint.
        let show_hint = self.terminal_copy_mode_available();
        let hint_h = if show_hint { lh.min(rect.height) } else { 0.0 };
        let term_rect = Rect::new(rect.x, rect.y, rect.width, (rect.height - hint_h).max(0.0));
        let cols = (term_rect.width / cell_w).floor().max(1.0) as u16;
        let rows = (term_rect.height / lh).floor().max(1.0) as u16;
        self.detail_terminal_pending_dims.set(Some((cols, rows)));
        // Closure to paint the hint after the terminal content (if reserved).
        let paint_hint = |app: &Self, backend: &mut dyn Backend| {
            if show_hint {
                let hint_rect = Rect::new(
                    rect.x,
                    rect.y + term_rect.height,
                    rect.width,
                    rect.height - term_rect.height,
                );
                backend.draw_list(hint_rect, &app.terminal_copy_hint_list());
            }
        };

        let Some(issue_key) = self.selected_issue_key() else {
            // No issue selected — show a neutral placeholder.
            backend.draw_list(
                term_rect,
                &ListView {
                    id: WidgetId::new("detail-terminal-no-issue"),
                    title: None,
                    items: vec![activity_item(
                        "  Select an issue to open a terminal.",
                        Color::rgb(160, 160, 160),
                    )],
                    selected_idx: 0,
                    scroll_offset: 0,
                    has_focus: false,
                    bordered: false,
                    h_scroll: 0,
                    max_content_width: None,
                    show_v_scrollbar: false,
                },
            );
            paint_hint(self, backend);
            return;
        };

        if let Some(sess) = self.detail_terminal_sessions.get(&issue_key) {
            let total = sess.history_len() + sess.rows() as usize;
            let sb = if total > sess.rows() as usize {
                Some(sess.scrollbar_state(None))
            } else {
                None
            };
            let snapshot = sess.to_terminal(
                WidgetId::new(format!("detail-terminal:{}:{}", issue_key.0, issue_key.1)),
                sb,
            );
            backend.draw_terminal(term_rect, &snapshot);
        } else {
            // Session pending or spawn error.
            let (msg, color) = match self.detail_terminal_spawn_errors.get(&issue_key) {
                Some(err) => (
                    format!("  ⚠ Terminal session error: {}  (F12 to focus)", err),
                    Color::rgb(220, 80, 80),
                ),
                None => (
                    "  Starting shell session…".to_string(),
                    Color::rgb(180, 180, 180),
                ),
            };
            backend.draw_list(
                term_rect,
                &ListView {
                    id: WidgetId::new("detail-terminal-placeholder"),
                    title: None,
                    items: vec![activity_item(&msg, color)],
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
        paint_hint(self, backend);
    }
}

// ── #467: Interactive launcher command builder ────────────────────────────────

/// #467: which kind of human-attended interactive session to launch.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum InteractiveLaunchMode {
    /// Implement the issue directly (work tools; default issue-body briefing).
    Work,
    /// Plan first, then implement in the SAME session.  Uses work tools
    /// (`--no-plan`, so the session can code after planning) seeded with a
    /// plan-then-work briefing.
    Plan,
    /// Human-attended adversarial review of a completed work assignment (#539).
    /// Emits `coord assign --interactive --review-of <work_aid> …`.
    Review,
    /// Leg 3 (#517): human-attended FIX of a request-changes review, OR of a
    /// work row whose Test gate failed (#581).  Continues the existing branch;
    /// emits `coord assign --interactive --fix-of <aid> …`.  `work_aid` carries
    /// the REVIEW id (request-changes path) or the WORK id (test-fail path).
    Fix,
    /// #1104: human-attended CONTINUE of a work assignment that FAILED
    /// outright (worker-reported, no test/review gate involved — that's
    /// `Fix`'s territory) while leaving a real branch behind. Continues the
    /// existing branch (no orphan branch); emits `coord assign --interactive
    /// --rework-of <aid> --briefing <text> …`. `work_aid` carries the FAILED
    /// WORK id. Unlike `Fix`, `--rework-of` requires an explicit `--briefing`
    /// (CLI-enforced), so the TUI seeds a default one (like `Plan`'s seeded
    /// briefing) rather than leaving it to the backend to derive.
    Continue,
    /// #569: human-attended diagnostic session for a stalled pipeline item.
    /// Superseded by `Chat` (#628), which subsumes diagnosis — the TUI no longer
    /// constructs this. Retained because the `coord assign --troubleshoot` CLI
    /// path and the launch helpers still model it.
    #[allow(dead_code)]
    Troubleshoot,
    /// Leg 3c / A3 (#517, #350, #581): human-attended TESTING agent for a
    /// completed work assignment.  Lists the smoke tests, pulls the artifact,
    /// guides the operator, records the verdict.  Emits `coord assign
    /// --interactive --smoke-of <work_aid> …`.  `work_aid` is the WORK id.
    Test,
    /// Leg 3c (#517, #306): human-attended MERGE agent for a completed+approved
    /// work assignment.  Rebases the branch onto the default branch, resolves
    /// conflicts, pushes.  Emits `coord assign --interactive --merge-of
    /// <work_aid> …`.  `work_aid` is the WORK id.
    Merge,
    /// #628: human-attended "Chat about issue" — a live interactive session
    /// seeded with all current data about the issue (body + board snapshot).
    /// Open Q&A / UX sketching / stall diagnosis; MAY edit the issue (`coord
    /// issue edit`) and send it to Pending (`coord ready`). Like Troubleshoot:
    /// local-only, no claim/worktree, briefed-from-file. Emits `coord assign
    /// --interactive --chat …`.
    Chat,
    /// Milestone Outcome Audit Phase 1 (#885): human-attended READ-ONLY
    /// milestone-outcome analyst for a milestone's tracking epic. Only
    /// offered on rows carrying the `epic` label. Like Troubleshoot/Chat:
    /// local-only, no claim/worktree — the target IS the selected issue, so
    /// no separate work_aid is needed. Emits `coord assign --interactive
    /// --audit-of <issue_num> …`.
    Audit,
    /// #1029: human-attended MILESTONE CHAT for an existing milestone's
    /// tracking issue — a genuine tmux-attached `claude` session, replacing
    /// the old headless `claude -p` / SSE-chat-overlay mechanism. Like
    /// Chat/Troubleshoot/Audit: local-only, no claim/worktree — the target IS
    /// the milestone's tracking issue, so no separate work_aid is needed.
    /// UNLIKE Chat/Troubleshoot/Audit, it DOES reattach (re-opening "Open
    /// milestone chat" on an already-running session must resume it, not
    /// spawn a duplicate — the whole point of #1029). Emits `coord assign
    /// --interactive --milestone-chat-of <tracking_issue> [--add-child <n>]
    /// …`.
    MilestoneChat,
}

/// #486: short verb for an interactive launch mode — used in the machine-picker
/// prompt and dialog title.
pub(crate) fn interactive_mode_verb(mode: InteractiveLaunchMode) -> &'static str {
    match mode {
        InteractiveLaunchMode::Work => "work",
        InteractiveLaunchMode::Plan => "plan",
        InteractiveLaunchMode::Review => "review",
        InteractiveLaunchMode::Fix => "fix",
        InteractiveLaunchMode::Continue => "continue",
        InteractiveLaunchMode::Troubleshoot => "troubleshoot",
        InteractiveLaunchMode::Test => "testing",
        InteractiveLaunchMode::Merge => "merge",
        InteractiveLaunchMode::Chat => "chat",
        InteractiveLaunchMode::Audit => "audit",
        InteractiveLaunchMode::MilestoneChat => "milestone chat",
    }
}

/// The board assignment TYPE a given interactive launch mode produces or
/// continues — used to keep the reattach decision mode-aware so a `Fix` launch
/// never reattaches into a running `review` session.  `--fix-of`/`--rework-of`
/// both continue the work branch as a `type="work"` row (coord/cli.py), so
/// Fix and Continue (#1104) both map to "work".
/// Troubleshoot never reattaches (callers guard); its sentinel matches nothing.
pub(crate) fn interactive_mode_assignment_type(mode: InteractiveLaunchMode) -> &'static str {
    match mode {
        InteractiveLaunchMode::Work
        | InteractiveLaunchMode::Plan
        | InteractiveLaunchMode::Fix
        | InteractiveLaunchMode::Continue => "work",
        InteractiveLaunchMode::Review => "review",
        InteractiveLaunchMode::Test => "smoke",
        InteractiveLaunchMode::Merge => "merge",
        InteractiveLaunchMode::Troubleshoot => "troubleshoot",
        // Chat never reattaches (callers guard); sentinel matches nothing.
        InteractiveLaunchMode::Chat => "chat",
        InteractiveLaunchMode::Audit => "audit",
        // #1029: MilestoneChat DOES reattach — this must match the board
        // `type="milestone-chat"` row so reopening "Open milestone chat" on a
        // still-running session resumes it instead of spawning a duplicate.
        InteractiveLaunchMode::MilestoneChat => "milestone-chat",
    }
}

/// #467: briefing seeded into a `Plan` interactive session — plan first, get
/// human sign-off, then implement in the same session.  Apostrophe-free so
/// [`shell_quote_arg`] wraps it cleanly in single quotes.
pub(crate) fn interactive_plan_briefing(issue_num: u64) -> String {
    format!(
        "Plan-then-implement for issue #{n} in this session. First read it with `gh issue view {n}`, then propose a concise implementation plan and ask me to confirm it. Once I approve, implement the plan here in this same session — do not stop after planning.",
        n = issue_num,
    )
}

/// #1104: default briefing seeded into a `Continue` interactive session —
/// `--rework-of` requires an explicit `--briefing` (CLI-enforced), and the
/// launcher line auto-submits with no chance for the operator to type one
/// first, so the TUI supplies a sensible default (same pattern as
/// [`interactive_plan_briefing`]). `prior_aid` is the failed work
/// assignment id being continued, so the worker can look up its own
/// prior commits/context. Apostrophe-free so [`shell_quote_arg`] wraps it
/// cleanly in single quotes.
pub(crate) fn interactive_continue_briefing(issue_num: u64, prior_aid: &str) -> String {
    format!(
        "Continue the prior attempt on this branch for issue #{n} — the previous work assignment ({aid}) failed before finishing (not a test or review gate — a worker-reported failure). Start by reviewing the existing commits on this branch (`git log`) and the prior failure via `coord log {aid}`, pick up where that attempt left off, and finish the work.",
        n = issue_num,
        aid = prior_aid,
    )
}

/// #863: extract the configured cap (`N`) from the `_dispatch_fix_of`
/// cap-refusal stderr line —
/// `"error: max_review_iterations (N) reached for work …"` — for the
/// force-confirm prompt.  Returns `None` if the format ever drifts; the
/// prompt falls back to generic wording in that case.
pub(crate) fn parse_max_review_iterations(stderr: &str) -> Option<u32> {
    let marker = "max_review_iterations (";
    let idx = stderr.find(marker)?;
    let rest = &stderr[idx + marker.len()..];
    let end = rest.find(')')?;
    rest[..end].trim().parse().ok()
}

/// Build the local launcher line that auto-runs when the user picks a
/// "Start … (interactive)" action (#467) or presses `s` in the Pipeline
/// detail Terminal tab.
///
/// The trailing `\r` is intentional — it auto-submits the line so the
/// launcher starts immediately.  Only the *launcher* is auto-run; the
/// briefing / kickoff prompt is then driven by `coord assign --interactive`
/// itself (the existing `interactive.py` seed path: bracketed-paste +
/// readiness gate) — so the TUI does NOT inject any prompt, copy anything
/// to the clipboard, or talk to the remote `ssh`/`tmux` stack.
///
/// The session is strictly human-attended: the TUI never parses the TTY
/// for completion or verdict and never auto-advances the pipeline state
/// (Anthropic ToS §3.7 / #437).  The launcher only ESTABLISHES the
/// session.
///
/// `<repo>` is shell-quoted via [`shell_quote_arg`] so a repo name that
/// contains spaces or shell metacharacters can't break the line; the
/// issue number is `u64`, so it never needs quoting.
pub(crate) fn build_interactive_launch_cmd(
    config_path: Option<&str>,
    machine: &str,
    repo: &str,
    issue_num: u64,
    mode: InteractiveLaunchMode,
    work_aid: Option<&str>,
) -> String {
    // `coord` finds `coordinator.yml` in its cwd by default, but the embedded
    // terminal's cwd is the issue's repo (which usually isn't the coordinator
    // checkout), so inject `--config <path>` — the same path the TUI's
    // CommandRunner uses — right after the subcommand.
    let cfg = match config_path {
        Some(p) if !p.is_empty() => format!("--config {} ", shell_quote_arg(p)),
        _ => String::new(),
    };
    // `coord assign` takes positional MACHINE REPO ISSUE — there is NO --repo
    // option.  Options precede the positionals.  `--no-plan` forces type=work
    // (full Read/Edit/Write/Bash tools) regardless of `dispatch.require_plan`,
    // so BOTH modes can actually implement — Plan just plans first per its
    // seeded briefing.
    let m = shell_quote_arg(machine);
    let r = shell_quote_arg(repo);
    match mode {
        InteractiveLaunchMode::Work => format!(
            "coord assign {}--interactive --no-plan {} {} {}\r",
            cfg, m, r, issue_num,
        ),
        InteractiveLaunchMode::Plan => format!(
            "coord assign {}--interactive --no-plan --briefing {} {} {} {}\r",
            cfg,
            shell_quote_arg(&interactive_plan_briefing(issue_num)),
            m,
            r,
            issue_num,
        ),
        InteractiveLaunchMode::Review => {
            // #539: `coord assign --interactive --review-of <work_aid>` routes
            // to the reviewer role; the session is human-attended, same as Work/Plan.
            // work_aid is always Some when Review is reached (caller guards it).
            let aid = shell_quote_arg(work_aid.unwrap_or(""));
            format!(
                "coord assign {}--interactive --review-of {} {} {} {}\r",
                cfg, aid, m, r, issue_num,
            )
        }
        InteractiveLaunchMode::Fix => {
            // Leg 3 (#517): `coord assign --interactive --fix-of <aid>`
            // continues the existing branch with write tools.  work_aid carries
            // the REVIEW id (request-changes) or the WORK id (test-fail, #581) —
            // the backend accepts either.  Always Some (the caller guards it).
            let aid = shell_quote_arg(work_aid.unwrap_or(""));
            format!(
                "coord assign {}--interactive --fix-of {} {} {} {}\r",
                cfg, aid, m, r, issue_num,
            )
        }
        InteractiveLaunchMode::Continue => {
            // #1104: `coord assign --interactive --rework-of <aid> --briefing
            // <text>` continues the existing failed-attempt branch with write
            // tools. work_aid carries the FAILED WORK id (always Some — the
            // caller guards it). Unlike `--fix-of`, `--rework-of` requires an
            // explicit `--briefing` — seed the TUI's default one.
            let aid = shell_quote_arg(work_aid.unwrap_or(""));
            let briefing = shell_quote_arg(&interactive_continue_briefing(
                issue_num,
                work_aid.unwrap_or(""),
            ));
            format!(
                "coord assign {}--interactive --rework-of {} --briefing {} {} {} {}\r",
                cfg, aid, briefing, m, r, issue_num,
            )
        }
        // #569: Troubleshoot is dispatched by the right-click handler via a
        // dedicated path that bypasses this function (the diagnostic briefing
        // requires app state not available here).  This arm must never be
        // reached; surface any future invariant violation immediately rather
        // than silently launching a plain work session with no briefing.
        InteractiveLaunchMode::Troubleshoot => unreachable!(
            "Troubleshoot mode must not reach build_interactive_launch_cmd — \
             handle it in the caller with the troubleshoot_briefing path"
        ),
        // #628: Chat, like Troubleshoot, is built from the briefing-file path in
        // the caller (chat_briefing) — never here.
        InteractiveLaunchMode::Chat => unreachable!(
            "Chat mode must not reach build_interactive_launch_cmd — \
             handle it in the caller with the chat_briefing path"
        ),
        InteractiveLaunchMode::Test => {
            // Leg 3c / A3 (#517): `coord assign --interactive --smoke-of
            // <work_aid>` launches the human-attended testing agent in the live
            // checkout.  work_aid is the WORK id (always Some — caller guards it).
            let aid = shell_quote_arg(work_aid.unwrap_or(""));
            format!(
                "coord assign {}--interactive --smoke-of {} {} {} {}\r",
                cfg, aid, m, r, issue_num,
            )
        }
        InteractiveLaunchMode::Merge => {
            // Leg 3c (#517, #306): `coord assign --interactive --merge-of
            // <work_aid>` launches the human-attended merge agent (rebase +
            // conflict resolution).  work_aid is the WORK id (always Some).
            let aid = shell_quote_arg(work_aid.unwrap_or(""));
            format!(
                "coord assign {}--interactive --merge-of {} {} {} {}\r",
                cfg, aid, m, r, issue_num,
            )
        }
        InteractiveLaunchMode::Audit => {
            // Milestone Outcome Audit Phase 1 (#885): `coord assign
            // --interactive --audit-of <epic_issue>` launches the human-
            // attended read-only milestone-outcome analyst. The audited epic
            // IS the selected issue, so `--audit-of` carries the same
            // `issue_num` as the trailing positional — no work_aid involved.
            format!(
                "coord assign {}--interactive --audit-of {} {} {} {}\r",
                cfg, issue_num, m, r, issue_num,
            )
        }
        // #1029: MilestoneChat is dispatched from the Milestone DAG panel's
        // context menu (a `MilestoneHeader` target), never from "the
        // selected pipeline issue" this function resolves its args from —
        // it has its own dedicated launcher/builder
        // (`launch_milestone_chat_session` / `build_milestone_chat_launch_cmd`)
        // and must never reach this one.
        InteractiveLaunchMode::MilestoneChat => unreachable!(
            "MilestoneChat mode must not reach build_interactive_launch_cmd — \
             handle it in launch_milestone_chat_session with \
             build_milestone_chat_launch_cmd"
        ),
    }
}

/// #1029: Build the single-line shell command that launches (or, via a
/// running-session reattach elsewhere, resumes) an interactive milestone
/// chat for an EXISTING milestone's tracking issue.  Unlike
/// Troubleshoot/Chat, the seed content is resolved entirely server-side
/// (`coord.milestone_chat.resolve_milestone_chat_briefing`, fetched fresh
/// from GitHub) — mirroring `--audit-of` — so no briefing file needs to be
/// written or passed from the TUI.  `add_child` is `Some(n)` for the "Add
/// sub-issue via chat…" entry point, `None` for plain "Open milestone chat".
pub(crate) fn build_milestone_chat_launch_cmd(
    config_path: Option<&str>,
    machine: &str,
    repo: &str,
    tracking_issue: u64,
    add_child: Option<u64>,
) -> String {
    let cfg = match config_path {
        Some(p) if !p.is_empty() => format!("--config {} ", shell_quote_arg(p)),
        _ => String::new(),
    };
    let child_flag = match add_child {
        Some(n) => format!("--add-child {} ", n),
        None => String::new(),
    };
    format!(
        "coord assign {}--interactive --milestone-chat-of {} {}{} {} {}\r",
        cfg,
        tracking_issue,
        child_flag,
        shell_quote_arg(machine),
        shell_quote_arg(repo),
        tracking_issue,
    )
}

/// #569: Build the single-line shell command that launches a read-only
/// Troubleshoot diagnostic session.
///
/// The (multi-line) diagnostic briefing is passed by FILE path — never inlined
/// — because a multi-line `--briefing` typed into the embedded PTY shell would
/// be split on its newlines and strand the shell at `quote>` (bug #2).  The
/// `--troubleshoot` flag runs read-only in the live checkout with no claim and
/// no worktree, so it never conflicts with the In-progress item's own claim
/// (bug #3).  The returned line is a single physical line; the trailing `\r`
/// is the submit key, not a line break.
pub(crate) fn build_briefed_interactive_launch_cmd(
    flag: &str,
    config_path: Option<&str>,
    briefing_file: &str,
    machine: &str,
    repo: &str,
    issue_num: u64,
) -> String {
    // Shared launcher for the briefing-file flavours (`--troubleshoot`, `--chat`)
    // — both pass a SINGLE physical line with the multi-line briefing on disk.
    let cfg = match config_path {
        Some(p) if !p.is_empty() => format!("--config {} ", shell_quote_arg(p)),
        _ => String::new(),
    };
    format!(
        "coord assign {}--interactive {} --briefing-file {} {} {} {}\r",
        cfg,
        flag,
        shell_quote_arg(briefing_file),
        shell_quote_arg(machine),
        shell_quote_arg(repo),
        issue_num,
    )
}

/// Minimal POSIX shell quoter for a single argument (#467).
///
/// Returns the argument unchanged when it is non-empty and consists only
/// of POSIX-safe characters (`[A-Za-z0-9_./-]`); otherwise wraps it in
/// single quotes and escapes any embedded single quotes via the standard
/// `'\''` trick.  Empty strings round-trip as `''`.
pub(crate) fn shell_quote_arg(s: &str) -> String {
    let safe = !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '/'));
    if safe {
        s.to_string()
    } else {
        let escaped = s.replace('\'', r"'\''");
        format!("'{}'", escaped)
    }
}

