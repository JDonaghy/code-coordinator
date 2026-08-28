//! Settings panel and periodic update helpers extracted from `app/mod.rs` (#744).
//!
//! **Import pattern:** `use super::*` is intentional — these methods live on `CoordApp`
//! and need the full parent namespace. See `sessions.rs` for the full rationale.
#[allow(unused_imports)]
use super::*;

/// #1954: how long an SSE watch stream stays latched `done` after hitting
/// the 3-failures/10s limit before `drain_pool_entry` retries it on its own.
/// Deliberately well clear of the 10-second failure-counting window, so a
/// retry attempt can never be mistaken for "still mid failure-burst" — if it
/// fails again, that starts a fresh window rather than immediately
/// re-latching.
const SSE_RETRY_BACKOFF: Duration = Duration::from_secs(20);

// ─── Shared periodic work (called from both handle() and tick()) ─────────────

/// Extract `(repo, issue_number)` from a `CommandResult::label` of the shape
/// `"coord diagnose <repo> <issue> [--stage <stage>] [--reset] [--dry-run]
/// [--json]"` (see `dispatch_diagnose_for_selected_pipeline_row`). Used to
/// build the #935 degraded-fallback dialog when a version-skewed daemon's
/// response has no `DIAGNOSE_JSON:` line — the positional repo/issue are
/// still in the label even though the JSON body never arrived.
fn parse_diagnose_label_repo_issue(label: &str) -> Option<(String, u64)> {
    let mut it = label.split_whitespace();
    if it.next()? != "coord" {
        return None;
    }
    if it.next()? != "diagnose" {
        return None;
    }
    let repo = it.next()?.to_string();
    let issue_number: u64 = it.next()?.parse().ok()?;
    Some((repo, issue_number))
}

/// Extract the `--stage <name>` value from a `coord diagnose …` label, if any.
/// Used to preserve the operator's focused-stage target when we retry a
/// `--json`-rejected dry-run without `--json` (#935).
pub(crate) fn parse_diagnose_label_stage(label: &str) -> Option<String> {
    let mut it = label.split_whitespace();
    while let Some(tok) = it.next() {
        if tok == "--stage" {
            return it.next().map(|s| s.to_string());
        }
    }
    None
}

/// True when a `coord` command failed specifically because it does not
/// recognise the `--json` option — i.e. a version-skewed CLI/daemon that
/// predates #935. Click emits `Error: No such option '--json'.` (exit 2) on
/// stderr with no stdout. We match on the option name so an unrelated failure
/// that merely mentions "json" doesn't trigger the no-`--json` retry.
pub(crate) fn diagnose_json_flag_unsupported(stderr: &str) -> bool {
    let s = stderr.to_ascii_lowercase();
    s.contains("no such option") && s.contains("--json")
}

impl CoordApp {
    /// #935: Best-effort-parse a *legacy* `coord diagnose` text response
    /// (findings `·` lines, actions `✓` lines, `DIAGNOSE_RESULT:` trailer) into
    /// the per-stage options dialog. Used when the daemon/CLI predates #935's
    /// `--json` support — either it ran but emitted no `DIAGNOSE_JSON:` line, or
    /// it rejected `--json` outright and we retried the dry-run without it.
    /// Returns true if a dialog was opened. The dialog is flagged `legacy: true`
    /// so the body notes the degraded source.
    fn open_legacy_diagnose_dialog(&mut self, label: &str, stdout: &str) -> bool {
        let Some((repo, issue_number)) = parse_diagnose_label_repo_issue(label) else {
            return false;
        };
        let out = stdout.trim();
        let trailer = out.lines().rev().find(|l| l.starts_with("DIAGNOSE_RESULT:"));
        let needs_reset = trailer
            .map(|l| l.contains("needs_reset=true"))
            .unwrap_or(false);
        let stage = trailer
            .and_then(|l| l.split_whitespace().find(|tok| tok.starts_with("stage=")))
            .and_then(|tok| tok.strip_prefix("stage="))
            .unwrap_or("work")
            .to_string();
        let findings: Vec<String> = out
            .lines()
            .filter(|l| l.trim_start().starts_with('·'))
            .map(|l| l.trim_start().trim_start_matches('·').trim().to_string())
            .collect();
        let actions_taken: Vec<String> = out
            .lines()
            .filter(|l| l.trim_start().starts_with('✓'))
            .map(|l| l.trim_start().trim_start_matches('✓').trim().to_string())
            .collect();
        let has_phantom_session = self.live_tmux_sessions.iter().any(|s| {
            s.assignment_id.starts_with("pending-")
                && s.issue_number == Some(issue_number)
                && s.repo_name.as_deref() == Some(&repo)
        });
        self.pending_diagnose_dialog = Some(PendingDiagnoseDialog {
            repo,
            issue_number,
            stage,
            findings,
            actions_taken,
            needs_reset,
            has_phantom_session,
            legacy: true,
        });
        true
    }

    /// #935: Open a *minimal* degraded diagnose dialog with no findings. Used
    /// when even the no-`--json` retry couldn't produce parseable output (e.g.
    /// a CLI old enough to also reject `--dry-run`, or one that failed for some
    /// other reason). The operator still gets the Recover / Reset / Clear-phantom
    /// / Dismiss options rather than a dead-end toast — the acceptance bar for
    /// #935 is "a dialog, not a bare toast" even in the worst-case skew.
    fn open_minimal_legacy_diagnose_dialog(&mut self, repo: &str, issue_number: u64) {
        let has_phantom_session = self.live_tmux_sessions.iter().any(|s| {
            s.assignment_id.starts_with("pending-")
                && s.issue_number == Some(issue_number)
                && s.repo_name.as_deref() == Some(&repo)
        });
        self.pending_diagnose_dialog = Some(PendingDiagnoseDialog {
            repo: repo.to_string(),
            issue_number,
            stage: "work".to_string(),
            findings: Vec::new(),
            actions_taken: Vec::new(),
            needs_reset: false,
            has_phantom_session,
            legacy: true,
        });
    }

