//! Unattended `coord drive --tmux` session integration (#1398), LOCAL half.
//!
//! `coord drive <repo> <issue> --tmux` runs the whole Work → Test → Review →
//! Merge sequence unattended, detached inside a `coord-drive-<repo>-<issue>`
//! tmux session so it survives the launching terminal closing, a coord-tui
//! restart, or an ssh drop (a drive can run 60-90 minutes). This module is
//! the TUI's half of that: discover live drive sessions (mirrors
//! `fleet_terminals.rs`'s `FleetTerminal`/`LiveTmuxSession` pattern), launch
//! one from the Pipeline row context menu, attach the per-issue Terminal tab
//! to it, and stop it. "Launch" always ends attached: `spawn_drive_shell`
//! types `coord drive … --tmux && coord drive-attach …` as a single chained
//! command line, so opening the Terminal tab right after clicking "Drive
//! locally (tmux, this machine)" shows the live run, not a launch
//! confirmation sitting above a dead shell prompt.
//!
//! **#2634: this is the demoted, ephemeral path — NOT the primary automated
//! action any more.** A drive launched from here runs in a local PTY only:
//! it dies with this machine/tmux, and it never writes the daemon's
//! `drive_queue` table, so it is invisible to the Queue panel
//! (`drive_queue.rs`) by construction — two disjoint drivers under
//! confusingly similar menu labels ("Drive (automated)" vs "Add to drive
//! queue") was exactly #2634's report. The Pipeline row menu's prominent
//! "Drive (automated)" item now calls `dispatch_drive_queue_add`
//! (`drive_queue.rs`) — the durable, board-backed path — instead of anything
//! in this module; this module backs only the explicitly-demoted "Drive
//! locally (tmux, this machine)" item, for an operator who deliberately
//! wants a local, unqueued run.
//!
//! LOCAL ONLY: unlike fleet terminals / interactive `coord-<aid>` sessions, a
//! drive launched here runs on the operator's own machine (see #1398's "Out
//! of scope" — running the driver on the daemon is a follow-up gated on
//! #1395 option B), so there is no `--remote` ssh sweep here, just a local
//! `coord drive-sessions --json`.
//!
//! Killing the tmux session (`coord drive-stop`) IS Stop: the driver's
//! per-issue `flock` releases the instant the process exits (the OS does
//! this on any process exit, including a killed tmux pane), so cancellation
//! is correct by construction — no extra cleanup code needed here.
//!
//! **Import pattern:** `use super::*` is intentional — see `sessions.rs` /
//! `terminal.rs` / `fleet_terminals.rs` for the same rationale.
#[allow(unused_imports)]
use super::*;

/// One live `coord-drive-<repo>-<issue>` tmux session, discovered via
/// `coord drive-sessions --json`. Mirrors `FleetTerminal`'s shape.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct DriveSession {
    /// Coord-local repo name (matches `PipelineIssue::coord_repo`).
    pub(crate) repo: String,
    /// GitHub issue number.
    pub(crate) issue: u64,
    /// `true` when a client currently has the tmux session attached.
    #[allow(dead_code)] // parsed for parity with LiveTmuxSession/FleetTerminal; not yet displayed
    pub(crate) attached: bool,
    /// `true` for an optimistic entry inserted by `spawn_drive_shell` right
    /// after a fresh launch, before the next `coord drive-sessions`
    /// discovery sweep can confirm it. Mirrors `FleetTerminal::pending` /
    /// `LiveTmuxSession`'s `"pending-"`-prefix convention.
    pub(crate) pending: bool,
    /// Number of discovery sweeps a `pending` entry has survived without
    /// being covered by a real result. Mirrors
    /// `LiveTmuxSession::pending_sweep_count` / `FleetTerminal`'s twin —
    /// `poll_drive_sessions` evicts the entry once this exceeds
    /// `CoordApp::PENDING_DRIVE_SWEEP_BUDGET`, so a phantom "driving" badge
    /// that never becomes a real tmux session doesn't linger forever.
    pub(crate) pending_sweep_count: u8,
}

/// Parse the bare JSON array `coord drive-sessions --json` emits: objects
/// shaped `{"repo","issue","session_name","attached"}`
/// (`coord/commands/drive.py::drive_sessions`). `session_name` is not kept —
/// `(repo, issue)` is all any TUI consumer needs.
pub(crate) fn parse_drive_sessions_json(text: &str) -> Vec<DriveSession> {
    let v: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let arr = match v.as_array() {
        Some(a) => a,
        None => return Vec::new(),
    };
    arr.iter()
        .filter_map(|entry| {
            let repo = entry.get("repo")?.as_str()?.to_string();
            let issue = entry.get("issue")?.as_u64()?;
            let attached = entry
                .get("attached")
                .and_then(|a| a.as_bool())
                .unwrap_or(false);
            Some(DriveSession {
                repo,
                issue,
                attached,
                pending: false,
                pending_sweep_count: 0,
            })
        })
        .collect()
}

/// Fetch local live drive sessions by running `coord drive-sessions --json`.
///
/// Returns an empty `Vec` when tmux is not running, `coord` is not on PATH,
/// or parsing fails. Called once at startup — cheap but synchronous — mirrors
/// `fetch_fleet_terminals` / `fetch_live_tmux_sessions`.
pub(crate) fn fetch_drive_sessions() -> Vec<DriveSession> {
    let out = std::process::Command::new("coord")
        .args(["drive-sessions", "--json"])
        .output()
        .ok();
    let out = match out {
        Some(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    parse_drive_sessions_json(&String::from_utf8_lossy(&out.stdout))
}

/// Refresh drive-session discovery in the background, re-armed at the same
/// cadence as the remote-session / fleet-terminal sweeps (`CoordApp::refresh`)
/// so a drive started from an EXTERNAL shell (or stopped out-of-band) is
/// picked up without a full TUI restart.
pub(crate) fn spawn_drive_sessions_fetch() -> std::sync::mpsc::Receiver<Vec<DriveSession>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(fetch_drive_sessions());
    });
    rx
}

/// Which command line `spawn_drive_shell` types into the freshly-spawned
/// per-issue terminal shell.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DriveShellMode {
    /// `coord drive <repo> <issue> --tmux && coord drive-attach <repo>
    /// <issue>` — start a fresh drive, then immediately attach the SAME
    /// pane to the tmux session it just created. `coord drive --tmux`
    /// creates the session synchronously (`tmux new-session -d` blocks
    /// until the session exists — `launch_drive_in_tmux`, `coord/drive.py`)
    /// and only then exits 0, so chaining with `&&` is race-free: the
    /// attach never fires on a `--tmux` invocation that failed (e.g. "already
    /// driving"), and it never races a session that isn't there yet. Without
    /// this chain the pane would show the launch confirmation and then sit
    /// at a dead shell prompt — the acceptance criterion is "opening the
    /// Terminal tab shows the live run," not "shows a one-line confirmation
    /// that a run started somewhere else."
    Launch,
    /// `coord drive-attach <repo> <issue>` — reattach to a live one.
    Attach,
}