    /// Time-based housekeeping that must run regardless of whether a UI
    /// event arrived: toast pruning, data auto-refresh, background command
    /// runner draining, pipeline loader polling, and auto-notify when
    /// running assignments exist. Returns true if anything changed and a
    /// redraw is required.
    pub(crate) fn run_periodic_work(&mut self) -> bool {
        let mut needs_redraw = false;

        // Toast pruning
        let before = self.toasts.len();
        self.prune_toasts();
        if self.toasts.len() != before {
            needs_redraw = true;
        }

        // Auto-refresh: kick off background data load when interval elapses.
        // Uses the user-configured cadence from settings (default 5 s); when
        // the cadence is "Off" no automatic reload happens (manual `r` still works).
        //
        // #315: while a chat resume is pending we override the cadence with a
        // 300 ms floor.  Without this, the bind waits up to a full refresh
        // interval (default 5 s) for the new assignment row to appear in
        // `data.assignments` — which feels like "the chat is frozen" to the
        // user even though the worker is already replying.
        let cadence = self.settings.refresh_cadence.as_duration();
        let should_refresh = if self.pending_chat_resume.is_some() {
            self.refreshed_at.elapsed() >= Duration::from_millis(300)
        } else {
            match cadence {
                Some(c) => self.refreshed_at.elapsed() >= c,
                None => false,
            }
        };
        if should_refresh && self.pending_data.is_none() {
            // #955 bug 2 / #954 Gap B: route through `refresh()` (not a
            // bare `start_data_load()` call) so the periodic cadence ALSO
            // re-arms the remote-session (#559) and fleet-terminal (#953)
            // discovery sweeps below — both were previously re-armed only
            // reactively, after a coord-CLI mutation completed INSIDE the
            // TUI (`refresh()`'s other call sites). A `coord terminal new`
            // run from an EXTERNAL shell never triggers one of those, so
            // the Terminal-view tree stayed stale until a full TUI
            // restart picked up the one-shot startup sweep again. Also
            // re-evicts/reconciles an optimistic pending `FleetTerminal`
            // so it doesn't sit showing "(creating…)" indefinitely.
            self.refresh();
            if self.active_view == SidebarView::Pipeline {
                self.maybe_kick_pipeline_loader();
            }
            // #1563: re-arm the paused-machine sweep at the same cadence as
            // the rest of the periodic refresh — picks up out-of-band edits
            // (`coord pause foo` from another terminal or thin client)
            // without polling on every tick. Deliberately NOT folded into
            // `refresh()` itself (see that function's doc comment): unlike
            // the sweeps `refresh()` re-arms, this one makes a real network
            // call on a thin client, and `refresh()` is also called
            // reactively from many single-mutation call sites where that
            // extra request isn't wanted.
            if self.pending_paused_machines.is_none() {
                self.pending_paused_machines = Some(spawn_paused_machines_fetch());
            }
            needs_redraw = true;
        }

        // Poll background command runner
        if let Some(result) = self.command_runner.poll() {
            // Surface command failures via toast — previously they only set
            // a green status-bar message that was easy to miss (and styled
            // the same as success).  Without this, e.g. `coord refine`
            // failing because a label doesn't exist on the repo would leave
            // the user staring at an unchanged board with no feedback.
            //
            // #532: a pull-artifact failure that the dialog is about to surface
            // shouldn't ALSO fire a generic toast — that's redundant feedback.
            // But only suppress when the dialog will actually pick it up: the
            // dialog handler matches on `pending_artifact_pull` carrying the
            // SAME work_id as the completed label.  If a second pull was
            // started in the meantime, `pending_artifact_pull` now points at
            // the newer work_id and the stale result would otherwise be
            // silently dropped (no toast, no dialog).  Check the match first,
            // suppress only when handled.
            //
            // #863: same idea for the Fix-dispatch cap preflight — a
            // `max_review_iterations` refusal is EXPECTED (not a bug) and gets
            // its own force-confirm prompt below, so the generic red toast
            // would be redundant/alarming noise for that one specific case.
            // Any OTHER preflight failure (aid not found, etc.) still gets the
            // normal toast — the operator needs to know Fix silently did
            // nothing.
            let suppress_for_fix_cap = self
                .pending_fix_cap_preflight
                .as_ref()
                .is_some_and(|p| is_fix_cap_preflight_label(&result.label, &p.work_aid))
                && result.stderr.contains("max_review_iterations");
            // #935: a version-skewed `coord` that predates the `--json` diagnose
            // flag rejects the "Diagnose & fix stage…" dry-run
            // (`diagnose <repo> <issue> --dry-run --json`) with a Click usage
            // error (exit 2, "No such option '--json'", no stdout). That is an
            // EXPECTED skew, not a bug: we transparently retry without `--json`
            // and open the legacy options dialog below, so the generic red toast
            // would be misleading noise. Suppress it for this one case.
            let diagnose_json_rejected = result.exit_code != 0
                && result.label.contains("diagnose ")
                && result.label.contains("--json")
                && diagnose_json_flag_unsupported(&result.stderr);
            // #935: completion of the no-`--json` retry we dispatched for the
            // case above. Whether it succeeded or failed, its result is surfaced
            // as a dialog (legacy parse, or a minimal degraded dialog) rather
            // than a toast — so suppress the generic failure toast here too.
            let is_diagnose_legacy_retry = self
                .pending_diagnose_legacy_retry
                .as_ref()
                .is_some_and(|(repo, issue)| {
                    result.label.contains("diagnose ")
                        && !result.label.contains("--json")
                        && parse_diagnose_label_repo_issue(&result.label)
                            == Some((repo.clone(), *issue))
                });
            // #1059: a failed "Dispatch Gate A mock" (`coord acceptance mock`)
            // is surfaced as a full-text modal dialog (below) rather than the
            // single-line command-failed toast — a long reason like "no
            // acceptance driver configured — add it under acceptance.drivers…"
            // is unreadable when truncated to ~40 cols, and the operator read
            // that truncation as a permanent wedge (#1041). Suppress the
            // generic toast for this one case so we don't double-notify.
            let gate_a_dispatch_failed =
                result.exit_code != 0 && is_gate_a_mock_dispatch_label(&result.label);
            // #2863: a failed `coord portal decompose-chat` gets the same
            // treatment for the same reason — its commonest failure is the
            // multi-sentence thin-client refusal, which names its own fix
            // and is unreadable clipped to ~40 cols. Unlike Gate A this ALSO
            // has to undo the optimistic "chat ready — type to start" toast
            // that contract §4d requires firing at dispatch time, before the
            // exit code exists; `fail_pending_decomposition_chat` does all
            // three (retract, disarm the 30 s bind timeout, raise the modal)
            // so the operator is left with exactly one error surface instead
            // of three contradictory toasts.
            let decompose_chat_dispatch_failed =
                result.exit_code != 0 && is_decompose_chat_dispatch_label(&result.label);
            if result.exit_code != 0
                && !should_suppress_command_failed_toast(
                    &result.label,
                    self.pending_artifact_pull.as_ref(),
                )
                && !suppress_for_fix_cap
                && !diagnose_json_rejected
                && !is_diagnose_legacy_retry
                && !gate_a_dispatch_failed
                && !decompose_chat_dispatch_failed
            {
                let reason = first_meaningful_stderr_line(&result.stderr)
                    .unwrap_or_else(|| format!("exit {} — no stderr captured", result.exit_code));
                // #771 review: the TUI toast body is a single un-wrapped row
                // (quadraui's `tui/toast.rs` paints title + body as one line
                // each, ~40 cols wide, no wrapping — the embedded `\n` this
                // used to join `label`/`reason` with was never a real line
                // break). A long label (e.g. "coord milestone dispatch
                // claude-coordinator 767") ate the whole row and silently
                // swallowed the actual failure reason — exactly the
                // "unreadable toast" the operator hit. Put the actionable
                // `reason` FIRST so it survives truncation; the full label
                // is already visible in the status bar for several seconds
                // after completion (`command_runner.message`, set below),
                // so demoting it here loses nothing.
                self.push_toast(
                    "Command failed",
                    &format!("{}  ({})", reason, result.label),
                    ToastSeverity::Error,
                );
            }
            // #1059: raise the full-text Gate A dispatch-failure modal so the
            // operator can read the COMPLETE reason (word-wrapped), not a
            // ~40-col truncation. This is the fix for the recurring "Dispatch
            // Gate A mock … reads as a permanent wedge" report — a clean
            // pre-dispatch failure (e.g. no acceptance driver configured for
            // the repo) now shows its full, actionable message and states
            // that nothing was claimed.
            if gate_a_dispatch_failed {
                let reason = first_meaningful_stderr_line(&result.stderr)
                    .unwrap_or_else(|| format!("exit {} — no stderr captured", result.exit_code));
                self.gate_a_error_dialog = Some(reason);
            }
            // #2863: collapse a failed decomposition dispatch down to one
            // coherent surface. The FULL stderr (not just its first
            // meaningful line, the way Gate A above takes it) goes into the
            // modal: the thin-client refusal's actionable half — "run it on
            // the daemon host" — is in its later sentences, so first-line
            // truncation would reproduce exactly the unreadability this is
            // fixing. Falls back to the first meaningful line, then to the
            // bare exit code, so an empty stderr still says something.
            if decompose_chat_dispatch_failed {
                let stderr = result.stderr.trim();
                let reason = if !stderr.is_empty() {
                    stderr.to_string()
                } else {
                    first_meaningful_stderr_line(&result.stdout).unwrap_or_else(|| {
                        format!("exit {} — no stderr captured", result.exit_code)
                    })
                };
                let submission_id = decompose_chat_label_submission_id(&result.label)
                    .unwrap_or_else(|| "this submission".to_string());
                self.fail_pending_decomposition_chat(&submission_id, &reason);
            }
            // #863: Fix-dispatch cap preflight completed.  A clean exit means
            // the cap doesn't block this dispatch (or `--force` already
            // overrode it) — proceed straight into the real human-attended
            // launch.  A `max_review_iterations` refusal raises the one-key
            // force-past-cap confirm instead of leaving the operator at a
            // dead end.  Any other failure already got the generic toast
            // above; nothing further to do.
            if let Some(p) = self.pending_fix_cap_preflight.clone() {
                if is_fix_cap_preflight_label(&result.label, &p.work_aid) {
                    self.pending_fix_cap_preflight = None;
                    if result.exit_code == 0 {
                        // This IS the follow-through from the preflight that
                        // just completed — pass `Some(FixPreflightTarget)` so
                        // `launch_interactive_session_on_machine_inner` (1)
                        // skips the cap-preflight gate (it just ran) and (2)
                        // resolves the repo/issue/work_aid from the PINNED
                        // target below rather than whatever is currently
                        // selected in the UI — otherwise an operator who
                        // navigates away while the preflight runs gets the
                        // launch silently misdirected to (or dropped for) the
                        // wrong issue (#863 review fix).
                        self.launch_interactive_session_on_machine_inner(
                            InteractiveLaunchMode::Fix,
                            p.machine,
                            None,
                            p.force,
                            Some(FixPreflightTarget {
                                coord_repo: p.coord_repo,
                                repo_slug: p.repo_slug,
                                issue_num: p.issue_num,
                                work_aid: p.work_aid,
                            }),
                        );
                    } else if result.stderr.contains("max_review_iterations") {
                        let max_iterations = parse_max_review_iterations(&result.stderr);
                        self.pending_fix_force_confirm = Some(PendingFixForceConfirm {
                            coord_repo: p.coord_repo,
                            repo_slug: p.repo_slug,
                            issue_num: p.issue_num,
                            machine: p.machine,
                            work_aid: p.work_aid,
                            max_iterations,
                        });
                    }
                }
            }
            // #935: The operator's `coord` CLI/daemon rejected the `--json`
            // diagnose flag (it predates #935). Transparently retry the SAME
            // dry-run WITHOUT `--json` — the retry succeeds against the old CLI
            // and emits the legacy `·`/`✓`/DIAGNOSE_RESULT: text we parse into
            // the options dialog on its completion (handled just below). This
            // keeps the per-stage doctor usable on a version-skewed fleet
            // instead of dead-ending at a "Command failed" toast (the exact
            // smoke-test failure this fixes).
            if diagnose_json_rejected {
                if let Some((repo, issue_number)) =
                    parse_diagnose_label_repo_issue(&result.label)
                {
                    let mut argv: Vec<String> =
                        vec!["diagnose".into(), repo.clone(), issue_number.to_string()];
                    if let Some(stage) = parse_diagnose_label_stage(&result.label) {
                        argv.push("--stage".into());
                        argv.push(stage);
                    }
                    argv.push("--dry-run".into());
                    let argv_refs: Vec<&str> = argv.iter().map(|s| s.as_str()).collect();
                    self.command_runner.spawn_queued(&argv_refs);
                    self.pending_diagnose_legacy_retry = Some((repo, issue_number));
                } else {
                    // argv shape unrecognized — can't retry, but still avoid a
                    // dead-end: tell the operator their CLI is too old for the
                    // per-stage doctor's JSON path.
                    self.push_toast(
                        "Diagnose",
                        "This coord CLI predates #935 (no --json). Upgrade the \
                         daemon/CLI to use 'Diagnose & fix stage'.",
                        ToastSeverity::Warning,
                    );
                }
            }
            // #935: completion of the no-`--json` retry dispatched above. Route
            // it into the legacy options dialog (best-effort text parse). If the
            // retry ALSO failed or produced nothing parseable (e.g. a CLI old
            // enough to also reject `--dry-run`), still open a minimal degraded
            // dialog so the operator gets Recover/Reset/Clear-phantom/Dismiss —
            // never a bare toast.
            else if is_diagnose_legacy_retry {
                if let Some((repo, issue_number)) =
                    self.pending_diagnose_legacy_retry.take()
                {
                    let opened = result.exit_code == 0
                        && self.open_legacy_diagnose_dialog(&result.label, &result.stdout);
                    if !opened {
                        self.open_minimal_legacy_diagnose_dialog(&repo, issue_number);
                    }
                    self.refresh();
                }
            }
            // Per-stage doctor: surface `coord diagnose`'s findings/actions.
            //
            // #935 Part B: when the label includes "--json" AND "--dry-run"
            // (the two-phase "Diagnose & fix stage…" dry-run pass), parse
            // the DIAGNOSE_JSON line and open an option dialog instead of a
            // plain toast.  For all other diagnose completions (the full
            // recover pass, the --reset pass, or old-style calls that don't
            // carry --json) keep the existing toast behaviour.
            else if result.label.contains("diagnose ") && result.exit_code == 0 {
                let out = result.stdout.trim();
                let is_dry_run_json = result.label.contains("--json")
                    && result.label.contains("--dry-run");
                if is_dry_run_json {
                    // Parse the DIAGNOSE_JSON line to populate the dialog.
                    let json_line = out
                        .lines()
                        .find(|l| l.starts_with("DIAGNOSE_JSON:"))
                        .and_then(|l| l.strip_prefix("DIAGNOSE_JSON:"));
                    if let Some(json_str) = json_line {
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(json_str) {
                            let repo = v.get("repo_name")
                                .and_then(|x| x.as_str())
                                .unwrap_or("")
                                .to_string();
                            let issue_number = v.get("issue_number")
                                .and_then(|x| x.as_u64())
                                .unwrap_or(0);
                            let stage = v.get("stage")
                                .and_then(|x| x.as_str())
                                .unwrap_or("work")
                                .to_string();
                            let findings: Vec<String> = v.get("findings")
                                .and_then(|x| x.as_array())
                                .map(|arr| {
                                    arr.iter()
                                        .filter_map(|e| e.as_str().map(|s| s.to_string()))
                                        .collect()
                                })
                                .unwrap_or_default();
                            let actions_taken: Vec<String> = v.get("actions_taken")
                                .and_then(|x| x.as_array())
                                .map(|arr| {
                                    arr.iter()
                                        .filter_map(|e| e.as_str().map(|s| s.to_string()))
                                        .collect()
                                })
                                .unwrap_or_default();
                            let needs_reset = v.get("needs_reset")
                                .and_then(|x| x.as_bool())
                                .unwrap_or(false);
                            // Check for a phantom pending- entry for this issue.
                            let has_phantom_session = self.live_tmux_sessions.iter().any(|s| {
                                s.assignment_id.starts_with("pending-")
                                    && s.issue_number == Some(issue_number)
                                    && s.repo_name.as_deref() == Some(&repo)
                            });
                            self.pending_diagnose_dialog = Some(PendingDiagnoseDialog {
                                repo,
                                issue_number,
                                stage,
                                findings,
                                actions_taken,
                                needs_reset,
                                has_phantom_session,
                                legacy: false,
                            });
                        } else {
                            // JSON parse failed — fall back to plain toast.
                            self.push_toast(
                                "Diagnose",
                                "Could not parse DIAGNOSE_JSON — see log for details.",
                                ToastSeverity::Warning,
                            );
                        }
                    } else {
                        // No DIAGNOSE_JSON line — the daemon predates #935's
                        // JSON support (or relayed a malformed response). The
                        // human-readable findings (`·`), actions (`✓`), and
                        // the `DIAGNOSE_RESULT:` trailer are ALWAYS emitted
                        // regardless of --json, so best-effort parse those
                        // into the SAME options dialog rather than leaving
                        // the operator with a bare toast and no way to act
                        // (the exact "blind fire-and-toast" UX #935 set out
                        // to eliminate).
                        if !self.open_legacy_diagnose_dialog(&result.label, out) {
                            // argv shape unrecognized — truly nothing to show.
                            self.push_toast(
                                "Diagnose",
                                "No DIAGNOSE_JSON line in output — daemon may be \
                                 pre-#935, and the fallback output could not be \
                                 parsed either.",
                                ToastSeverity::Warning,
                            );
                        }
                    }
                } else {
                    // Full recover / reset / legacy pass: existing toast behaviour.
                    let needs_reset = out
                        .lines()
                        .rev()
                        .find(|l| l.starts_with("DIAGNOSE_RESULT:"))
                        .map(|l| l.contains("needs_reset=true"))
                        .unwrap_or(false);
                    let is_reset = result.label.contains("--reset");
                    // Prefer the action (✓) lines; fall back to the findings (·).
                    let actions: Vec<&str> = out
                        .lines()
                        .filter(|l| l.trim_start().starts_with('✓'))
                        .collect();
                    let mut body = if !actions.is_empty() {
                        actions.join("\n")
                    } else {
                        out.lines()
                            .filter(|l| l.trim_start().starts_with('·'))
                            .take(6)
                            .collect::<Vec<_>>()
                            .join("\n")
                    };
                    if body.is_empty() {
                        body = "no changes needed".to_string();
                    }
                    let (title, sev) = if is_reset {
                        ("Stage reset (branch kept)", ToastSeverity::Info)
                    } else if needs_reset {
                        body.push_str(
                            "\n\nUse 'Reset stage (keeps branch + commits)' to clear it.",
                        );
                        ("Diagnose — still wedged", ToastSeverity::Warning)
                    } else {
                        ("Diagnose & fix stage", ToastSeverity::Info)
                    };
                    self.push_toast(title, &body, sev);
                }
                self.refresh();
            }
            // #264: chain the queued `coord ready` after `coord stop`
            // finished cleanly at the end of a refinement chat.  We only
            // fire it on successful stop — leaving the issue in
            // status:refining when the worker couldn't be killed is the
            // safer default (the user can drop to backlog manually).
            if let Some(p) = self.pending_refine_ready.clone() {
                if result.label.contains(&format!("stop {}", p.assignment_id))
                    && result.exit_code == 0
                {
                    self.pending_refine_ready = None;
                    let issue_str = p.issue_number.to_string();
                    // Use spawn_queued so `ready` is enqueued if another command
                    // was queued while this stop was running.  ready_started is
                    // true for both Started and Queued — it will run eventually.
                    use crate::commands::SpawnQueuedOutcome;
                    let ready_outcome = self
                        .command_runner
                        .spawn_queued(&["ready", &p.repo, &issue_str]);
                    let ready_started = matches!(
                        ready_outcome,
                        SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
                    );
                    if !ready_started {
                        self.push_toast(
                            "Refine with chat",
                            "ready already queued or running — skipping duplicate.",
                            ToastSeverity::Info,
                        );
                    }
                    // #410: Send path — queue coord assign to run after coord ready.
                    // Gate on `ready_started`: if coord ready never launched we must
                    // not dispatch the issue (it would skip the status:ready flip).
                    if p.then_dispatch && ready_started {
                        if let Some(machine) = self.best_machine_for(&p.repo).cloned() {
                            let machine_name = machine.name.clone();
                            let model_str = self
                                .settings
                                .machine_model
                                .get(&machine_name)
                                .map(|mp| mp.as_str().to_string());
                            let issue_str2 = p.issue_number.to_string();
                            let mut cmd: Vec<String> = vec![
                                "assign".into(),
                                machine_name.clone(),
                                p.repo.clone(),
                                issue_str2,
                            ];
                            if let Some(ref m) = model_str {
                                cmd.push("--model".into());
                                cmd.push(m.clone());
                            }
                            let cmd_refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
                            use crate::commands::SpawnQueuedOutcome;
                            match self.command_runner.spawn_queued(&cmd_refs) {
                                SpawnQueuedOutcome::Started => {
                                    self.push_toast(
                                        "Send",
                                        &format!(
                                            "#{} dispatched → {}",
                                            p.issue_number, machine_name
                                        ),
                                        ToastSeverity::Info,
                                    );
                                }
                                SpawnQueuedOutcome::Queued => {
                                    self.push_toast(
                                        "Send",
                                        &format!(
                                            "#{}: dispatch queued → {}",
                                            p.issue_number, machine_name
                                        ),
                                        ToastSeverity::Info,
                                    );
                                }
                                SpawnQueuedOutcome::Deduped => {}
                            }
                            self.maybe_kick_pipeline_loader();
                        } else {
                            self.push_toast(
                                "Send",
                                &format!(
                                    "#{}: ready — but no reachable machine for {}.",
                                    p.issue_number, p.repo
                                ),
                                ToastSeverity::Warning,
                            );
                        }
                    }
                }
            }
            // #336 / #434 / #532: pull-artifact completion — open an info
            // dialog showing the full destination path (success) or the
            // error message (failure).  Replaces the 4-second toast with a
            // re-openable, copyable dialog; the durable panel line remains.
            if let Some((work_id, repo, sanitized)) = &self.pending_artifact_pull.clone() {
                if result.label.contains(&format!("pull-artifact {}", work_id)) {
                    self.pending_artifact_pull = None;
                    let pull_message: String;
                    if result.exit_code == 0 {
                        let home = std::env::var("HOME").unwrap_or_else(|_| "~".to_string());
                        let path = format!("{home}/.coord/artifacts/{repo}/{sanitized}/");
                        pull_message = path.clone();
                        // #532: open dialog instead of ephemeral toast.
                        self.artifact_pull_dialog = Some(ArtifactPullDialog {
                            path: Some(path.clone()),
                            body: format!("Saved to:\n{}", path),
                        });
                    } else {
                        // Capture the error text for the dialog and the
                        // durable panel line.
                        pull_message =
                            first_meaningful_stderr_line(&result.stderr).unwrap_or_else(|| {
                                format!("exit {} — no stderr captured", result.exit_code)
                            });
                        // #532: open dialog showing the failure reason.
                        self.artifact_pull_dialog = Some(ArtifactPullDialog {
                            path: None,
                            body: format!(
                                "Pull failed (exit {}):\n{}",
                                result.exit_code, pull_message
                            ),
                        });
                    }
                    // #434: persist the result so it remains visible after
                    // the dialog dismisses (drives the "Last pull: …" panel
                    // line and lets `a` re-open the dialog).
                    self.last_artifact_pulls.insert(
                        work_id.clone(),
                        ArtifactPullResult {
                            exit_code: result.exit_code,
                            message: pull_message,
                            finished_at: Instant::now(),
                        },
                    );
                }
            }
            // #290 recovery: a coord command just finished.  If this was a
            // merge dispatch and no merge_queue row appeared yet (CI gate
            // blocked, queue empty, or transient read error), the optimistic
            // inflight flag would otherwise stay set forever — permanently
            // showing the Merge box as blue with no way to retry.  Clear any
            // inflight entry that still has no matching row in the current
            // data so the Go button returns and the user can retry.  Entries
            // whose DB rows already arrived are handled (and cleared earlier)
            // by apply_pending_data; this only fires for the stuck case.
            self.pipeline_inflight_merges.retain(|(_, issue_number)| {
                self.data
                    .merge_queue
                    .iter()
                    .any(|m| m.issue_number == Some(*issue_number))
            });
            // A coord command just finished — most mutate labels (track,
            // refine, ready, backlog, bounce) or assignment state, all written
            // to the DB cache.  refresh() reloads `data` from the DB, which
            // rebuilds the Pipeline from the cache in apply_pending_data, so the
            // user sees the result on the next tick (no gh search).
            self.refresh();
            needs_redraw = true;
        }

        // #264: bind a pending refinement-chat dispatch to its new
        // assignment row as soon as it appears in the DB.
        if self.pending_refinement.is_some() && self.maybe_bind_pending_refinement() {
            needs_redraw = true;
        }

        // #316: bind a pending board-chat dispatch (new-issue or refine-board)
        // to its assignment row when it appears in the DB.
        if self.pending_board_chat.is_some() && self.maybe_bind_pending_board_chat() {
            needs_redraw = true;
        }

        // #1017: bind a pending milestone-chat dispatch (New/Open/Add-sub-issue
        // via chat) to its `type="milestone-chat"` assignment row when it
        // appears in the DB, so the operator gets a live, attachable chat pane.
        if self.pending_milestone_chat.is_some() && self.maybe_bind_pending_milestone_chat() {
            needs_redraw = true;
        }

        // #2533: bind a pending decomposition-chat dispatch ("Pull into
        // decomposition session" on an Approved-work-items row) to its
        // `type="decomposition-chat"` assignment row when it appears in the
        // DB, same poll+bind lifecycle as the two chat families above.
        if self.pending_decomposition_chat.is_some()
            && self.maybe_bind_pending_decomposition_chat()
        {
            needs_redraw = true;
        }

        // #314 Phase B: bind a pending test-chat dispatch to its new assignment row.
        if self.pending_test_chat.is_some() && self.maybe_bind_pending_test_chat() {
            needs_redraw = true;
        }

        // #315: rebind the open chat overlay to the new assignment when a
        // `coord chat-continue` re-dispatch has landed in the DB.
        if self.pending_chat_resume.is_some() && self.maybe_bind_pending_resume() {
            needs_redraw = true;
        }

        // #319 Phase A: capture the chat's reply to the refinement-notes
        // synth prompt and open the review modal when it's complete.
        if self.pending_refinement_notes_synth.is_some() && self.poll_refinement_notes_synth() {
            needs_redraw = true;
        }
        // #319 Phase A: drain the `gh issue comment` shell-out result.
        if self.refinement_notes_post_rx.is_some() && self.poll_refinement_notes_post() {
            needs_redraw = true;
        }
        // #316 Phase B: drain the `gh issue create` shell-out result.
        if self.file_issue_post_rx.is_some() && self.poll_file_issue_post() {
            needs_redraw = true;
        }
        // #486 Leg 4: drain the background local+remote session fetch.
        if self.pending_remote_sessions.is_some() && self.poll_remote_sessions() {
            needs_redraw = true;
        }
        // #953: drain the background local+remote fleet-terminal fetch.
        if self.pending_remote_terminals.is_some() && self.poll_remote_terminals() {
            needs_redraw = true;
        }
        // #1398: drain the background drive-session discovery sweep.
        if self.pending_drive_sessions.is_some() && self.poll_drive_sessions() {
            needs_redraw = true;
        }
        // #1563: drain the background paused-machine sweep (local file or
        // daemon `GET /pause` on a thin client — see
        // `spawn_paused_machines_fetch`).
        if self.pending_paused_machines.is_some() && self.poll_paused_machines() {
            needs_redraw = true;
        }

        // #603: the fix-briefing preview arrived → repaint the confirm dialog.
        if self.fix_briefing_rx.is_some() && self.poll_fix_briefing_preview() {
            needs_redraw = true;
        }

        // #315: drain InjectFallback signals from `spawn_inject_post`.
        // These fire when /inject/{id} returned 409/410 — the worker
        // exited mid-flight after submit_inject's gate.  Transparently
        // trigger chat-continue so the typed message isn't lost.
        loop {
            let fb = match self.inject_fallback_rx.try_recv() {
                Ok(fb) => fb,
                Err(_) => break,
            };
            // Suppress if a resume is already in flight for this issue.
            if self.pending_chat_resume.is_some() {
                continue;
            }
            let arm_unix_secs = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            let config_path = self.command_runner.config_path.clone();
            // Look up the old assignment's type so the resume bind matches
            // the same chat type (fixes #361: test-chat / new-issue-chat
            // continuations never rebound when filter was "refinement"-only).
            let old_type = self
                .data
                .assignments
                .iter()
                .find(|a| a.id == fb.aid)
                .and_then(|a| a.assignment_type.clone());
            spawn_chat_continue(config_path, fb.aid.clone(), fb.text.clone());
            self.pending_chat_resume = Some(PendingChatResume {
                old_assignment_id: fb.aid.clone(),
                issue_number: fb.issue_number,
                dispatched_at: Instant::now(),
                arm_unix_secs,
                old_type,
            });
            self.pipeline_status = Some((
                format!("⏳ Resuming chat #{}…", fb.issue_number),
                Instant::now(),
            ));
            needs_redraw = true;
        }

        // Auto-notify: run `coord notify` when running assignments exist.
        // #584: skip on a thin client — `coord notify` is a host-side control
        // command; running it locally here only errors (no DB/config).
        let has_running = self.data.assignments.iter().any(|a| a.status == "running");
        if has_running
            && !is_remote_board_service()
            && self.last_notify.elapsed() >= NOTIFY_EVERY
            && !self.command_runner.is_running()
        {
            self.command_runner.spawn(&["notify"]);
            self.last_notify = Instant::now();
        }

        // Drain all SSE watch channels in the pool (including background
        // sessions not currently focused).  Draining always runs so data
        // accumulates into `sse.lines` (turn-count increments, Log tab
        // content).  A redraw is only requested when the drained content is
        // actually visible: Pipeline / Board / Kanban / MergeQueue views
        // show turn-count badges and/or the Log tab; Settings / Terminal /
        // Machines show none of that data.  When the watch overlay is open
        // (`watch_focused.is_some()`) a repaint is always needed regardless
        // of the underlying view.  On a view switch the normal event handler
        // sets `needs_redraw = true`, so accumulated-but-unseen SSE data
        // paints immediately when the user navigates back.
        if !self.watch_pool.is_empty() {
            let got_new = self.drain_watch_pool();
            if got_new {
                let watch_data_visible = self.watch_focused.is_some()
                    || !matches!(
                        self.active_view,
                        SidebarView::Settings | SidebarView::Terminal | SidebarView::Machines
                    );
                needs_redraw |= watch_data_visible;
            }
        }

        // Advance inject chat spinner when the overlay is open.
        // NOTE: previously this set `needs_redraw = true` unconditionally,
        // forcing a 60 fps repaint whenever the chat was open even though
        // nothing in the chat ever set `chat.set_busy(true)` — so the
        // spinner is invisible and the redraw was pure waste.  That waste
        // was unnoticed when the chat was used only for short worker-
        // guidance bursts; the #264 refinement chat keeps the overlay open
        // for minutes and surfaced it as ~50 % CPU.  Until any caller
        // actually animates a visible spinner here, just advance the frame
        // counter without forcing a repaint — repaints are driven by real
        // content changes (transcript / SSE drain) below.
        if let Some(ref mut chat) = self.inject_chat {
            self.inject_spinner_frame = self.inject_spinner_frame.wrapping_add(1);
            chat.set_spinner_frame(self.inject_spinner_frame);
        }

        // #235: Drain Phase 1 build completions and toast the outcome.
        // Cheap no-op when no jobs are in flight.
        needs_redraw |= self.poll_test_build_jobs();

        // #349: Drain completed test-plan step jobs.
        if self.poll_test_step_jobs() {
            needs_redraw = true;
        }
        // #349: Maybe spawn `coord test-plan` if the test stage is focused
        // and no plan is cached yet (or the plan is stale).
        self.maybe_spawn_test_plan();

        // #271 part 2 follow-up: drain completed `gh pr view` fetches
        // into the cache so the Test guidance block picks them up.
        if self.poll_pending_pr_fetches() {
            needs_redraw = true;
        }

        // #876: poll_pending_comments_fetches removed — Summary tab now
        // sources from in-memory board assignments.

        // #336: Poll the in-flight artifact manifest fetch and populate cache.
        if let Some((key, rx)) = &self.artifact_fetch_rx {
            match rx.try_recv() {
                Ok(outcome) => {
                    let (manifest, absence_reason) = match outcome {
                        ArtifactFetchOutcome::Found(m) => (Some(m), None),
                        ArtifactFetchOutcome::NotStashed => {
                            (None, Some(ArtifactAbsence::NotStashed))
                        }
                        ArtifactFetchOutcome::Empty => {
                            (None, Some(ArtifactAbsence::ManifestEmpty))
                        }
                        ArtifactFetchOutcome::Unreachable(e) => {
                            (None, Some(ArtifactAbsence::AgentUnreachable(e)))
                        }
                    };
                    let entry = ArtifactCacheEntry {
                        fetched_at: Instant::now(),
                        manifest,
                        absence_reason,
                    };
                    let key_clone = key.clone();
                    self.artifact_cache.insert(key_clone, entry);
                    self.artifact_fetch_rx = None;
                    needs_redraw = true;
                }
                Err(std::sync::mpsc::TryRecvError::Empty) => {}
                Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    self.artifact_fetch_rx = None;
                }
            }
        }
        // #336: Trigger a fresh manifest fetch when the pipeline Test-stage
        // context is visible and the cache entry is absent or older than 30 s.
        if self.artifact_fetch_rx.is_none() && self.active_view == SidebarView::Pipeline {
            if let Some((host, repo, sanitized, _work_id)) = self.artifact_fetch_target() {
                let key = (repo.clone(), sanitized.clone());
                let needs_fetch = match self.artifact_cache.get(&key) {
                    None => true,
                    Some(e) => e.fetched_at.elapsed() > Duration::from_secs(30),
                };
                if needs_fetch {
                    let rx = spawn_artifact_fetch(&host, &repo, &sanitized);
                    self.artifact_fetch_rx = Some((key, rx));
                }
            }
        }

        // #1337: Poll the in-flight full-findings detail fetch into the cache
        // (the /board wire carries only a bounded review_findings preview;
        // the Review pane hydrates the full body from GET /assignment/{id}).
        if let Some((key, rx)) = &self.findings_fetch_rx {
            match rx.try_recv() {
                Ok(full) => {
                    let got_body = full.is_some();
                    let entry = FindingsDetailEntry {
                        fetched_at: Instant::now(),
                        full,
                    };
                    let key_clone = key.clone();
                    self.findings_detail_cache.insert(key_clone, entry);
                    self.findings_fetch_rx = None;
                    if got_body {
                        needs_redraw = true;
                    }
                }
                Err(std::sync::mpsc::TryRecvError::Empty) => {}
                Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    self.findings_fetch_rx = None;
                }
            }
        }
        // #1337: Arm a fetch when the Pipeline view shows a review whose
        // findings were wire-truncated and not yet hydrated (the target
        // helper handles cache hits + failure back-off).
        if self.findings_fetch_rx.is_none() && self.active_view == SidebarView::Pipeline {
            if let Some(key) = self.findings_fetch_target() {
                if let Some((url, token)) = resolve_board_service() {
                    let rx = spawn_findings_detail_fetch(url, token, key.0.clone());
                    self.findings_fetch_rx = Some((key, rx));
                }
            }
        }

        // #2497/#1939: Poll the in-flight full-issue-body detail fetch into
        // the cache (the /board wire drops a closed issue's body to 0 chars,
        // and an open non-epic issue's body to its machine-parsed residue;
        // the Board/Pipeline Issue tab and the Drive/Merge Queue tab's
        // issue-detail pane all hydrate the full body from
        // GET /issue/{repo}/{number}).
        if let Some((key, rx)) = &self.issue_fetch_rx {
            match rx.try_recv() {
                Ok(full) => {
                    let got_body = full.is_some();
                    let entry = IssueDetailEntry {
                        fetched_at: Instant::now(),
                        full,
                    };
                    let key_clone = key.clone();
                    self.issue_detail_cache.insert(key_clone, entry);
                    self.issue_fetch_rx = None;
                    if got_body {
                        needs_redraw = true;
                    }
                }
                Err(std::sync::mpsc::TryRecvError::Empty) => {}
                Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    self.issue_fetch_rx = None;
                }
            }
        }
        // #2497/#1939: Arm a fetch when the Board Issue tab, the Pipeline
        // Issue tab, or the Drive/Merge Queue tab's issue-detail pane shows
        // an issue whose body was wire-truncated and not yet hydrated (the
        // target helper handles tab/view gating + cache hits + failure
        // back-off).
        if self.issue_fetch_rx.is_none() {
            if let Some(key) = self.issue_body_fetch_target() {
                if let Some((url, token)) = resolve_board_service() {
                    let rx = spawn_issue_detail_fetch(url, token, key.0.clone(), key.1);
                    self.issue_fetch_rx = Some((key, rx));
                }
            }
        }

        // #207: Machine metrics polling — only when the Machines panel is
        // visible so we don't burn background threads when the user is on
        // another view.
        if self.active_view == SidebarView::Machines {
            // Drain any completed in-flight metrics fetches first.
            let mut drained: Vec<PendingMetrics> = Vec::new();
            for pm in self.pending_metrics.drain(..) {
                match pm.rx.try_recv() {
                    Ok(Ok(sample)) => {
                        let buf = self.machine_metrics
                            .entry(pm.machine)
                            .or_default();
                        buf.push_back(sample);
                        if buf.len() > METRICS_HISTORY {
                            buf.pop_front();
                        }
                        needs_redraw = true;
                    }
                    Ok(Err(_)) => {}  // fetch failed — silently skip
                    Err(std::sync::mpsc::TryRecvError::Empty) => {
                        // Still in flight — put it back.
                        drained.push(pm);
                    }
                    Err(std::sync::mpsc::TryRecvError::Disconnected) => {}
                }
            }
            self.pending_metrics = drained;

            // Kick a new round when the cadence has elapsed and no fetches
            // are outstanding (avoids piling up requests when the agent is slow).
            if self.pending_metrics.is_empty()
                && self.metrics_last_polled.elapsed() >= METRICS_CADENCE
            {
                self.metrics_last_polled = Instant::now();
                for m in &self.data.machines {
                    if m.reachable && !m.host.is_empty() {
                        let pm = spawn_machine_metrics(&m.host, 7433, m.name.clone());
                        self.pending_metrics.push(pm);
                    }
                }
            }
        }

        // #1039: Audit panel fetch — armed only while the panel is visible
        // (mirrors the artifact-fetch Pipeline-only gating above), so no
        // background thread runs while the operator is elsewhere.
        if self.active_view == SidebarView::Audit {
            if let Some(rx) = &self.audit_fetch_rx {
                match rx.try_recv() {
                    Ok(AuditFetchOutcome::Page(page)) => {
                        self.audit_page = Some(page);
                        self.audit_fetch_error = None;
                        self.audit_no_service = false;
                        self.audit_last_fetched = Some(Instant::now());
                        self.audit_fetch_rx = None;
                        needs_redraw = true;
                    }
                    Ok(AuditFetchOutcome::NoBoardService) => {
                        // Not an error — just nothing configured to fetch
                        // from (local-SQLite-mode). Leave any previously
                        // cached page as-is; stop retrying until the TTL
                        // elapses again.
                        //
                        // #1039 review fix: this used to be a silent no-op
                        // that left the empty state indistinguishable from
                        // "fetch not yet completed" or "genuinely empty
                        // board" — a live-daemon smoke test that hit this
                        // path (`resolve_board_service()` unexpectedly
                        // returning `None`, e.g. a stale/unreadable
                        // `~/.coord/client.toml`) had no way to tell that
                        // apart from a real empty page short of attaching a
                        // debugger. `audit_no_service` surfaces it directly
                        // in the panel (see `audit.rs`). Also needed
                        // `needs_redraw = true` (missing before, unlike the
                        // other two arms) so the hint actually paints on
                        // the next frame instead of waiting for an
                        // unrelated redraw to happen to fire.
                        self.audit_fetch_error = None;
                        self.audit_no_service = true;
                        self.audit_last_fetched = Some(Instant::now());
                        self.audit_fetch_rx = None;
                        needs_redraw = true;
                    }
                    Ok(AuditFetchOutcome::Unreachable(reason)) => {
                        // Genuine failure — surfaced via `audit_fetch_error`
                        // in the empty-state message (`audit.rs`). Leave any
                        // previously cached page as-is; stop retrying until
                        // the TTL elapses again so a down daemon doesn't
                        // spin threads.
                        self.audit_fetch_error = Some(reason);
                        self.audit_no_service = false;
                        self.audit_last_fetched = Some(Instant::now());
                        self.audit_fetch_rx = None;
                        needs_redraw = true;
                    }
                    Err(std::sync::mpsc::TryRecvError::Empty) => {
                        // Still in flight.
                    }
                    Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                        self.audit_fetch_rx = None;
                    }
                }
            }
            // Kick a fresh fetch on first visit, after `r` (which clears
            // `audit_last_fetched`), or once the TTL has elapsed.
            if self.audit_fetch_rx.is_none() {
                let needs_fetch = match self.audit_last_fetched {
                    None => true,
                    Some(t) => t.elapsed() >= AUDIT_FETCH_TTL,
                };
                if needs_fetch {
                    // #1040: build the request from the panel's current
                    // filter selection (contract §8/§9/§11) — `since` is
                    // computed fresh on every fetch (not cached) so a
                    // `LastHour`/`Today`/`D7` window keeps sliding forward
                    // across the 15s refresh cadence, exactly like it would
                    // on a fresh `t` cycle.
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs_f64();
                    let since = self.audit_time_range.since(now);
                    let category = self.audit_category.query_value();
                    let type_filter = self.audit_type_filter.query.trim();
                    let type_filter =
                        if type_filter.is_empty() { None } else { Some(type_filter) };
                    // #2653: tier defaults to `Business`, so most fetches DO
                    // send a `tier=business` param now — that's the whole
                    // point (see AuditTier's doc comment).
                    let tier = self.audit_tier.query_value();
                    self.audit_fetch_rx =
                        Some(spawn_audit_fetch(since, category, type_filter, tier));
                }
            }
        }

        // #1741: Reports panel fetches — same view-gating as the Audit block
        // above, for the same reason: this is the class of bug that had the
        // audit panel polling forever from other views. Both the catalogue
        // fetch and the in-flight run drain are inside the gate, so
        // navigating away stops the panel doing any background work at all.
        //
        // Unlike `/audit` there is no TTL: the catalogue is static metadata
        // (fetched once per session, re-armed by `r` via
        // `reports_refresh_catalogue`), and a run only happens when the
        // operator asks for one.
        if self.active_view == SidebarView::Reports {
            if let Some(rx) = &self.reports_catalogue_rx {
                match rx.try_recv() {
                    Ok(ReportsCatalogueOutcome::Catalogue(reports)) => {
                        self.reports_catalogue = Some(reports);
                        self.reports_error = None;
                        self.reports_no_service = false;
                        self.reports_catalogue_fetched = true;
                        self.reports_catalogue_rx = None;
                        self.reports_sel = 0;
                        self.reports_field_sel = 0;
                        // Open every untouched section so the first frame
                        // shows usable parameter forms, not a stack of
                        // collapsed titles.
                        self.reports_seed_expansion();
                        needs_redraw = true;
                    }
                    Ok(ReportsCatalogueOutcome::NoBoardService) => {
                        self.reports_error = None;
                        self.reports_no_service = true;
                        self.reports_catalogue_fetched = true;
                        self.reports_catalogue_rx = None;
                        needs_redraw = true;
                    }
                    Ok(ReportsCatalogueOutcome::Unreachable(reason)) => {
                        self.reports_error = Some(reason);
                        self.reports_no_service = false;
                        self.reports_catalogue_fetched = true;
                        self.reports_catalogue_rx = None;
                        needs_redraw = true;
                    }
                    Err(std::sync::mpsc::TryRecvError::Empty) => {
                        // Still in flight.
                    }
                    Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                        self.reports_catalogue_rx = None;
                        self.reports_catalogue_fetched = true;
                    }
                }
            } else if !self.reports_catalogue_fetched {
                self.reports_catalogue_rx = Some(spawn_reports_catalogue_fetch());
            }

            if let Some(rx) = &self.reports_run_rx {
                match rx.try_recv() {
                    Ok(ReportRunOutcome::Result(result)) => {
                        self.reports_result = Some(*result);
                        self.reports_error = None;
                        self.reports_no_service = false;
                        self.reports_running = None;
                        self.reports_run_rx = None;
                        self.reports_result_scroll = 0;
                        needs_redraw = true;
                    }
                    Ok(ReportRunOutcome::NoBoardService) => {
                        // Surfaced as an error here (unlike the catalogue
                        // case): the operator explicitly asked for a run, so
                        // silence would read as a hung request.
                        self.reports_error = Some("no board service configured".to_string());
                        self.reports_no_service = true;
                        self.reports_running = None;
                        self.reports_run_rx = None;
                        needs_redraw = true;
                    }
                    Ok(ReportRunOutcome::Unreachable(reason)) => {
                        self.reports_error = Some(reason);
                        self.reports_running = None;
                        self.reports_run_rx = None;
                        needs_redraw = true;
                    }
                    Err(std::sync::mpsc::TryRecvError::Empty) => {
                        // Still in flight — the section badge shows "running…".
                    }
                    Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                        self.reports_run_rx = None;
                        self.reports_running = None;
                    }
                }
            }

            // #1765: the CSV export. Every terminal state writes a visible
            // message into the notes area — a silent write is
            // indistinguishable from a silent failure, and only the message
            // carries the destination.
            if let Some(rx) = &self.reports_export_rx {
                match rx.try_recv() {
                    Ok(ReportExportOutcome::Written { path, bytes }) => {
                        // Name the row order explicitly: the export is the
                        // report's canonical order, NOT the panel's
                        // client-side sort (#1762), and that difference is
                        // better said than discovered in a spreadsheet.
                        self.reports_export_status = Some(format!(
                            "Exported {bytes} bytes → {} (report order, not the on-screen sort)",
                            path.display()
                        ));
                        self.reports_export_rx = None;
                        needs_redraw = true;
                    }
                    Ok(ReportExportOutcome::NoBoardService) => {
                        self.reports_export_status =
                            Some("Export failed: no board service configured.".to_string());
                        self.reports_export_rx = None;
                        needs_redraw = true;
                    }
                    Ok(ReportExportOutcome::Failed(reason)) => {
                        self.reports_export_status = Some(format!("Export failed: {reason}"));
                        self.reports_export_rx = None;
                        needs_redraw = true;
                    }
                    Err(std::sync::mpsc::TryRecvError::Empty) => {
                        // Still in flight — the notes area shows "Exporting →".
                    }
                    Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                        // The worker died without answering. Say so rather
                        // than leaving "Exporting…" on screen forever.
                        self.reports_export_status =
                            Some("Export failed: the export worker stopped.".to_string());
                        self.reports_export_rx = None;
                        needs_redraw = true;
                    }
                }
            }
        }

        needs_redraw
    }

    /// Drain pending messages from every SSE stream in `watch_pool`,
    /// accumulate lines, and handle per-entry reconnect/error logic.
    /// Returns `true` when new data arrived on any stream (redraw needed).
    ///
    /// Reconnect strategy (per entry): on transient errors, reopen the stream
    /// using `Last-Event-Id` so replay starts from the last received byte
    /// offset.  After **3 failures within 10 seconds** a toast is shown
    /// (keyed to the affected issue) and the stream latches `done` —
    /// pressing `R` while watching that issue reconnects immediately, but
    /// per #1954 that isn't the only way back: once
    /// [`SSE_RETRY_BACKOFF`] has passed since the first failure in the
    /// losing window, the *next* drain tick clears the failure state and
    /// retries on its own (see the `sse.done` branch below). A worker that
    /// hiccups during dispatch (e.g. `/stream` 404 before
    /// `assignment.log_path` is set) routinely recovers seconds later —
    /// there is no good reason for a 4-hour run to stay dark for the rest
    /// of the assignment just because three fast retries lost a race at
    /// the start. Background streams (not currently focused) show the
    /// toast with the issue number so the user can switch to them to
    /// diagnose the failure, but no longer *need* to.
    ///
    /// Maximum pool size is `WATCH_POOL_CAP` (currently 8 concurrent streams).
    pub(crate) fn drain_watch_pool(&mut self) -> bool {
        let ids: Vec<String> = self.watch_pool.keys().cloned().collect();
        let mut got_new = false;
        for id in ids {
            got_new |= self.drain_pool_entry(&id);
        }
        got_new
    }

    /// Drain one pool entry.  Returns `true` when new data arrived.
    pub(crate) fn drain_pool_entry(&mut self, id: &str) -> bool {
        let mut got_new = false;
        let mut needs_reconnect = false;
        let mut fail_limit_hit = false;
        let mut not_found = false;

        // First pass: drain all pending messages while we hold a mutable ref.
        if let Some(ctx) = self.watch_pool.get_mut(id) {
            let sse = &mut ctx.sse;
            if sse.done {
                // #1954: distinguish "stream ended cleanly" (fail_count is
                // whatever it was before the clean `Done`, never bumped past
                // the fail-limit by the `Done` path itself) from "latched
                // dead by the 3-failures/10s guard" (fail_count >= 3, the
                // only path that sets both `done` and `fail_count >= 3`
                // together). Only the latter is eligible for automatic
                // retry — a cleanly-finished assignment's log is not coming
                // back, and reopening it would just replay stale content
                // forever. `first_fail_at` doubles as the backoff clock:
                // it's set to the start of the losing window and untouched
                // once latched, so its age is exactly how long the stream
                // has been dark.
                let eligible_for_retry = sse.fail_count >= 3
                    && sse
                        .first_fail_at
                        .is_some_and(|t| t.elapsed() >= SSE_RETRY_BACKOFF);
                if !eligible_for_retry {
                    return false;
                }
                sse.done = false;
                sse.fail_count = 0;
                sse.first_fail_at = None;
                needs_reconnect = true;
                got_new = true;
            } else {
                loop {
                    use std::sync::mpsc::TryRecvError;
                    let msg = match sse.rx.try_recv() {
                        Ok(m) => m,
                        Err(TryRecvError::Empty) => break,
                        Err(TryRecvError::Disconnected) => {
                            // Background thread exited without sending; treat as error.
                            sse.fail_count += 1;
                            match sse.first_fail_at {
                                None => sse.first_fail_at = Some(Instant::now()),
                                Some(t) if t.elapsed() > Duration::from_secs(10) => {
                                    sse.first_fail_at = Some(Instant::now());
                                    sse.fail_count = 1;
                                }
                                _ => {}
                            }
                            if sse.fail_count >= 3 {
                                sse.done = true;
                                fail_limit_hit = true;
                            } else {
                                needs_reconnect = true;
                            }
                            got_new = true;
                            break;
                        }
                    };

                    match msg {
                        SseWatchMsg::Lines { last_id, text } => {
                            sse.last_event_id = last_id;
                            // Reassemble lines split across SSE chunks. The agent
                            // emits whatever it read from the log file (up to
                            // LOG_CHUNK_SIZE=4096 bytes), so a JSON line longer
                            // than that arrives in pieces. If the chunk doesn't
                            // end with `\n`, hold the trailing partial line until
                            // the next chunk completes it. Without this, broken
                            // half-lines reach parse_json_event and we lose
                            // fields like total_cost_usd / stop_reason that come
                            // after the split point.
                            let mut payload = std::mem::take(&mut sse.pending_tail);
                            payload.push_str(&text);
                            let (complete, tail) = if payload.ends_with('\n') {
                                (payload.clone(), String::new())
                            } else if let Some(last_nl) = payload.rfind('\n') {
                                let (a, b) = payload.split_at(last_nl + 1);
                                (a.to_string(), b.to_string())
                            } else {
                                (String::new(), payload.clone())
                            };
                            let now = Instant::now();
                            for line in complete.lines() {
                                if json_str(line, "type").as_deref() == Some("assistant") {
                                    sse.current_turn += 1;
                                }
                                sse.lines.push(line.to_string());
                                sse.line_times.push(now);
                            }
                            sse.pending_tail = tail;
                            got_new = true;
                        }
                        SseWatchMsg::Done { last_id } if !sse.pending_tail.is_empty() => {
                            // Stream is ending — flush any trailing partial line
                            // before transitioning to done. Without this, a final
                            // result line whose terminating `\n` never reached us
                            // (worker exited mid-write) would be invisible.
                            let tail = std::mem::take(&mut sse.pending_tail);
                            let now = Instant::now();
                            for line in tail.lines() {
                                if json_str(line, "type").as_deref() == Some("assistant") {
                                    sse.current_turn += 1;
                                }
                                sse.lines.push(line.to_string());
                                sse.line_times.push(now);
                            }
                            sse.last_event_id = last_id;
                            sse.done = true;
                            got_new = true;
                            break;
                        }
                        SseWatchMsg::Done { last_id } => {
                            sse.last_event_id = last_id;
                            sse.done = true;
                            got_new = true;
                            break;
                        }
                        SseWatchMsg::Error(err_msg) => {
                            // Surface the actual error in the log so the user
                            // can diagnose connection issues without grepping
                            // agent journalctl. Capped to one line in the panel.
                            sse.lines.push(format!("[sse error] {}", err_msg));
                            // #899: every push to `lines` MUST push a matching
                            // timestamp — the render cache-extend path slices
                            // `line_times` with indices derived from `lines.len()`
                            // and panics ("range start index N out of range") if
                            // the two vectors desync.
                            sse.line_times.push(Instant::now());
                            // Connection failure. Update the failure window.
                            sse.fail_count += 1;
                            match sse.first_fail_at {
                                None => sse.first_fail_at = Some(Instant::now()),
                                Some(t) if t.elapsed() > Duration::from_secs(10) => {
                                    // Window expired: reset to a fresh 10-second window.
                                    sse.first_fail_at = Some(Instant::now());
                                    sse.fail_count = 1;
                                }
                                _ => {}
                            }
                            if sse.fail_count >= 3 {
                                sse.done = true;
                                fail_limit_hit = true;
                            } else {
                                needs_reconnect = true;
                            }
                            got_new = true;
                            break;
                        }
                        SseWatchMsg::NotFound => {
                            // #2064: the agent has no log for this assignment
                            // and never will — a 404 on connect, not a
                            // dropped link. Latch done *without* touching
                            // fail_count/first_fail_at, so the `sse.done`
                            // branch's `eligible_for_retry` check above
                            // (which requires `fail_count >= 3`) never fires
                            // and this stream just goes quiet for good,
                            // instead of re-raising "lost connection" forever.
                            sse.lines.push("[no log for this row]".to_string());
                            sse.line_times.push(Instant::now());
                            sse.done = true;
                            not_found = true;
                            got_new = true;
                            break;
                        }
                        SseWatchMsg::Heartbeat => {
                            // No-op: the thread just confirmed the channel is alive.
                        }
                    }
                }
            }
        }

        // Post-drain: reconnect or show error toast (no pool borrow held).
        if fail_limit_hit {
            // Include issue number so background-stream failures are identifiable.
            let issue_num = self.watch_pool.get(id).map(|ctx| ctx.state.issue_number);
            let is_focused = self.watch_focused.as_deref() == Some(id);
            let retry_secs = SSE_RETRY_BACKOFF.as_secs();
            let msg = if is_focused {
                format!(
                    "Lost connection 3× in 10 s — retrying in {retry_secs}s (press R for now)"
                )
            } else if let Some(n) = issue_num {
                format!(
                    "Lost connection to #{n} 3× in 10 s — retrying in {retry_secs}s \
                     (switch to it and press R for now)"
                )
            } else {
                format!("Lost SSE connection 3× in 10 s — retrying in {retry_secs}s")
            };
            self.push_toast("SSE stream error", &msg, ToastSeverity::Error);
        } else if not_found {
            // #2064: terminal, not a connection problem — say the true thing
            // once at info severity instead of an error toast that would
            // otherwise recur every backoff cycle forever (fail_count never
            // reaches the retry-eligible threshold, so this branch only runs
            // on the single tick the `NotFound` message arrives).
            let issue_num = self.watch_pool.get(id).map(|ctx| ctx.state.issue_number);
            let msg = match issue_num {
                Some(n) => format!("#{n} has no log to stream — nothing to watch here"),
                None => "No log to stream for this row".to_string(),
            };
            self.push_toast("No log for this row", &msg, ToastSeverity::Info);
        } else if needs_reconnect {
            // Clone what we need before taking a new mutable borrow.
            let (host, last_id) = match self.watch_pool.get(id) {
                Some(ctx) => (ctx.sse.host.clone(), ctx.sse.last_event_id),
                None => return got_new,
            };
            if !host.is_empty() {
                let new_rx = spawn_sse_watch(&host, id, last_id);
                if let Some(ctx) = self.watch_pool.get_mut(id) {
                    ctx.sse.rx = new_rx;
                }
            }
        }

        got_new
    }

    /// Compatibility alias used by unit tests that still call `drain_sse_watch`.
    /// Delegates to `drain_watch_pool` so all pool entries are processed.
    #[cfg(test)]
    pub(crate) fn drain_sse_watch(&mut self) -> bool {
        self.drain_watch_pool()
    }

    // ─── Settings panel ───────────────────────────────────────────────────────

    /// #237: empty sidebar list for the Settings view.  The form lives in
    /// the main panel and spans the full width; the sidebar shows just a
    /// header so the overall panel chrome stays consistent across views.
    pub(crate) fn settings_sidebar_placeholder(&self) -> ListView {
        ListView {
            id: WidgetId::new("settings-sidebar-placeholder"),
            title: Some(StyledText::plain(" SETTINGS ")),
            items: Vec::new(),
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }


    /// Build the unified settings `Form`.
    ///
    /// #237: All categories render as one full-width scrollable form with
    /// `FieldKind::Label` headers between groups — mirroring vimcode's
    /// settings layout.  The previous version split the panel into a
    /// category nav (sidebar) plus a per-category form (main); that split
    /// caused click hit-test misalignment because `FormController`'s cached
    /// metrics were sized for a different rect than where it was actually
    /// drawn.  One rect ⇒ no drift.
    ///
    /// Field IDs are stable across renders so the `FormController` can
    /// match events correctly.
    pub(crate) fn build_settings_form(&self) -> Form {
        let mut fields: Vec<FormField> = Vec::new();

        // ── Display ────────────────────────────────────────────────────
        fields.push(settings_label("Display"));
        fields.push(FormField {
            id: WidgetId::new("settings:theme"),
            label: StyledText::plain("Theme"),
            kind: FieldKind::SegmentedControl {
                options: Theme::LABELS.iter().map(|s| s.to_string()).collect(),
                selected_idx: self.settings.theme.to_idx(),
            },
            hint: StyledText::plain("Visual style (Light/High Contrast coming soon)"),
            disabled: false,
            validation: None,
        });

        // ── Refresh ────────────────────────────────────────────────────
        fields.push(settings_label("Auto-Refresh"));
        fields.push(FormField {
            id: WidgetId::new("settings:cadence"),
            label: StyledText::plain("Cadence"),
            kind: FieldKind::SegmentedControl {
                options: RefreshCadence::LABELS
                    .iter()
                    .map(|s| s.to_string())
                    .collect(),
                selected_idx: self.settings.refresh_cadence.to_idx(),
            },
            hint: StyledText::plain("How often the board is reloaded from the database"),
            disabled: false,
            validation: None,
        });

        // ── Notifications ──────────────────────────────────────────────
        fields.push(settings_label("Notifications"));
        fields.push(FormField {
            id: WidgetId::new("settings:audio"),
            label: StyledText::plain("Audio on completion"),
            kind: FieldKind::Toggle {
                value: self.settings.audio_on_completion,
            },
            hint: StyledText::plain("Ring a bell when an assignment finishes"),
            disabled: false,
            validation: None,
        });

        // ── Watch Overlay ──────────────────────────────────────────────
        fields.push(settings_label("Watch Overlay"));
        fields.push(FormField {
            id: WidgetId::new("settings:log-ttl"),
            label: StyledText::plain("Log cache TTL"),
            kind: FieldKind::SegmentedControl {
                options: LogCacheTtl::LABELS.iter().map(|s| s.to_string()).collect(),
                selected_idx: self.settings.log_cache_ttl.to_idx(),
            },
            hint: StyledText::plain("How long a fetched log is reused before re-requesting"),
            disabled: false,
            validation: None,
        });

        // ── Keybindings ────────────────────────────────────────────────
        fields.push(settings_label("Keybindings"));
        let refresh_key = self
            .settings
            .keybindings
            .get(ACTION_PIPELINE_REFRESH)
            .map(|s| s.as_str())
            .unwrap_or("Ctrl+R");
        fields.push(FormField {
            id: WidgetId::new("settings:keybind:pipeline_refresh"),
            label: StyledText::plain("Pipeline refresh"),
            kind: FieldKind::TextInput {
                value: refresh_key.to_string(),
                placeholder: "e.g. Ctrl+R or <C-r> or F5".to_string(),
                cursor: None,
                selection_anchor: None,
            },
            hint: StyledText::plain("Key to force-refresh issues from GitHub. Empty = disabled."),
            disabled: false,
            validation: None,
        });

        // ── Machine Models ─────────────────────────────────────────────
        fields.push(settings_label("Per-Machine Model Overrides"));
        if self.data.machines.is_empty() {
            fields.push(FormField {
                id: WidgetId::new("settings:no-machines"),
                label: StyledText::plain("No machines available"),
                kind: FieldKind::ReadOnly {
                    value: StyledText::plain("—"),
                },
                hint: StyledText::plain("Machines are discovered from coordinator.yml"),
                disabled: true,
                validation: None,
            });
        }
        for machine in &self.data.machines {
            let current_pref = self
                .settings
                .machine_model
                .get(&machine.name)
                .copied()
                .unwrap_or_default();
            fields.push(FormField {
                id: WidgetId::new(format!("settings:model:{}", machine.name)),
                label: StyledText::plain(machine.name.clone()),
                kind: FieldKind::SegmentedControl {
                    options: ModelPref::LABELS.iter().map(|s| s.to_string()).collect(),
                    selected_idx: current_pref.to_idx(),
                },
                hint: StyledText::plain(
                    "Session-level override; coordinator.yml is the project default",
                ),
                disabled: false,
                validation: None,
            });
        }

        // Compute focused_field from settings_field_sel, skipping label fields.
        let interactive: Vec<WidgetId> = fields
            .iter()
            .filter(|f| !matches!(f.kind, FieldKind::Label | FieldKind::ReadOnly { .. }))
            .map(|f| f.id.clone())
            .collect();
        let focused_field = interactive.get(self.settings_field_sel).cloned();

        Form {
            id: WidgetId::new("settings-form"),
            fields,
            focused_field,
            scroll_offset: self.settings_form.borrow().scroll_offset(),
            has_focus: true,
        }
    }

    /// Return the IDs of the interactive (non-label, non-read-only) fields
    /// for the current settings category, in form order.
    ///
    /// Used to map `settings_field_sel` to a concrete field when handling
    /// keyboard events.
    pub(crate) fn settings_interactive_field_ids(&self) -> Vec<WidgetId> {
        let form = self.build_settings_form();
        form.fields
            .iter()
            .filter(|f| !matches!(f.kind, FieldKind::Label | FieldKind::ReadOnly { .. }))
            .map(|f| f.id.clone())
            .collect()
    }

    /// Handle a directional key (h/l or Left/Right) against the focused
    /// settings form field.  Returns `true` when a setting changed.
    ///
    /// Builds the form only once to avoid the double-rebuild that occurred
    /// when `settings_interactive_field_ids` and the field-kind lookup both
    /// called `build_settings_form` separately.
    pub(crate) fn settings_change_focused(&mut self, direction: i32) -> bool {
        // Build once; extract both the interactive-field list and the kind.
        let form = self.build_settings_form();
        let interactive: Vec<_> = form
            .fields
            .iter()
            .filter(|f| !matches!(f.kind, FieldKind::Label | FieldKind::ReadOnly { .. }))
            .collect();
        let Some(field) = interactive.get(self.settings_field_sel) else {
            return false;
        };
        let field_id = field.id.clone();

        let event = match &field.kind {
            FieldKind::SegmentedControl {
                options,
                selected_idx,
            } => {
                let n = options.len();
                if n == 0 {
                    return false;
                }
                let new_idx = if direction > 0 {
                    (selected_idx + 1) % n
                } else {
                    selected_idx.checked_sub(1).unwrap_or(n - 1)
                };
                FormEvent::SegmentedControlChanged {
                    id: field_id,
                    selected_idx: new_idx,
                }
            }
            FieldKind::Toggle { value } => FormEvent::ToggleChanged {
                id: field_id,
                value: !value,
            },
            _ => return false,
        };
        self.apply_settings_event(&event)
    }

    /// Recompute `active_theme` from the current `settings.theme` and the
    /// optional `~/.coord/theme.toml` custom-palette file.
    ///
    /// Called after every settings change that might affect the palette.
    pub(crate) fn rebuild_active_theme(&mut self) {
        self.active_theme = crate::settings::TuiSettings::load_custom_theme_file()
            .unwrap_or_else(|| self.settings.theme.to_quadraui_theme());
    }

    /// Apply a `FormEvent` from the settings form to the settings state,
    /// save to disk, and return `true` if something changed.
    ///
    /// When the save fails (e.g. read-only home directory), a non-fatal
    /// error toast is shown so the user is aware without interrupting their
    /// workflow.
    pub(crate) fn apply_settings_event(&mut self, event: &FormEvent) -> bool {
        match event {
            FormEvent::SegmentedControlChanged { id, selected_idx } => {
                match id.as_str() {
                    "settings:theme" => {
                        self.settings.theme = Theme::from_idx(*selected_idx);
                        self.rebuild_active_theme();
                    }
                    "settings:cadence" => {
                        self.settings.refresh_cadence = RefreshCadence::from_idx(*selected_idx);
                    }
                    "settings:log-ttl" => {
                        self.settings.log_cache_ttl = LogCacheTtl::from_idx(*selected_idx);
                    }
                    field_id if field_id.starts_with("settings:model:") => {
                        let machine = field_id["settings:model:".len()..].to_string();
                        self.settings
                            .machine_model
                            .insert(machine, ModelPref::from_idx(*selected_idx));
                    }
                    _ => return false,
                }
                if let Err(e) = self.settings.save() {
                    self.push_toast(
                        "Settings",
                        &format!("could not persist settings: {e}"),
                        ToastSeverity::Error,
                    );
                }
                true
            }
            FormEvent::ToggleChanged { id, value } => {
                if id.as_str() == "settings:audio" {
                    self.settings.audio_on_completion = *value;
                    if let Err(e) = self.settings.save() {
                        self.push_toast(
                            "Settings",
                            &format!("could not persist settings: {e}"),
                            ToastSeverity::Error,
                        );
                    }
                    true
                } else {
                    false
                }
            }
            FormEvent::TextInputChanged { id, value }
            | FormEvent::TextInputCommitted { id, value }
                if id.as_str().starts_with("settings:keybind:") =>
            {
                let action = &id.as_str()["settings:keybind:".len()..];
                self.settings
                    .keybindings
                    .insert(action.to_string(), value.clone());
                self.parsed_keybindings = parse_keybindings(&self.settings);
                if let Err(e) = self.settings.save() {
                    self.push_toast(
                        "Settings",
                        &format!("could not persist settings: {e}"),
                        ToastSeverity::Error,
                    );
                }
                true
            }
            _ => false,
        }
    }
}