/// Pending "Stop drive" confirmation — carries the `(repo, issue)`
/// `confirm_kill_drive` needs to fire `coord drive-stop <repo> <issue>`
/// without re-deriving the target from the (possibly already-changed)
/// selection. Mirrors `PendingKillTerminal` / `PendingKillSession`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PendingKillDrive {
    pub(crate) repo: String,
    pub(crate) issue: u64,
}

impl CoordApp {
    /// Mirrors `PENDING_TERMINAL_SWEEP_BUDGET` / `PENDING_SESSION_SWEEP_BUDGET`.
    pub(crate) const PENDING_DRIVE_SWEEP_BUDGET: u8 = 2;

    /// Whether a live `coord-drive-*` tmux session exists for this
    /// `(issue_number, repo_name)`. `repo_name` is the coord-local repo name
    /// (`PipelineIssue::coord_repo`), matching what `coord drive <repo>
    /// <issue>` was invoked with.
    pub(crate) fn issue_has_live_drive(&self, issue_number: u64, repo_name: &str) -> bool {
        self.drive_sessions
            .iter()
            .any(|s| s.issue == issue_number && s.repo == repo_name)
    }

    /// #1499: whether ANY assignment on the board for this `(issue_number,
    /// repo_name)` carries `Assignment::driven_by` — i.e. was dispatched by
    /// `coord drive` at some point, regardless of whether that driver
    /// process is still alive. Unlike `issue_has_live_drive` (a live tmux
    /// sweep that goes blank the instant the process exits), this reads
    /// straight off the durable board data (`self.data.assignments`), so it
    /// still answers "was this driven?" after the driver has exited and the
    /// tmux session is long gone — the exact gap #1499 reported: a dead
    /// driver's stall was indistinguishable from an issue nobody ever drove.
    pub(crate) fn issue_has_drive_provenance(&self, issue_number: u64, repo_name: &str) -> bool {
        self.data.assignments.iter().any(|a| {
            a.repo == repo_name && a.issue_number == issue_number && a.driven_by.is_some()
        })
    }

    /// Resolve the coord-local repo name for a per-issue terminal key
    /// `(repo_slug, issue_number)`. Mirrors the lookup `detail_terminal_cwd`
    /// does — needed here because `drive_detail_terminals`'s lazy-spawn only
    /// has the `(repo_slug, issue_number)` key, while `issue_has_live_drive`
    /// (like every other repo-scoped gate in this app, #983) is keyed on the
    /// coord-local name.
    pub(crate) fn issue_key_coord_repo(&self, issue_key: &(String, u64)) -> Option<String> {
        if let Some(issue) = self
            .pipeline_issues
            .iter()
            .find(|iss| iss.repo_slug == issue_key.0 && iss.number == issue_key.1)
        {
            if let Some(cr) = issue.coord_repo.as_deref() {
                if !cr.is_empty() {
                    return Some(cr.to_string());
                }
            }
        }
        if self.active_view == SidebarView::Board {
            if let Some(coord_repo) = self.board_active_repo() {
                return Some(coord_repo.to_string());
            }
        }
        None
    }

    /// Badge span for a Pipeline row currently (or previously) being driven —
    /// spliced between `#N` and the title, mirroring `epic_badge_span`'s
    /// placement so it can't be clipped by a long title or overwritten by
    /// the row's right-aligned repo-tag badge.
    ///
    /// #1499: three distinguishable states, checked in order —
    /// 1. **live**: a `coord-drive-*` tmux session is running right now →
    ///    `" [driving]"`.
    /// 2. **exited unfinished**: no live session, but some assignment for
    ///    this issue carries `driven_by` (durable board provenance) → `"
    ///    [drive exited]"` — the stall signal #1499/#50 want surfaced: a
    ///    driver ran here and is no longer running, yet the issue is still
    ///    sitting in the Pipeline (a merged/closed issue simply has no
    ///    Pipeline row left to hang this badge on).
    /// 3. **never driven**: neither of the above → no badge (today's
    ///    behaviour, unchanged).
    pub(crate) fn drive_badge_span(&self, issue: &PipelineIssue) -> Option<StyledSpan> {
        let repo = issue.coord_repo.as_deref()?;
        if self.issue_has_live_drive(issue.number, repo) {
            return Some(StyledSpan::with_fg(
                " [driving]".to_string(),
                Color::rgb(120, 200, 255),
            ));
        }
        if self.issue_has_drive_provenance(issue.number, repo) {
            return Some(StyledSpan::with_fg(
                " [drive exited]".to_string(),
                Color::rgb(230, 160, 60),
            ));
        }
        None
    }

    /// Launch `coord drive <repo> <issue> --tmux` for the selected Pipeline
    /// issue (#2634: backs the demoted "Drive locally (tmux, this machine)"
    /// menu action, not the primary "Drive (automated)" one any more — see
    /// module doc) — hands the WHOLE Work → Test → Review → Merge sequence
    /// to `coord drive`, which dispatches every stage itself. Unlike
    /// `launch_interactive_session_on_machine`, this carries no work_aid and
    /// never runs `coord assign`. The pane doesn't stop at the launch
    /// confirmation: `spawn_drive_shell` chains straight into `coord
    /// drive-attach` so the Terminal tab shows the live run immediately,
    /// not a dead prompt (see `DriveShellMode::Launch`'s doc).
    pub(crate) fn launch_drive_for_selected_issue(&mut self) {
        let Some((repo, issue_key)) = self.selected_issue_repo_and_key() else {
            self.pipeline_status = Some((
                "Cannot resolve repo for this issue — drive not started".to_string(),
                Instant::now(),
            ));
            return;
        };
        let issue_num = issue_key.1;
        if self.issue_has_live_drive(issue_num, &repo) {
            self.push_toast(
                "Drive locally (tmux)",
                "A drive is already running for this issue — use Attach instead.",
                ToastSeverity::Warning,
            );
            return;
        }
        self.spawn_drive_shell(repo, issue_key, DriveShellMode::Launch);
    }

    /// Attach to a live drive session for the selected issue (#1398's
    /// "Attach to drive" menu action).
    pub(crate) fn attach_drive_for_selected_issue(&mut self) {
        let Some((repo, issue_key)) = self.selected_issue_repo_and_key() else {
            self.pipeline_status = Some((
                "Cannot resolve repo for this issue — nothing to attach to".to_string(),
                Instant::now(),
            ));
            return;
        };
        self.spawn_drive_shell(repo, issue_key, DriveShellMode::Attach);
    }