// ─── Settings helpers ─────────────────────────────────────────────────────────

/// Build a non-interactive category label `FormField` for the settings form.
pub(crate) fn settings_label(text: &str) -> FormField {
    FormField {
        id: WidgetId::new(format!(
            "settings-label:{}",
            text.to_lowercase().replace(' ', "-")
        )),
        label: StyledText::plain(text.to_string()),
        kind: FieldKind::Label,
        hint: StyledText::default(),
        disabled: false,
        validation: None,
    }
}

// ─── Purge helper ─────────────────────────────────────────────────────────────

/// Open a short-lived read-write connection to `coord.db` and delete:
///
/// * `assignments` rows where `status IN ('done', 'failed')` and
///   `finished_at < now - older_than_secs`
/// * `issues` rows where `state = 'closed'` and
///   `synced_at < now - older_than_secs`
///
/// Returns the total number of rows deleted across both tables.
///
/// A separate read-write connection is used because the main data-load
/// connection is opened with `SQLITE_OPEN_READ_ONLY`.  SQLite WAL mode
/// serialises concurrent writers, so this is safe.
/// Compute the cutoff timestamp for purge predicates.
pub(crate) fn purge_cutoff(older_than_secs: f64) -> f64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    now - older_than_secs
}

/// Count rows that would be deleted by [`purge_done_assignments_conn`].
/// Inner helper that takes an explicit connection so tests can run against
/// an in-memory DB without touching the real coord.db.
pub(crate) fn count_purgeable_conn(conn: &Connection, cutoff: f64) -> rusqlite::Result<(usize, usize)> {
    let a: i64 = conn.query_row(
        "SELECT COUNT(*) FROM assignments \
         WHERE status IN ('done', 'failed') \
         AND finished_at IS NOT NULL \
         AND finished_at < ?1",
        rusqlite::params![cutoff],
        |r| r.get(0),
    )?;
    let i: i64 = conn.query_row(
        "SELECT COUNT(*) FROM issues \
         WHERE state = 'closed' \
         AND synced_at IS NOT NULL \
         AND synced_at < ?1",
        rusqlite::params![cutoff],
        |r| r.get(0),
    )?;
    Ok((a as usize, i as usize))
}

/// Delete old `done`/`failed` assignments and old closed issues.
/// Inner helper — see [`count_purgeable_conn`].
pub(crate) fn purge_done_assignments_conn(conn: &Connection, cutoff: f64) -> rusqlite::Result<(usize, usize)> {
    let assignments_deleted = conn.execute(
        "DELETE FROM assignments \
         WHERE status IN ('done', 'failed') \
         AND finished_at IS NOT NULL \
         AND finished_at < ?1",
        rusqlite::params![cutoff],
    )?;
    let issues_deleted = conn.execute(
        "DELETE FROM issues \
         WHERE state = 'closed' \
         AND synced_at IS NOT NULL \
         AND synced_at < ?1",
        rusqlite::params![cutoff],
    )?;
    Ok((assignments_deleted, issues_deleted))
}

/// Count rows that would be deleted by [`purge_done_assignments_db`].
///
/// Returns `(assignments, closed_issues)` so the confirmation prompt and the
/// completion toast show matching numbers — the user is never surprised by
/// a "47 rows removed" toast after confirming "Purge 3 rows".
pub(crate) fn count_purgeable_db(older_than_secs: f64) -> rusqlite::Result<(usize, usize)> {
    let conn = open_purge_conn()?;
    count_purgeable_conn(&conn, purge_cutoff(older_than_secs))
}