    /// Shared shell-spawn body for `launch_drive_for_selected_issue` /
    /// `attach_drive_for_selected_issue` — spawns a plain local shell into
    /// the per-issue terminal map and types the launch/attach command line
    /// into it. Same mechanism every other interactive launch path uses
    /// (`launch_interactive_session_on_machine_inner`): "the local PTY types
    /// the command line, `coord` does the ssh+tmux work."
    fn spawn_drive_shell(&mut self, repo: String, issue_key: (String, u64), mode: DriveShellMode) {
        let issue_num = issue_key.1;
        let cfg_path = self
            .command_runner
            .config_path
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned());
        let (cols, rows) = self.detail_terminal_pending_dims.get().unwrap_or((80, 24));
        let cwd = self.detail_terminal_cwd(&issue_key);
        let shell = quadraui::terminal_engine::default_shell();
        match quadraui::terminal_engine::TerminalSession::spawn(
            cols.max(20),
            rows.max(5),
            &shell,
            &cwd,
            10_000,
        ) {
            Ok(mut sess) => {
                let line = match mode {
                    DriveShellMode::Launch => {
                        // `--config` is a per-SUBCOMMAND option — must come
                        // AFTER `drive`, not before it (same rule the
                        // `coord reattach`/`coord assign` launch lines follow).
                        // `drive-attach` takes no `--config` (it's a bare
                        // `tmux attach-session`, no coord config to load —
                        // see `coord/commands/drive.py::drive_attach`), so
                        // the flag is only threaded into the first half of
                        // the chain.
                        let cfg = match cfg_path.as_deref() {
                            Some(p) if !p.is_empty() => {
                                format!("--config {} ", shell_quote_arg(p))
                            }
                            _ => String::new(),
                        };
                        // Chain straight into `drive-attach` once the launch
                        // succeeds (see `DriveShellMode::Launch`'s doc for why
                        // this is race-free) — otherwise this pane shows the
                        // launch confirmation and then sits at a dead shell
                        // prompt instead of the live run, defeating the
                        // issue's headline acceptance criterion.
                        format!(
                            "coord drive {}{} {} --tmux && coord drive-attach {} {}\r",
                            cfg,
                            shell_quote_arg(&repo),
                            issue_num,
                            shell_quote_arg(&repo),
                            issue_num,
                        )
                    }
                    DriveShellMode::Attach => {
                        format!(
                            "coord drive-attach {} {}\r",
                            shell_quote_arg(&repo),
                            issue_num,
                        )
                    }
                };
                sess.send_str(&line);
                self.detail_terminal_sessions.insert(issue_key.clone(), sess);
                self.detail_terminal_spawn_errors.remove(&issue_key);

                if mode == DriveShellMode::Launch {
                    // #559-style optimistic entry so the badge/menu flip to
                    // "driving" immediately, without waiting for the next
                    // discovery sweep. Merged away (or evicted) by
                    // `poll_drive_sessions`.
                    self.drive_sessions
                        .retain(|s| !(s.pending && s.issue == issue_num && s.repo == repo));
                    self.drive_sessions.push(DriveSession {
                        repo: repo.clone(),
                        issue: issue_num,
                        attached: false,
                        pending: true,
                        pending_sweep_count: 0,
                    });
                    // The "[driving]" badge is baked into the cached Pipeline
                    // sidebar tree at `rebuild_pipeline_sidebar` time (it
                    // isn't recomputed on every render like the context
                    // menu, which reads `drive_sessions` fresh whenever it's
                    // opened) — without this call the optimistic entry above
                    // is real but invisible until the next unrelated board
                    // refresh rebuilds the tree, contradicting "immediately"
                    // in the comment above. `None` lets it recapture the
                    // current selection itself; nothing about launching a
                    // drive changes which issue is selected.
                    self.rebuild_pipeline_sidebar(None);
                    self.pipeline_status = Some((
                        format!("Driving {repo} #{issue_num} — Work → Test → Review → Merge"),
                        Instant::now(),
                    ));
                    // #2634: the launch was the quietest action in this
                    // module — only the transient status line above plus an
                    // optimistic badge, no toast, unlike `confirm_kill_drive`'s
                    // "Drive stopped" and the already-driving warning toast
                    // just above in this same function. Symmetric with both.
                    self.push_toast(
                        "Drive locally (tmux)",
                        &format!(
                            "driving {repo} #{issue_num} on this machine (local, ephemeral — \
                             not visible in the Queue panel)"
                        ),
                        ToastSeverity::Success,
                    );
                }
            }
            Err(e) => {
                // #2634: a failed PTY spawn used to be stashed into
                // `detail_terminal_spawn_errors` with no toast and no status
                // line at all — completely silent, indistinguishable from a
                // successful launch until the operator went looking for the
                // (missing) live run. Name the failure out loud, same
                // severity/posture as every other launch-failure toast in
                // this app.
                let verb = match mode {
                    DriveShellMode::Launch => "launch a local drive for",
                    DriveShellMode::Attach => "attach to the drive for",
                };
                self.push_toast(
                    "Drive locally (tmux)",
                    &format!("failed to {verb} {repo} #{issue_num}: {e}"),
                    ToastSeverity::Error,
                );
                self.detail_terminal_spawn_errors
                    .insert(issue_key, e.to_string());
            }
        }
    }

    /// Fire the confirmed "Stop drive" (#1398): `coord drive-stop <repo>
    /// <issue>` kills the tmux session, which releases the driver's
    /// per-issue flock — this IS the correct cancellation, no extra cleanup
    /// needed (see module docs). Removes the optimistic entry immediately;
    /// reconciled by the next discovery sweep or a TUI restart either way.
    pub(crate) fn confirm_kill_drive(&mut self, killed: PendingKillDrive) {
        let issue_str = killed.issue.to_string();
        self.command_runner
            .spawn_queued(&["drive-stop", &killed.repo, &issue_str]);

        self.drive_sessions
            .retain(|s| !(s.repo == killed.repo && s.issue == killed.issue));

        // Symmetric with the launch side in `spawn_drive_shell`: the
        // "[driving]" badge is baked into the cached Pipeline sidebar tree,
        // so removing the session from `drive_sessions` alone leaves a
        // stale badge on screen until the next unrelated board refresh.
        self.rebuild_pipeline_sidebar(None);

        self.push_toast(
            "Drive stopped",
            &format!("{} #{}", killed.repo, killed.issue),
            ToastSeverity::Info,
        );
    }

    /// Drain the background drive-session discovery sweep (#1398). Mirrors
    /// `poll_remote_terminals`'s pending-entry merge, keyed on `(repo,
    /// issue)` instead of `(machine, name)`. Returns `true` on update.
    pub(crate) fn poll_drive_sessions(&mut self) -> bool {
        let Some(rx) = self.pending_drive_sessions.as_ref() else {
            return false;
        };
        match rx.try_recv() {
            Ok(sessions) => {
                let covered: std::collections::HashSet<(String, u64)> = sessions
                    .iter()
                    .map(|s| (s.repo.clone(), s.issue))
                    .collect();
                let surviving_pending: Vec<DriveSession> = self
                    .drive_sessions
                    .drain(..)
                    .filter_map(|mut s| {
                        if !s.pending {
                            return None; // real entries are replaced by discovery
                        }
                        if covered.contains(&(s.repo.clone(), s.issue)) {
                            return None; // real session appeared → drop optimistic
                        }
                        s.pending_sweep_count = s.pending_sweep_count.saturating_add(1);
                        if s.pending_sweep_count > Self::PENDING_DRIVE_SWEEP_BUDGET {
                            return None; // budget exhausted → evict phantom
                        }
                        Some(s)
                    })
                    .collect();
                self.drive_sessions = surviving_pending;
                self.drive_sessions.extend(sessions);
                self.pending_drive_sessions = None;
                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.pending_drive_sessions = None;
                false
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::fixtures::{make_assignment_typed, make_test_app};

    fn pipeline_issue(number: u64, coord_repo: Option<&str>) -> PipelineIssue {
        PipelineIssue {
            number,
            title: format!("issue {number}"),
            body: String::new(),
            repo_slug: format!("acme/{}", coord_repo.unwrap_or("unmapped")),
            coord_repo: coord_repo.map(str::to_string),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string()],
            is_closed: false,
            body_truncated: false,
            body_len: None,
        }
    }

    // ── parse_drive_sessions_json ────────────────────────────────────────────

    #[test]
    fn parse_drive_sessions_json_extracts_fields() {
        let json = r#"[{"repo":"myrepo","issue":42,"session_name":"coord-drive-myrepo-42","attached":true}]"#;
        let got = parse_drive_sessions_json(json);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].repo, "myrepo");
        assert_eq!(got[0].issue, 42);
        assert!(got[0].attached);
        assert!(!got[0].pending);
        assert_eq!(got[0].pending_sweep_count, 0);
    }

    #[test]
    fn parse_drive_sessions_json_attached_defaults_false_when_absent() {
        let json = r#"[{"repo":"myrepo","issue":42}]"#;
        let got = parse_drive_sessions_json(json);
        assert_eq!(got.len(), 1);
        assert!(!got[0].attached);
    }

    #[test]
    fn parse_drive_sessions_json_empty_array() {
        assert!(parse_drive_sessions_json("[]").is_empty());
    }

    #[test]
    fn parse_drive_sessions_json_malformed_returns_empty() {
        assert!(parse_drive_sessions_json("not json").is_empty());
        assert!(parse_drive_sessions_json(r#"{"not":"an array"}"#).is_empty());
    }

    #[test]
    fn parse_drive_sessions_json_missing_required_field_skips_entry() {
        // `issue` missing entirely — the whole entry must be dropped, not panic.
        let json = r#"[{"repo":"myrepo"},{"repo":"other","issue":7}]"#;
        let got = parse_drive_sessions_json(json);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].repo, "other");
    }

    // ── issue_has_live_drive / issue_key_coord_repo / drive_badge_span ──────

    #[test]
    fn issue_has_live_drive_matches_repo_and_issue() {
        let mut app = make_test_app(BoardData::default());
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];
        assert!(app.issue_has_live_drive(42, "myrepo"));
        assert!(!app.issue_has_live_drive(42, "other-repo"), "must not cross repos");
        assert!(!app.issue_has_live_drive(7, "myrepo"), "must not cross issues");
    }

    #[test]
    fn issue_has_live_drive_false_when_no_sessions() {
        let app = make_test_app(BoardData::default());
        assert!(!app.issue_has_live_drive(42, "myrepo"));
    }

    #[test]
    fn issue_key_coord_repo_resolves_via_pipeline_issues() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        assert_eq!(
            app.issue_key_coord_repo(&("acme/myrepo".to_string(), 42)),
            Some("myrepo".to_string())
        );
    }

    #[test]
    fn issue_key_coord_repo_none_when_unknown_and_not_board_view() {
        let app = make_test_app(BoardData::default());
        assert_eq!(app.issue_key_coord_repo(&("acme/myrepo".to_string(), 42)), None);
    }

    #[test]
    fn drive_badge_span_some_when_driving() {
        let mut app = make_test_app(BoardData::default());
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: true,
            pending_sweep_count: 0,
        }];
        let issue = pipeline_issue(42, Some("myrepo"));
        assert!(app.drive_badge_span(&issue).is_some());
    }

    #[test]
    fn drive_badge_span_none_when_not_driving() {
        let app = make_test_app(BoardData::default());
        let issue = pipeline_issue(42, Some("myrepo"));
        assert!(app.drive_badge_span(&issue).is_none());
    }

    #[test]
    fn drive_badge_span_none_when_repo_unmapped() {
        let mut app = make_test_app(BoardData::default());
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];
        let issue = pipeline_issue(42, None);
        assert!(
            app.drive_badge_span(&issue).is_none(),
            "an issue with no coord_repo can never match a repo-scoped drive session"
        );
    }

    // ── #1499: durable drive provenance (issue_has_drive_provenance / the
    // "drive exited" badge state) ────────────────────────────────────────────

    fn driven_assignment(issue: u64, repo: &str) -> Assignment {
        let mut a = make_assignment_typed("done", issue, repo, Some("work"));
        a.driven_by = Some(format!("drive:{repo}#{issue}"));
        a
    }

    #[test]
    fn issue_has_drive_provenance_true_when_an_assignment_carries_driven_by() {
        let app = make_test_app(BoardData {
            assignments: vec![driven_assignment(42, "myrepo")],
            ..Default::default()
        });
        assert!(app.issue_has_drive_provenance(42, "myrepo"));
    }

    #[test]
    fn issue_has_drive_provenance_false_when_no_assignment_carries_driven_by() {
        let app = make_test_app(BoardData {
            assignments: vec![make_assignment_typed("done", 42, "myrepo", Some("work"))],
            ..Default::default()
        });
        assert!(
            !app.issue_has_drive_provenance(42, "myrepo"),
            "a hand-dispatched assignment (driven_by=None) must not read as drive provenance"
        );
    }

    #[test]
    fn issue_has_drive_provenance_scoped_to_repo_and_issue() {
        let app = make_test_app(BoardData {
            assignments: vec![driven_assignment(42, "myrepo")],
            ..Default::default()
        });
        assert!(
            !app.issue_has_drive_provenance(42, "other-repo"),
            "must not cross repos"
        );
        assert!(
            !app.issue_has_drive_provenance(7, "myrepo"),
            "must not cross issues"
        );
    }

    #[test]
    fn drive_badge_span_shows_drive_exited_when_provenance_but_not_live() {
        let app = make_test_app(BoardData {
            assignments: vec![driven_assignment(42, "myrepo")],
            ..Default::default()
        });
        let issue = pipeline_issue(42, Some("myrepo"));
        let span = app
            .drive_badge_span(&issue)
            .expect("a driven, no-longer-live issue must still carry a badge");
        assert_eq!(span.text, " [drive exited]");
    }

    #[test]
    fn drive_badge_span_prefers_live_over_drive_exited() {
        let mut app = make_test_app(BoardData {
            assignments: vec![driven_assignment(42, "myrepo")],
            ..Default::default()
        });
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];
        let issue = pipeline_issue(42, Some("myrepo"));
        let span = app.drive_badge_span(&issue).expect("must carry a badge");
        assert_eq!(
            span.text, " [driving]",
            "a live session must win over stale provenance from an earlier run"
        );
    }

    #[test]
    fn drive_badge_span_none_when_never_driven() {
        let app = make_test_app(BoardData {
            assignments: vec![make_assignment_typed("done", 42, "myrepo", Some("work"))],
            ..Default::default()
        });
        let issue = pipeline_issue(42, Some("myrepo"));
        assert!(
            app.drive_badge_span(&issue).is_none(),
            "an issue with no drive_sessions entry and no driven_by provenance must carry no badge"
        );
    }

    /// Black-box `TuiDriver` coverage (#1499): the same board state must
    /// render THREE distinguishable Pipeline rows side by side — a live
    /// drive, a drive that exited without finishing, and an issue that was
    /// never driven — through the real `event → handle → render` path
    /// (`quadraui::tui::testing::driver_with_shell`), matching this repo's
    /// CLAUDE.md policy for coord-tui behaviour changes. Asserts straight
    /// off the rendered screen grid, not the internal `drive_badge_span`
    /// return value the unit tests above already pin.
    #[test]
    fn tuidriver_pipeline_distinguishes_live_exited_and_never_driven() {
        use quadraui::tui::testing::driver_with_shell;

        // All three get a plain "done" work assignment (only #43's carries
        // `driven_by`) so all three land in the SAME Pipeline lifecycle
        // bucket ("No milestone" under one repo group) — otherwise an
        // issue with no board assignment at all sorts into a different
        // top-level bucket ("New") than one with a completed assignment
        // ("In-progress"), and the test would need to expand two separate
        // collapsed headers instead of one.
        let mut app = make_test_app(BoardData {
            assignments: vec![
                make_assignment_typed("done", 42, "myrepo", Some("work")),
                driven_assignment(43, "myrepo"),
                make_assignment_typed("done", 44, "myrepo", Some("work")),
            ],
            ..Default::default()
        });
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];
        app.pipeline_issues = vec![
            pipeline_issue(42, Some("myrepo")),
            pipeline_issue(43, Some("myrepo")),
            pipeline_issue(44, Some("myrepo")),
        ];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        // Bakes the sidebar tree (including drive badge spans) from the
        // fixture's `pipeline_issues` — mirrors what a real board refresh /
        // selection change does before any row is ever on screen.
        app.rebuild_pipeline_sidebar(None);

        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 140, 40);

        // All three land in the "No milestone" bucket, collapsed by default —
        // expand it so the rows are actually on screen (mirrors the
        // `tuidriver_drive_automated_menu_item_switches_to_terminal_and_shows_badge`
        // precedent above).
        let screen_before = driver.screen();
        let (label_x, label_y) = driver.find("No milestone").unwrap_or_else(|| {
            panic!("'No milestone' bucket header not found:\n{screen_before}")
        });
        // The chevron hit-test region is `[row + 0.5, row + 1.5)` here (three
        // grouped issues push the fractional-anchor boundary down half a
        // row versus the single-issue precedent above) — same nudge
        // rationale as the context-menu click adjustments elsewhere in this
        // file/`tests.rs`; verified empirically against this exact fixture.
        driver.click((label_x - 2.0).max(0.0), label_y + 0.5);

        let screen = driver.screen();
        assert!(
            driver.find("#42").is_some(),
            "issue #42 (live drive) must be on screen:\n{screen}"
        );
        assert!(
            driver.find("#43").is_some(),
            "issue #43 (drive exited) must be on screen:\n{screen}"
        );
        assert!(
            driver.find("#44").is_some(),
            "issue #44 (never driven) must be on screen:\n{screen}"
        );
        assert!(
            screen.contains("[driving]"),
            "the live drive (#42) must render the '[driving]' badge:\n{screen}"
        );
        assert!(
            screen.contains("[drive exited]"),
            "the drive that exited without finishing (#43) must render the \
             '[drive exited]' badge, distinguishable from both '[driving]' \
             and no badge at all:\n{screen}"
        );
    }

    // ── launch / attach ──────────────────────────────────────────────────────

    #[test]
    fn launch_drive_for_selected_issue_spawns_shell_and_marks_pending() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;

        app.launch_drive_for_selected_issue();

        assert!(
            app.detail_terminal_sessions
                .contains_key(&("acme/myrepo".to_string(), 42)),
            "must spawn the per-issue terminal shell"
        );
        assert!(
            app.drive_sessions
                .iter()
                .any(|s| s.repo == "myrepo" && s.issue == 42 && s.pending),
            "must optimistically mark the issue as driving"
        );
    }

    /// #1398 review: the reviewer asked for a test that asserts "the
    /// dispatched `coord drive ... --tmux` invocation (or the shell-typed
    /// command line) for the right repo/issue" — `spawn_drive_shell` types
    /// the launch line into a REAL PTY (no `CommandRunner` involved for
    /// launch/attach, only for `confirm_kill_drive`), so the only way to
    /// observe it is to read back what the shell echoes, same pattern as
    /// `paste_to_pty_bracketed_when_mode_2004_enabled` /
    /// `paste_to_detail_terminal_bracketed_when_mode_2004_enabled` in
    /// `tests.rs`.
    ///
    /// #1398 review round 2: also asserts the launch line CHAINS into
    /// `coord drive-attach` — the actual fix for "Drive (automated) never
    /// attaches the Terminal tab to the live run." Without the `&&
    /// coord drive-attach …` suffix this pane would show only the launch
    /// confirmation and then sit at a dead shell prompt.
    #[test]
    #[cfg(unix)]
    fn launch_drive_types_the_coord_drive_command_line_for_the_right_repo_and_issue() {
        use std::time::{Duration, Instant};

        fn poll_until(
            sess: &mut quadraui::terminal_engine::TerminalSession,
            max_ms: u64,
            predicate: impl Fn(&quadraui::terminal_engine::TerminalSession) -> bool,
        ) -> bool {
            let start = Instant::now();
            let limit = Duration::from_millis(max_ms);
            while start.elapsed() < limit {
                sess.poll();
                if predicate(sess) {
                    return true;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            false
        }

        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.detail_terminal_pending_dims.set(Some((80, 24)));

        app.launch_drive_for_selected_issue();

        let sess = app
            .detail_terminal_sessions
            .get_mut(&("acme/myrepo".to_string(), 42))
            .expect("must spawn the per-issue terminal shell");

        assert!(
            poll_until(sess, 5_000, |s| s.screen_text().contains("coord drive")),
            "the typed launch line must reach the shell's echoed screen; got:\n{}",
            sess.screen_text()
        );
        let screen = sess.screen_text();
        assert!(
            screen.contains("myrepo"),
            "must target the selected issue's repo; got:\n{screen}"
        );
        assert!(
            screen.contains("42"),
            "must target the selected issue's number; got:\n{screen}"
        );
        assert!(
            screen.contains("--tmux"),
            "launch must run detached inside tmux, not as a TUI child; got:\n{screen}"
        );
        assert!(
            screen.contains("&& coord drive-attach"),
            "launch must chain straight into `coord drive-attach` so the pane \
             ends up attached to the live run instead of a dead shell prompt \
             after the launch confirmation; got:\n{screen}"
        );
    }

    /// Same as above for `attach_drive_for_selected_issue`: types `coord
    /// drive-attach <repo> <issue>` (no `--tmux` — attach reuses the
    /// already-running session).
    #[test]
    #[cfg(unix)]
    fn attach_drive_types_the_coord_drive_attach_command_line_for_the_right_repo_and_issue() {
        use std::time::{Duration, Instant};

        fn poll_until(
            sess: &mut quadraui::terminal_engine::TerminalSession,
            max_ms: u64,
            predicate: impl Fn(&quadraui::terminal_engine::TerminalSession) -> bool,
        ) -> bool {
            let start = Instant::now();
            let limit = Duration::from_millis(max_ms);
            while start.elapsed() < limit {
                sess.poll();
                if predicate(sess) {
                    return true;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            false
        }

        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.detail_terminal_pending_dims.set(Some((80, 24)));
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];

        app.attach_drive_for_selected_issue();

        let sess = app
            .detail_terminal_sessions
            .get_mut(&("acme/myrepo".to_string(), 42))
            .expect("must spawn the per-issue terminal shell");

        assert!(
            poll_until(sess, 5_000, |s| s.screen_text().contains("coord drive-attach")),
            "the typed attach line must reach the shell's echoed screen; got:\n{}",
            sess.screen_text()
        );
        let screen = sess.screen_text();
        assert!(
            screen.contains("myrepo"),
            "must target the selected issue's repo; got:\n{screen}"
        );
        assert!(
            screen.contains("42"),
            "must target the selected issue's number; got:\n{screen}"
        );
    }

    /// #2634: black-box `TuiDriver` coverage for the fixed marquee flow —
    /// "Right-click a Pipeline row → Drive (automated)" now queues the issue
    /// on the durable drive queue instead of launching an invisible local
    /// tmux session — through the REAL `event → handle → render` path
    /// (`quadraui::tui::testing::driver_with_shell`), not a direct call into
    /// an internal method. Drives a right-click (mirrors
    /// `tuidriver_right_click_opens_board_context_menu`'s full
    /// `UiEvent::MouseDown { button: Right }` chain) on the Pipeline row,
    /// asserts BOTH the primary "Drive (automated)" item AND the demoted
    /// "Drive locally (tmux, this machine)" item render — distinguishable
    /// from each other on screen, addressing the issue's "two adjacent
    /// actions whose names both promise the automated driver" report —
    /// clicks the primary one, and asserts the confirmation toast
    /// `dispatch_drive_queue_add` pushes ("queuing myrepo #42…") lands on
    /// screen. The TuiDriver shell adapter doesn't expose the wrapped
    /// `CoordApp` for a typed `spawned_calls` check (same documented
    /// limitation `tuidriver_add_to_drive_queue_spawns_subprocess_and_toasts`
    /// works around) — that half of the acceptance criterion (the exact
    /// `coord drive-queue add myrepo 42` argv) is covered directly by the
    /// companion test `dispatch_start_drive_queues_via_drive_queue_add`
    /// below.
    #[test]
    fn tuidriver_drive_automated_menu_item_queues_via_drive_queue_add() {
        use quadraui::tui::testing::driver_with_shell;

        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        // Bakes the sidebar tree (including the drive badge span) from the
        // fixture's `pipeline_issues` — mirrors what a real board refresh /
        // selection change does before the row is ever on screen.
        app.rebuild_pipeline_sidebar(None);

        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 140, 40);

        // The lone issue has no milestone, so it lands in the "No milestone"
        // bucket, collapsed by default (mirrors `tuidriver_pipeline_new_
        // milestone_chevron_click_expands_and_persists`'s precedent) — expand
        // it first so "#42" is actually on screen to click.
        let screen_before = driver.screen();
        let (label_x, label_y) = driver.find("No milestone").unwrap_or_else(|| {
            panic!("'No milestone' bucket header not found:\n{screen_before}")
        });
        driver.click((label_x - 2.0).max(0.0), label_y);

        let (x, y) = driver.find("#42").unwrap_or_else(|| {
            panic!(
                "#2634: could not find Pipeline row '#42' after expanding its \
                 bucket:\n{}",
                driver.screen()
            )
        });

        // Right-click the row — the same full `UiEvent::MouseDown { button:
        // Right }` → `handle` → `open_context_menu` chain a real right-click
        // takes (`tuidriver_right_click_opens_board_context_menu`'s pattern),
        // not a direct `open_context_menu`/`dispatch_context_menu_action` call.
        driver.dispatch(UiEvent::MouseDown {
            widget: None,
            button: MouseButton::Right,
            position: Point::new(x, y),
            modifiers: Modifiers::default(),
        });

        let menu_screen = driver.screen();
        assert!(
            menu_screen.contains("Drive (automated)"),
            "right-click on an idle Pipeline row must offer the primary \
             'Drive (automated)' item:\n{menu_screen}",
        );
        assert!(
            menu_screen.contains("Drive locally (tmux, this machine)"),
            "#2634: the demoted local/ephemeral alternative must render \
             alongside the primary item, distinguishably named so the two \
             can never again be mistaken for one another:\n{menu_screen}",
        );

        let (dx, dy) = driver.find("Drive (automated)").unwrap_or_else(|| {
            panic!(
                "#2634: could not find 'Drive (automated)' menu item on screen:\n{}",
                driver.screen()
            )
        });
        // `find` returns the row's screen-centre, but the context menu's
        // fractional-anchor layout hit-tests `[row + 0.5, row + 1.5)` — one
        // item-height below where it visibly renders (see the identical
        // nudge + rationale on the "Author acceptance tests (interactive)"
        // click above `tuidriver_author_acceptance_tests_interactive_menu_
        // item_dispatches`).
        driver.click(dx, dy - 0.1);

        let screen = driver.screen();
        assert!(
            screen.contains("queuing myrepo #42"),
            "#2634: clicking the primary 'Drive (automated)' item must \
             toast the drive-queue confirmation ('queuing myrepo #42…'), \
             not silently launch a local tmux session; got:\n{screen}",
        );
    }

    /// #2634: the demoted "Drive locally (tmux, this machine)" item is what
    /// "Drive (automated)" used to be — a local, ephemeral `coord drive
    /// --tmux` launch that switches the Terminal tab to the live run and
    /// flips the row's `[driving]` badge. Same TuiDriver shape as the test
    /// above, retargeted at the renamed item, so the OLD behavior stays
    /// covered under its new name rather than disappearing along with the
    /// rename.
    #[test]
    fn tuidriver_drive_locally_menu_item_switches_to_terminal_and_shows_badge() {
        use quadraui::tui::testing::driver_with_shell;

        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.rebuild_pipeline_sidebar(None);

        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 140, 40);

        let screen_before = driver.screen();
        assert!(
            screen_before.contains("Gates"),
            "sanity: must start on the Overview tab (renders the Repo/Local/ \
             Labels/Gates meta block) before anything is clicked:\n{screen_before}"
        );

        let (label_x, label_y) = driver.find("No milestone").unwrap_or_else(|| {
            panic!("'No milestone' bucket header not found:\n{screen_before}")
        });
        driver.click((label_x - 2.0).max(0.0), label_y);

        let (x, y) = driver.find("#42").unwrap_or_else(|| {
            panic!(
                "#2634: could not find Pipeline row '#42' after expanding its \
                 bucket:\n{}",
                driver.screen()
            )
        });

        driver.dispatch(UiEvent::MouseDown {
            widget: None,
            button: MouseButton::Right,
            position: Point::new(x, y),
            modifiers: Modifiers::default(),
        });

        let (dx, dy) = driver
            .find("Drive locally (tmux, this machine)")
            .unwrap_or_else(|| {
                panic!(
                    "#2634: could not find 'Drive locally (tmux, this machine)' \
                     menu item on screen:\n{}",
                    driver.screen()
                )
            });
        driver.click(dx, dy - 0.1);

        // Tab-switch: the Overview-only Repo/Local/Labels/Gates meta block
        // (`pipeline_tab_body_list`, rendered ONLY when `pipeline_detail_tab
        // == Overview`) must be gone — the pinned stage strip that every
        // OTHER tab (Terminal included) shows above its own body is not
        // enough on its own to prove the tab changed (it renders on every
        // non-Overview tab), so its disappearance is the honest on-screen
        // signal that we left Overview. The exact destination
        // (`PipelineDetailTab::Terminal`, not some other tab) is pinned by
        // the direct-call companion test
        // `dispatch_start_drive_local_launches_and_switches_to_terminal_tab`,
        // which the TuiDriver shell adapter doesn't expose a typed field to
        // re-check here (same split as
        // `sessions_panel_stop_running_session_shows_feedback_toast`).
        let screen = driver.screen();
        assert!(
            !screen.contains("Gates"),
            "clicking 'Drive locally (tmux, this machine)' must switch away \
             from the Overview tab (to Terminal) so the live run is what's \
             shown, not a launch confirmation left sitting on Overview:\n{screen}",
        );
        assert!(
            screen.contains("[driving]"),
            "the Pipeline row must carry the '[driving]' badge once a local \
             drive has been launched for it:\n{screen}",
        );
    }

    #[test]
    fn launch_drive_for_selected_issue_refuses_when_already_driving() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];

        app.launch_drive_for_selected_issue();

        assert!(
            !app.detail_terminal_sessions
                .contains_key(&("acme/myrepo".to_string(), 42)),
            "must NOT spawn a second launch — Attach is the correct action"
        );
        assert_eq!(
            app.drive_sessions.len(),
            1,
            "must not add a duplicate/pending entry"
        );
    }

    #[test]
    fn attach_drive_for_selected_issue_spawns_shell() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];

        app.attach_drive_for_selected_issue();

        assert!(
            app.detail_terminal_sessions
                .contains_key(&("acme/myrepo".to_string(), 42)),
            "must spawn the per-issue terminal shell"
        );
    }

    // ── confirm_kill_drive ────────────────────────────────────────────────────

    #[test]
    fn confirm_kill_drive_dispatches_stop_and_removes_optimistically() {
        let mut app = make_test_app(BoardData::default());
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];

        app.confirm_kill_drive(PendingKillDrive {
            repo: "myrepo".to_string(),
            issue: 42,
        });

        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "drive-stop".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
            ]],
            "must dispatch `coord drive-stop myrepo 42`; got {:?}",
            app.command_runner.spawned_calls,
        );
        assert!(
            app.drive_sessions.is_empty(),
            "killed drive must be removed from drive_sessions optimistically"
        );
    }

    // ── poll_drive_sessions ───────────────────────────────────────────────────

    #[test]
    fn poll_drive_sessions_replaces_pending_with_real_entry() {
        let mut app = make_test_app(BoardData::default());
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: true,
            pending_sweep_count: 0,
        }];
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send(vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: true,
            pending: false,
            pending_sweep_count: 0,
        }])
        .unwrap();
        app.pending_drive_sessions = Some(rx);

        assert!(app.poll_drive_sessions());

        assert_eq!(app.drive_sessions.len(), 1);
        assert!(app.drive_sessions[0].attached, "the REAL entry must win, not the stale pending one");
        assert!(!app.drive_sessions[0].pending);
        assert!(app.pending_drive_sessions.is_none());
    }

    #[test]
    fn poll_drive_sessions_survives_uncovered_pending_until_budget_exhausted() {
        let mut app = make_test_app(BoardData::default());
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: true,
            pending_sweep_count: 0,
        }];

        // Sweep 1: empty result, not yet covered — survives (count -> 1).
        let (tx1, rx1) = std::sync::mpsc::channel();
        tx1.send(Vec::new()).unwrap();
        app.pending_drive_sessions = Some(rx1);
        assert!(app.poll_drive_sessions());
        assert_eq!(app.drive_sessions.len(), 1, "sweep 1: phantom survives");
        assert_eq!(app.drive_sessions[0].pending_sweep_count, 1);

        // Sweep 2: still empty — survives (count -> 2 == budget).
        let (tx2, rx2) = std::sync::mpsc::channel();
        tx2.send(Vec::new()).unwrap();
        app.pending_drive_sessions = Some(rx2);
        assert!(app.poll_drive_sessions());
        assert_eq!(app.drive_sessions.len(), 1, "sweep 2: still within budget");
        assert_eq!(app.drive_sessions[0].pending_sweep_count, 2);

        // Sweep 3: still empty — budget exceeded, evicted.
        let (tx3, rx3) = std::sync::mpsc::channel();
        tx3.send(Vec::new()).unwrap();
        app.pending_drive_sessions = Some(rx3);
        assert!(app.poll_drive_sessions());
        assert!(
            app.drive_sessions.is_empty(),
            "sweep 3: phantom must be evicted once the sweep budget is exhausted"
        );
    }

    #[test]
    fn poll_drive_sessions_empty_channel_returns_false_without_clearing() {
        let mut app = make_test_app(BoardData::default());
        let (_tx, rx) = std::sync::mpsc::channel::<Vec<DriveSession>>();
        app.pending_drive_sessions = Some(rx);
        assert!(!app.poll_drive_sessions());
        assert!(
            app.pending_drive_sessions.is_some(),
            "an empty (not-yet-landed) channel must not clear the pending slot"
        );
    }

    #[test]
    fn poll_drive_sessions_disconnected_channel_clears_pending() {
        let mut app = make_test_app(BoardData::default());
        let (tx, rx) = std::sync::mpsc::channel::<Vec<DriveSession>>();
        drop(tx);
        app.pending_drive_sessions = Some(rx);
        assert!(!app.poll_drive_sessions());
        assert!(app.pending_drive_sessions.is_none());
    }

    // ── context menu ──────────────────────────────────────────────────────────

    #[test]
    fn context_menu_offers_drive_when_not_driving() {
        let app = make_test_app(BoardData::default());
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::New,
            Some("myrepo"),
        );
        assert!(
            items.iter().any(|i| i.label == "Drive (automated)"),
            "Drive (automated) must be offered when nothing is live"
        );
        // #2634: the demoted local/ephemeral alternative must render
        // alongside the primary queue-backed item, distinctly labelled —
        // the whole point of the rename is that the two can never again be
        // mistaken for one another.
        assert!(
            items
                .iter()
                .any(|i| i.label == "Drive locally (tmux, this machine)"),
            "the demoted local-tmux launcher must also be offered when \
             nothing is live"
        );
        assert!(!items.iter().any(|i| i.label == "Attach to drive"));
        assert!(!items.iter().any(|i| i.label == "Stop drive"));
    }

    #[test]
    fn context_menu_collapses_to_attach_and_stop_when_driving() {
        let mut app = make_test_app(BoardData::default());
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::InProgress,
            Some("myrepo"),
        );
        assert!(items.iter().any(|i| i.label == "Attach to drive"));
        assert!(items.iter().any(|i| i.label == "Stop drive"));
        assert!(
            !items.iter().any(|i| i.label == "Drive (automated)"),
            "the one-click Drive launcher must not be offered while already driving"
        );
        assert!(
            !items
                .iter()
                .any(|i| i.label == "Drive locally (tmux, this machine)"),
            "#2634: the demoted local-tmux launcher must not be offered \
             while already driving either"
        );
        assert!(
            !items.iter().any(|i| i.label == "Start (interactive)"),
            "#1398: conflicting single-stage actions must be greyed out (omitted) while driving"
        );
        assert!(!items.iter().any(|i| i.label == "Start (automated)"));
    }

    #[test]
    fn context_menu_drive_scoped_to_repo_no_cross_repo_leak() {
        // A live drive for repo-a/#42 must not affect repo-b/#42's menu (#983
        // precedent — every other repo-scoped gate in this file follows it).
        let mut app = make_test_app(BoardData::default());
        app.drive_sessions = vec![DriveSession {
            repo: "repo-a".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::New,
            Some("repo-b"),
        );
        assert!(items.iter().any(|i| i.label == "Drive (automated)"));
        assert!(!items.iter().any(|i| i.label == "Attach to drive"));
    }

    // ── dispatch_context_menu_action ────────────────────────────────────────

    fn pipeline_target_with_repo(issue_number: u64, repo: &str) -> ContextMenuTarget {
        ContextMenuTarget::PipelineRow {
            issue_number: Some(issue_number),
            repo_name: Some(repo.to_string()),
            lifecycle: PipelineRowLifecycle::InProgress,
        }
    }

    /// #2634: the primary "Drive (automated)" action now queues the issue
    /// on the durable drive queue (same seam as `"drive-queue-add"`) instead
    /// of launching a local tmux session — companion direct-call test to
    /// `tuidriver_drive_automated_menu_item_queues_via_drive_queue_add`,
    /// which can't see `command_runner` through the TuiDriver shell
    /// adapter. Mirrors `dispatch_context_menu_action_drive_queue_add_
    /// spawns_subprocess_for_board_row`'s assertion shape (`tests.rs`).
    #[test]
    fn dispatch_start_drive_queues_via_drive_queue_add() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.pipeline_detail_tab = PipelineDetailTab::Summary;

        let target = pipeline_target_with_repo(42, "myrepo");
        let handled = app.dispatch_context_menu_action("start-drive", &target);

        assert!(handled);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "drive-queue".to_string(),
                "add".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
            ]],
            "must dispatch `coord drive-queue add myrepo 42`; got {:?}",
            app.command_runner.spawned_calls,
        );
        assert!(
            app.drive_queue_contains("myrepo", 42),
            "must optimistically reflect the queue entry"
        );
        // Queueing starts nothing locally — there is no live run yet for a
        // Terminal tab to show, so this must NOT switch tabs or spawn a PTY
        // (unlike the demoted "start-drive-local" action below).
        assert_eq!(
            app.pipeline_detail_tab,
            PipelineDetailTab::Summary,
            "queueing must not switch the detail tab away from where the \
             operator was"
        );
        assert!(!app
            .detail_terminal_sessions
            .contains_key(&("acme/myrepo".to_string(), 42)));
    }

    /// #2634: the demoted "Drive locally (tmux, this machine)" action is
    /// what "start-drive" used to do — local `coord drive --tmux` launch,
    /// switching the Terminal tab to the live run.
    #[test]
    fn dispatch_start_drive_local_launches_and_switches_to_terminal_tab() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.pipeline_detail_tab = PipelineDetailTab::Summary;

        let target = pipeline_target_with_repo(42, "myrepo");
        let handled = app.dispatch_context_menu_action("start-drive-local", &target);

        assert!(handled);
        assert_eq!(app.pipeline_detail_tab, PipelineDetailTab::Terminal);
        assert!(app
            .detail_terminal_sessions
            .contains_key(&("acme/myrepo".to_string(), 42)));
    }

    #[test]
    fn dispatch_attach_drive_attaches_and_switches_to_terminal_tab() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.pipeline_detail_tab = PipelineDetailTab::Summary;
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];

        let target = pipeline_target_with_repo(42, "myrepo");
        let handled = app.dispatch_context_menu_action("attach-drive", &target);

        assert!(handled);
        assert_eq!(app.pipeline_detail_tab, PipelineDetailTab::Terminal);
        assert!(app
            .detail_terminal_sessions
            .contains_key(&("acme/myrepo".to_string(), 42)));
    }

    #[test]
    fn dispatch_stop_drive_arms_confirm_dialog() {
        let mut app = make_test_app(BoardData::default());
        let target = pipeline_target_with_repo(42, "myrepo");

        let handled = app.dispatch_context_menu_action("stop-drive", &target);

        assert!(handled);
        assert_eq!(
            app.pending_kill_drive,
            Some(PendingKillDrive {
                repo: "myrepo".to_string(),
                issue: 42,
            }),
            "must arm the confirm dialog rather than killing directly"
        );
        assert!(
            app.command_runner.spawned_calls.is_empty(),
            "must NOT dispatch drive-stop before the operator confirms"
        );
    }

    #[test]
    fn dispatch_stop_drive_unhandled_when_target_missing_repo() {
        let mut app = make_test_app(BoardData::default());
        let target = ContextMenuTarget::PipelineRow {
            issue_number: Some(42),
            repo_name: None,
            lifecycle: PipelineRowLifecycle::InProgress,
        };
        let handled = app.dispatch_context_menu_action("stop-drive", &target);
        assert!(!handled);
        assert!(app.pending_kill_drive.is_none());
    }

    // ── per-issue Terminal tab auto-attach (`drive_detail_terminals`) ───────

    #[test]
    fn opening_terminal_tab_auto_attaches_to_live_drive() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.pipeline_detail_tab = PipelineDetailTab::Terminal;
        app.drive_sessions = vec![DriveSession {
            repo: "myrepo".to_string(),
            issue: 42,
            attached: false,
            pending: false,
            pending_sweep_count: 0,
        }];
        app.detail_terminal_pending_dims.set(Some((80, 24)));

        app.drive_detail_terminals();

        assert!(
            app.detail_terminal_sessions
                .contains_key(&("acme/myrepo".to_string(), 42)),
            "the lazy-spawn hook must attach a session for the live drive"
        );
    }

    #[test]
    fn opening_terminal_tab_without_live_drive_spawns_plain_shell() {
        let mut app = make_test_app(BoardData::default());
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.pipeline_detail_tab = PipelineDetailTab::Terminal;
        app.detail_terminal_pending_dims.set(Some((80, 24)));

        app.drive_detail_terminals();

        // No live drive → falls back to today's plain shell (unattached).
        assert!(
            app.detail_terminal_sessions
                .contains_key(&("acme/myrepo".to_string(), 42)),
            "must still spawn the default per-issue shell"
        );
    }
}