/// Delete old `done`/`failed` assignments and old closed issues.
/// Returns `(assignments_deleted, issues_deleted)`; errors propagate to the
/// caller for a visible error toast (silent `.unwrap_or(0)` previously hid
/// SQLITE_BUSY).
pub(crate) fn purge_done_assignments_db(older_than_secs: f64) -> rusqlite::Result<(usize, usize)> {
    let conn = open_purge_conn()?;
    purge_done_assignments_conn(&conn, purge_cutoff(older_than_secs))
}

/// #200: Record a Test gate verdict on the given work assignment id.
/// `verdict` is "passed" | "failed" | "skipped". `reason` is only stored for
/// failures (ignored otherwise).
///
/// Also mirrors `smoke_test` / `smoke_test_reason` so `coord fix` can see the
/// verdict — `coord fix` guards on `smoke_test == "fail"` (cli.py:4285-4291).
/// Without this mirror, a report+fix dispatched from the TUI would immediately
/// exit-1 with "smoke_test is None, expected 'fail'".  Mirrors the same logic
/// as `coord test --fail/--pass` (cli.py:3308-3312).
///
/// Inner function accepts an explicit connection so tests can run against an
/// in-memory DB without touching the real coord.db.
pub(crate) fn record_test_verdict_conn(
    conn: &Connection,
    assignment_id: &str,
    verdict: &str,
    reason: Option<&str>,
) -> rusqlite::Result<()> {
    conn.execute(
        "UPDATE assignments SET test_state = ?1, test_reason = ?2 WHERE assignment_id = ?3",
        rusqlite::params![verdict, reason, assignment_id],
    )?;
    // Mirror smoke_test for "passed" and "failed" verdicts only (same as CLI).
    if matches!(verdict, "failed" | "passed") {
        let smoke_val: &str = if verdict == "failed" { "fail" } else { "pass" };
        let smoke_reason: Option<&str> = if verdict == "failed" { reason } else { None };
        conn.execute(
            "UPDATE assignments SET smoke_test = ?1, smoke_test_reason = ?2 \
             WHERE assignment_id = ?3",
            rusqlite::params![smoke_val, smoke_reason, assignment_id],
        )?;
    }
    Ok(())
}

/// #590 Phase 2: POST the verdict to the daemon when a board service is set
/// (the thin client's local coord.db is the wrong DB). Mirrors the smoke_test
/// derivation in [`record_test_verdict_conn`] so the daemon writes the same
/// columns. Built on the shared [`post_daemon_json`] helper (#1012).
pub(crate) fn record_test_verdict_remote(
    assignment_id: &str,
    verdict: &str,
    reason: Option<&str>,
) -> Result<(), String> {
    let (url, token) = resolve_board_service().ok_or("no board service configured")?;
    let (smoke_test, smoke_reason): (Option<&str>, Option<&str>) = match verdict {
        "failed" => (Some("fail"), reason),
        "passed" => (Some("pass"), None),
        _ => (None, None),
    };
    let body = serde_json::json!({
        "assignment_id": assignment_id,
        "test_state": verdict,
        "test_reason": reason,
        "smoke_test": smoke_test,
        "smoke_test_reason": smoke_reason,
    });
    post_daemon_json(&url, token.as_deref(), "/test-verdict", &body)?;
    Ok(())
}

/// #1012: (add, remove) label sets for each `dispatch_board_row_command`
/// subcommand — the shared entry point behind the Board row-menu actions
/// Refine, Send-to-Pipeline (`track`), Mark Refined (`ready`), and
/// Drop-to-Backlog/Drop-to-Refining (`backlog`/`refine`) — plus #1500's
/// Pipeline row actions Mark ready / Unmark ready (`queue`/`unqueue`),
/// consumed directly by `apply_staging_label_change` rather than through
/// `dispatch_board_row_command`. These mirror the Python
/// `_apply_label_change` call sites **exactly**: `coord/commands/chat.py`
/// (`ready`, `refine`) and `coord/commands/issues.py` (`track`, `backlog`,
/// `queue`, `unqueue`). Any drift here silently desyncs the Rust
/// direct-POST path from the `coord` CLI fallback, so keep these lists
/// byte-for-byte in sync with those functions.
///
/// Returns `None` for a subcommand this seam doesn't know how to translate
/// to a label change — the caller falls back to the `coord` subprocess for
/// those (e.g. any future subcommand added to `dispatch_board_row_command`
/// without also updating this table).
pub(crate) fn label_change_for_subcommand(
    subcommand: &str,
) -> Option<(&'static [&'static str], &'static [&'static str])> {
    match subcommand {
        // coord/commands/chat.py::ready
        "ready" => Some((
            &["status:ready", "coord"],
            &["status:refining", "status:backlog"],
        )),
        // coord/commands/chat.py::refine
        "refine" => Some((&["status:refining"], &["status:ready"])),
        // coord/commands/issues.py::track
        "track" => Some((
            &["coord", "status:ready"],
            &["status:refining", "status:backlog"],
        )),
        // coord/commands/issues.py::backlog
        "backlog" => Some((
            &[],
            &["status:refining", "status:ready", "status:queued"],
        )),
        // coord/commands/issues.py::queue (#1500 "Mark ready")
        "queue" => Some((&["status:queued"], &[])),
        // coord/commands/issues.py::unqueue (#1500 "Unmark ready")
        "unqueue" => Some((&[], &["status:queued"])),
        _ => None,
    }
}

/// #1012: POST an add/remove label change straight to the daemon's
/// `/issue-label` seam (mirrors Python `state.apply_issue_labels` /
/// `_apply_label_change`), built on the shared [`post_daemon_json`] helper.
/// Returns `(new_labels, changed)` — `changed` is `false` when every `add`
/// label was already present and every `remove` label was already absent
/// (the daemon computes this the same way the CLI's no-op detection does).
pub(crate) fn apply_issue_labels_remote(
    url: &str,
    token: Option<&str>,
    repo_name: &str,
    issue_number: u64,
    add: &[&str],
    remove: &[&str],
    repo_github: Option<&str>,
) -> Result<(Vec<String>, bool), String> {
    let body = serde_json::json!({
        "repo_name": repo_name,
        "issue_number": issue_number,
        "add": add,
        "remove": remove,
        "repo_github": repo_github,
    });
    let resp = post_daemon_json(url, token, "/issue-label", &body)?;
    let labels = resp
        .get("labels")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();
    let changed = resp
        .get("changed")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    Ok((labels, changed))
}

pub(crate) fn record_test_verdict_db(
    assignment_id: &str,
    verdict: &str,
    reason: Option<&str>,
) -> rusqlite::Result<()> {
    if is_remote_board_service() {
        return record_test_verdict_remote(assignment_id, verdict, reason).map_err(|e| {
            rusqlite::Error::ToSqlConversionFailure(Box::new(std::io::Error::new(
                std::io::ErrorKind::Other,
                e,
            )))
        });
    }
    let conn = open_purge_conn()?;
    record_test_verdict_conn(&conn, assignment_id, verdict, reason)
}
