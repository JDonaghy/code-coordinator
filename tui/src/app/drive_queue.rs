//! The operator-declared `coord drive` work queue (#1750 DQ-3 / #1755) —
//! an always-visible status-bar segment, the Queue panel (#1866/#1867), and
//! "Add to drive queue" on the Pipeline row menu.
//!
//! The TUI is where the operator reads the pipeline and forms the intent
//! "that one next"; before this, acting on that intent meant leaving the TUI
//! for a shell, and the epic's alert requirement ("if nothing can start,
//! alert the operator") had no surface at all.
//!
//! **Read path is entirely server-derived.** `self.data.drive_queue` is a raw
//! dump of the daemon's `drive_queue` table (`/board`'s `drive_queue` key,
//! see [`BoardDriveQueueEntry`]); nothing here recomputes a row's `state` —
//! DQ-2's tick decides `waiting`/`running`/`blocked`/`done` and this module
//! consumes that verbatim, exactly the way `fleet_health.rs` consumes a
//! probe's `severity`. The one thing derived locally is the *aggregate*
//! sentence in the status bar ([`drive_queue_status_text`]), which is a pure
//! function of the rows so it is directly testable without a driver.
//!
//! **Write path is `coord drive-queue …` through the spawn-and-toast seam**
//! (`command_runner.spawn_queued` + [`CoordApp::push_toast`], the same
//! posture as `milestone_dag.rs` / `escalation.rs`) — no direct DB access,
//! and no client-side mutation of `data.drive_queue` beyond the optimistic
//! removal `escalation.rs` already precedents.
//!
//! **One queue surface, not two (#1868).** Until #1868 this module also
//! carried a right-click detail overlay that rendered the same rows through
//! a second code path — two surfaces over one data source drift, and #1868
//! retired it now that the Queue panel is proven. The status-bar segment's
//! "Drive queue…" menu item now switches straight to the panel
//! (`SidebarView::Queue`) instead of opening a modal.
//!
//! **Import pattern:** `use super::*` is intentional — see `fleet_health.rs`
//! / `escalation.rs` / `drive.rs` for the same rationale.
#[allow(unused_imports)]
use super::*;

/// Wire values of `drive_queue.state` this module reasons about. Everything
/// else (an unrecognised state from a newer daemon) counts as neither
/// eligible nor blocked and is rendered verbatim — never silently folded
/// into a healthy bucket.
pub(crate) const QUEUE_STATE_WAITING: &str = "waiting";
pub(crate) const QUEUE_STATE_RUNNING: &str = "running";
pub(crate) const QUEUE_STATE_BLOCKED: &str = "blocked";
pub(crate) const QUEUE_STATE_DONE: &str = "done";

/// #1757: wire values of `drive_queue.hold_state` — the DEPLOY GATE's
/// lifecycle, orthogonal to the entry's queue `state` above. `fired` is the
/// only one that stops the queue; `armed` is a gate that has not triggered
/// yet and `released` one a human (or a passing `resume_when`) has cleared.
/// Mirrors `HOLD_*` in `coord/drive_queue.py`.
pub(crate) const HOLD_STATE_FIRED: &str = "fired";

/// #2186: wire values of `drive_queue.hold_scope` — how far a FIRED gate
/// reaches. `entry` (the default) holds only entries whose own `after` names
/// the gate's key; `fleet` is the deliberately-opt-in whole-queue stop.
/// Mirrors `HOLD_SCOPE_ENTRY`/`HOLD_SCOPE_FLEET` in `coord/drive_queue.py`.
pub(crate) const HOLD_SCOPE_ENTRY: &str = "entry";
pub(crate) const HOLD_SCOPE_FLEET: &str = "fleet";

/// #1755: pending "Add to drive queue after…" text input. Mirrors
/// `PendingMilestoneRowInput`'s single-buffer shape (Enter submits, Esc
/// cancels) rather than a full form — the whole payload is a short list of
/// issue numbers.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PendingDriveQueueAfter {
    /// Coord-local repo of the row being queued — also the repo bare issue
    /// numbers in `buf` resolve against (`parse_after_spec`'s rule).
    pub(crate) repo_name: String,
    pub(crate) issue_number: u64,
    pub(crate) buf: String,
}

/// Severity of the aggregate queue reading, in ascending rank order
/// (declaration order IS the rank, `#[derive(Ord)]` — same convention as
/// `FleetSeverity`). `Blocked` outranks `Stalled` per the issue's own
/// "blocked outranks stalled" rule.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Default)]
pub(crate) enum DriveQueueLevel {
    /// Nothing queued at all. Muted — an empty queue is not a problem, but
    /// it must still SAY it's empty (#1631's "OK states its OK-ness").
    #[default]
    Empty,
    /// Something is running and/or something is waiting behind it.
    Normal,
    /// Rows are waiting and not one of them can start. This is the epic's
    /// alert case: the queue looks busy but nothing will ever move without
    /// the operator.
    Stalled,
    /// #1757/#2186: a FLEET-scoped DEPLOY GATE has fired. The queue is
    /// deliberately stopped and will not launch anything — including a fully
    /// eligible, unrelated entry — until a human deploys and releases it.
    ///
    /// Deliberately NOT raised for an entry-scoped gate (the default since
    /// #2186): that only holds its own dependents, and the rest of the queue
    /// keeps launching in the same tick — see `coord/drive_queue.py`'s
    /// `Hold.stops_fleet`, which this mirrors, and the #2186 incident this
    /// level exists to stop repeating (one gate silently reading as a
    /// fleet-wide stop on the one always-visible surface).
    ///
    /// Outranks `Stalled` (this is a definite stop, not "nothing happens to
    /// be eligible right now") and ranks below `Blocked` (a gate is the
    /// system working as designed; `blocked` is something that failed).
    Held,
    /// At least one row is `blocked` — a hard stop DQ-2 already gave up on.
    Blocked,
}

impl DriveQueueLevel {
    /// `(fg, bg)` for the status-bar segment. Deliberately reuses the exact
    /// palette `FleetSeverity::colors` uses for ok/warn/crit so two adjacent
    /// segments never disagree about what amber means.
    pub(crate) fn colors(self) -> (Color, Color) {
        match self {
            // Muted grey-on-dark: present, legible, visibly *not* an alert.
            DriveQueueLevel::Empty => (Color::rgb(150, 150, 160), Color::rgb(30, 30, 40)),
            DriveQueueLevel::Normal => (Color::rgb(200, 220, 255), Color::rgb(40, 60, 90)),
            // Same warn amber as `Stalled`: both mean "the queue has stopped
            // and needs you", and two different ambers for one meaning is how
            // a status bar stops being readable at a glance.
            DriveQueueLevel::Stalled | DriveQueueLevel::Held => {
                (Color::rgb(255, 210, 100), Color::rgb(70, 45, 10))
            }
            DriveQueueLevel::Blocked => (Color::rgb(255, 255, 255), Color::rgb(150, 30, 30)),
        }
    }
}

/// Rows that still have work ahead of them — `done` entries are history and
/// must not inflate "N waiting" or keep the segment shouting forever.
fn is_pending(e: &BoardDriveQueueEntry) -> bool {
    e.state != QUEUE_STATE_DONE
}

/// #1757: is this row's OWN deploy gate currently fired?
///
/// Read straight off `hold_state`, which the tick owns — the TUI never
/// re-derives "should this be held" from `hold_after` + `state`, for the same
/// reason it never re-derives `waiting`/`running`: two implementations of one
/// rule drift, and the one an operator is reading is the wrong one.
///
/// Deliberately NOT gated on `is_pending`: a fired gate lives on a `done`
/// entry by construction (the gate fires when the entry lands), so filtering
/// `done` rows out here would hide every hold that exists.
///
/// Scope-agnostic (#2186) — this only says "this entry's own gate is
/// closed", the same thing `Hold.blocking` says in `coord/drive_queue.py`.
/// It does NOT say the queue is stopped; for that, see [`stops_fleet`].
pub(crate) fn is_holding(e: &BoardDriveQueueEntry) -> bool {
    e.hold_state == HOLD_STATE_FIRED
}

/// #2186: does this row's fired gate stop the WHOLE queue?
///
/// Mirrors `Hold.stops_fleet` in `coord/drive_queue.py` exactly: `blocking
/// and scope == fleet`. An armed-not-fired gate never stops anything
/// (`is_holding` is `false`); a fired gate stops only its own dependents
/// unless it was explicitly declared `--scope=fleet` at `add` time. This is
/// the one predicate [`summarize_drive_queue`] and [`drive_queue_status_text`]
/// consult before claiming "nothing will launch" — consulting `is_holding`
/// alone here is exactly the bug #2186 fixes: every future entry-scoped gate
/// firing would read as a fleet-wide stop on the always-visible status bar.
pub(crate) fn stops_fleet(e: &BoardDriveQueueEntry) -> bool {
    is_holding(e) && e.hold_scope == HOLD_SCOPE_FLEET
}

/// The sentence shown for a held gate — the operator's own `hold_reason`
/// when there is one, else a fallback naming the entry. Never empty: a status
/// bar that says only "HELD" makes the operator go and reconstruct why.
fn hold_headline(e: &BoardDriveQueueEntry) -> String {
    if e.hold_reason.is_empty() {
        format!("deploy gate after {}", e.key())
    } else {
        e.hold_reason.clone()
    }
}

/// #2589: does *reason* name the "claimed success, wrote nothing" signature
/// — an acceptance-author or plain work session that exited DONE/ADVISORY
/// while its branch carried zero commits?
///
/// Mirrors `coord.drive_queue._is_empty_branch_death_reason` exactly (same
/// two text shapes, same "no commits" required in both) — a pure text
/// classifier, not a typed column, because neither Python `_die` call site
/// this recognizes embeds a dedicated marker; today nothing reads their
/// reason text for anything but display, on either side of the wire.
fn is_empty_branch_death_reason(reason: &str) -> bool {
    let lowered = reason.to_lowercase();
    if !lowered.contains("no commits") {
        return false;
    }
    if lowered.contains("acceptance author")
        && (lowered.contains("exited advisory") || lowered.contains("exited done"))
    {
        return true;
    }
    lowered.contains("exited advisory") && lowered.contains("nothing was pushed")
}

/// #2589: does *reason* carry the "no assignment was ever created for this
/// run" marker `coord.drive_queue._reconcile_running` stamps onto a
/// `retry`/`exhausted` reason when `_dispatch_produced_nothing` fires?
///
/// Mirrors `coord.drive_queue._is_dispatch_failure_reason` exactly.
fn is_dispatch_failure_reason(reason: &str) -> bool {
    reason
        .to_lowercase()
        .contains("no assignment was ever created for this run")
}

/// #2589: does *reason* name a `blocked` cause with NOTHING for the
/// coordinator's #2230 merge-gate sweep to re-check?
///
/// Mirrors `coord.drive_queue.is_pre_dispatch_block_reason` exactly — see
/// its docstring for the full rationale. The short version: both shapes
/// below are pre-dispatch by construction (no branch, no PR, no merge-queue
/// row ever existed for this run), so `coord`'s own `_blocked_gate_reading`
/// has "no evidence either way" for them, forever — a `blocked` row this
/// classifier matches needs a human, not a tick. Used by [`CoordApp::
/// queue_row`] to render such a row visibly distinct from an ordinary
/// `blocked` row that genuinely might self-heal, closing the exact gap
/// claude-coordinator#2589 reports: two `blocked` rows that looked
/// identical, one of which needed an operator and the other did not.
pub(crate) fn is_pre_dispatch_block_reason(reason: &str) -> bool {
    is_empty_branch_death_reason(reason) || is_dispatch_failure_reason(reason)
}

/// Is this row's `after` list satisfied by the queue it sits in?
///
/// Deliberately a *local, conservative* read: the authoritative eligibility
/// decision belongs to DQ-2's tick (which also weighs board state, capacity
/// and machine routing). All this answers is the question the operator can
/// verify by eye — "is a pre-req of mine still sitting in this same queue,
/// unfinished, OR gated shut?" — which is the only stall cause the TUI can
/// name without re-implementing the tick. A pre-req that isn't in the queue
/// at all is treated as satisfied (it may have landed long ago).
///
/// #2186: a pre-req whose OWN gate has fired is unsatisfied even though its
/// row has by then reconciled to `done` — mirroring `_resolve_prereqs` in
/// `coord/drive_queue.py`, which checks `held_gates` BEFORE `facts.landed`
/// for exactly this reason (an entry merging is not the same fact as its
/// gate having released). Checked regardless of the gate's own scope: an
/// entry-scoped gate still holds ITS dependents, which is precisely the case
/// this local read must not call eligible.
fn after_satisfied(entry: &BoardDriveQueueEntry, all: &[BoardDriveQueueEntry]) -> bool {
    entry.after.iter().all(|key| {
        !all.iter().any(|other| {
            other.key() == *key && (other.state != QUEUE_STATE_DONE || is_holding(other))
        })
    })
}

/// Aggregate reading + counts behind [`drive_queue_status_text`]. Split out
/// so the segment's colour and its text can never be derived from two
/// different passes over the rows.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct DriveQueueSummary {
    pub(crate) level: DriveQueueLevel,
    pub(crate) running: usize,
    pub(crate) waiting: usize,
    pub(crate) blocked: usize,
    /// Waiting rows whose in-queue pre-reqs are all satisfied — i.e. rows a
    /// tick could plausibly pick up. Zero-with-waiting-rows is the stall.
    pub(crate) eligible: usize,
    /// #1757: rows whose OWN deploy gate has fired, any scope. Purely
    /// informational (e.g. the Queue panel sidebar's "N held" line) — it
    /// does NOT by itself mean the tick will launch nothing; an entry-scoped
    /// gate (the default since #2186) only holds its own dependents. See
    /// [`DriveQueueSummary::fleet_held`] for the count that actually stops
    /// the queue.
    pub(crate) held: usize,
    /// #2186: rows whose FLEET-scoped deploy gate has fired. Non-zero means
    /// the tick will launch nothing at all, whatever `eligible` says — the
    /// one count [`summarize_drive_queue`]'s `level` and
    /// [`drive_queue_status_text`]'s HELD branch actually act on. Zero even
    /// while `held` is non-zero is the entry-scoped case #2186 exists for:
    /// one gate fired, the rest of the queue keeps moving.
    pub(crate) fleet_held: usize,
}

/// Summarise the queue. Pure over the board rows — no clock, no self.
pub(crate) fn summarize_drive_queue(entries: &[BoardDriveQueueEntry]) -> DriveQueueSummary {
    let mut s = DriveQueueSummary::default();
    // Counted over ALL entries, not just pending ones: a fired gate sits on a
    // `done` row by construction.
    s.held = entries.iter().filter(|e| is_holding(e)).count();
    s.fleet_held = entries.iter().filter(|e| stops_fleet(e)).count();
    for e in entries.iter().filter(|e| is_pending(e)) {
        match e.state.as_str() {
            QUEUE_STATE_RUNNING => s.running += 1,
            QUEUE_STATE_BLOCKED => s.blocked += 1,
            QUEUE_STATE_WAITING => {
                s.waiting += 1;
                if after_satisfied(e, entries) {
                    s.eligible += 1;
                }
            }
            // An unrecognised state from a newer daemon: counted as pending
            // (it is not `done`) but never as waiting/eligible, so it can
            // neither trigger nor mask a stall.
            _ => {}
        }
    }
    s.level = if s.blocked > 0 {
        // Blocked outranks stalled — a hard stop is worse news than a queue
        // that is merely waiting for capacity.
        DriveQueueLevel::Blocked
    } else if s.fleet_held > 0 {
        // #1757/#2186: a FLEET-scoped fired gate outranks a stall, and it
        // outranks it even when the stall is REAL — "3 waiting, none
        // eligible" is a symptom here, and "you have a deploy to do" is the
        // cause. Showing the symptom would send the operator looking for a
        // dependency bug. Deliberately `fleet_held`, not `held`: an
        // entry-scoped gate does not stop the queue, so it must not force
        // this level either — that was the #2186 incident.
        DriveQueueLevel::Held
    } else if s.waiting > 0 && s.eligible == 0 && s.running == 0 {
        // Nothing running AND nothing that could start: the epic's "if
        // nothing can start, alert the operator" case. A row running means
        // the queue IS moving, so waiting-behind-it is normal, not a stall.
        DriveQueueLevel::Stalled
    } else if s.running > 0 || s.waiting > 0 {
        DriveQueueLevel::Normal
    } else {
        DriveQueueLevel::Empty
    };
    s
}

/// The status-bar sentence. Always non-empty — an empty queue reads
/// `QUEUE: empty`, never nothing (#1631: silence is indistinguishable from a
/// broken feature).
///
/// ```text
/// QUEUE: empty
/// QUEUE: 1 running · 3 waiting
/// QUEUE: STALLED — 3 waiting, none eligible
/// QUEUE: HELD — release + restart coord-serve
/// QUEUE: BLOCKED 2 · 1 waiting
/// ```
pub(crate) fn drive_queue_status_text(entries: &[BoardDriveQueueEntry]) -> String {
    let s = summarize_drive_queue(entries);
    match s.level {
        DriveQueueLevel::Empty => "QUEUE: empty".to_string(),
        DriveQueueLevel::Held => {
            // #2186: this branch is only reached when `fleet_held > 0`, but
            // the queue may ALSO carry an entry-scoped fired gate (lower
            // position, say) — `find` must land on the fleet-scoped one
            // specifically, or the bar would name the wrong gate.
            let gate = entries
                .iter()
                .find(|e| stops_fleet(e))
                .map(hold_headline)
                .unwrap_or_else(|| "deploy gate".to_string());
            let mut out = format!("QUEUE: HELD — {gate}");
            // A rising probe count is the difference between "the deploy is
            // pending" and "the probe has been failing for two hours" — the
            // second is the one that needs a human, so it must be on the bar.
            if let Some(p) = entries
                .iter()
                .find(|e| stops_fleet(e))
                .map(|e| e.hold_probes)
                .filter(|p| *p > 0)
            {
                out.push_str(&format!(" (probe failed {p}×)"));
            }
            out
        }
        DriveQueueLevel::Blocked => {
            let mut out = format!("QUEUE: BLOCKED {}", s.blocked);
            if s.running > 0 {
                out.push_str(&format!(" · {} running", s.running));
            }
            if s.waiting > 0 {
                out.push_str(&format!(" · {} waiting", s.waiting));
            }
            out
        }
        DriveQueueLevel::Stalled => format!(
            "QUEUE: STALLED — {} waiting, none eligible",
            s.waiting
        ),
        DriveQueueLevel::Normal => {
            let mut parts: Vec<String> = Vec::new();
            if s.running > 0 {
                parts.push(format!("{} running", s.running));
            }
            if s.waiting > 0 {
                parts.push(format!("{} waiting", s.waiting));
            }
            format!("QUEUE: {}", parts.join(" · "))
        }
    }
}

impl CoordApp {
    /// The persistent status-bar segment — ALWAYS present, in every view,
    /// regardless of depth (see [`drive_queue_status_text`]). Carries no
    /// `action_id`: reached by right-clicking the status bar, per the module
    /// doc comment.
    pub(crate) fn drive_queue_status_bar_segment(&self) -> StatusBarSegment {
        let entries = &self.data.drive_queue;
        let level = summarize_drive_queue(entries).level;
        let (fg, bg) = level.colors();
        StatusBarSegment {
            text: format!(" {} ", drive_queue_status_text(entries)),
            fg,
            bg,
            // Bold only when it's news — an idle/normal queue shouldn't
            // compete with the fleet-health segment next to it for attention.
            bold: matches!(
                level,
                DriveQueueLevel::Stalled | DriveQueueLevel::Held | DriveQueueLevel::Blocked
            ),
            action_id: None,
        }
    }

    /// The status bar's "Drive queue…" item, appended to the shared
    /// right-click-the-status-bar menu alongside `context_menu_items_for_
    /// fleet_health`'s. Accelerator shown *inside* the menu, same framing.
    pub(crate) fn context_menu_items_for_drive_queue_segment(&self) -> Vec<ContextMenuItem> {
        vec![ContextMenuItem::action("open-drive-queue-detail", "Drive queue…").with_shortcut("q")]
    }

    /// The queue in run order (`position` ascending, `repo#issue` as a
    /// stable tiebreaker so two rows that momentarily share a position
    /// during a `move` never flicker past each other).
    pub(crate) fn drive_queue_entries(&self) -> Vec<&BoardDriveQueueEntry> {
        let mut rows: Vec<&BoardDriveQueueEntry> = self.data.drive_queue.iter().collect();
        rows.sort_by(|a, b| {
            a.position
                .cmp(&b.position)
                .then_with(|| a.repo_name.cmp(&b.repo_name))
                .then_with(|| a.issue_number.cmp(&b.issue_number))
        });
        rows
    }

    /// Is (repo, issue) already queued? Drives the Pipeline menu's
    /// "Add to drive queue" ⇄ "Remove from drive queue" swap.
    ///
    /// `done` rows count as *not* queued: the row is history, and re-queuing
    /// it is exactly what an operator re-running a merged-then-reopened issue
    /// wants (`coord drive-queue add` updates in place anyway).
    pub(crate) fn drive_queue_contains(&self, repo: &str, issue: u64) -> bool {
        self.data.drive_queue.iter().any(|e| {
            e.repo_name == repo && e.issue_number == issue as i64 && e.state != QUEUE_STATE_DONE
        })
    }

    /// The queued entry for (repo, issue), if any — including `done` rows,
    /// which the overlay still lists.
    pub(crate) fn drive_queue_entry_for(
        &self,
        repo: &str,
        issue: u64,
    ) -> Option<&BoardDriveQueueEntry> {
        self.data
            .drive_queue
            .iter()
            .find(|e| e.repo_name == repo && e.issue_number == issue as i64)
    }

    /// `(coord-local repo, issue)` for a Pipeline/Board right-click target,
    /// or `None` when the row has no issue or no repo mapping.
    ///
    /// Every `coord drive-queue` verb is repo-scoped, so a row whose
    /// `coord_repo` never resolved (an issue in a repo `coordinator.yml`
    /// doesn't know) has nothing this module can do with it — the menu items
    /// are suppressed for exactly that case (see
    /// [`Self::drive_queue_menu_items_for_pipeline_row`]), and this returning
    /// `None` is the belt-and-braces half of the same guard.
    pub(crate) fn pipeline_menu_repo_issue(
        &self,
        target: &ContextMenuTarget,
    ) -> Option<(String, u64)> {
        match target {
            ContextMenuTarget::PipelineRow {
                issue_number,
                repo_name,
                ..
            }
            | ContextMenuTarget::BoardRow {
                issue_number,
                repo_name,
                ..
            } => Some((repo_name.clone()?, (*issue_number)?)),
            // #2375: the Queue panel's own row now offers the same
            // `decide-escalation:<index>` submenu the Pipeline row does
            // (`context_menu_items_for_drive_queue_row`), so its dispatch
            // needs the same repo/issue resolution — `issue_number` here is
            // a non-optional `i64` (a queue row always names one), matching
            // the `.max(0) as u64` clamp `context_menu_items_for_drive_queue_row`
            // already uses to build the escalation lookup.
            ContextMenuTarget::DriveQueueRow {
                repo_name,
                issue_number,
                ..
            } => Some((repo_name.clone(), (*issue_number).max(0) as u64)),
            _ => None,
        }
    }

    /// The drive-queue block of a Pipeline row's right-click menu.
    ///
    /// A row already in the queue offers **Remove** instead of the three Add
    /// variants — re-adding a queued row is a silent in-place update
    /// (`coord drive-queue add` upserts), which is not what "Add" reads as
    /// on a row that is already there.
    ///
    /// Suppressed entirely for a row with no issue number or no coord repo:
    /// `coord drive-queue add` takes `<repo> <issue>` and refuses a repo
    /// `coordinator.yml` has never heard of, so an unmapped row could only
    /// ever produce a failing command.
    ///
    /// `offer_add` is the "plausibly drivable" gate. `false` on an
    /// already-in-progress row: `coord drive` takes an issue from wherever
    /// it is to merged, so queuing a row that a worker (or another drive) is
    /// already mid-way through is a race, not an intent. **Remove is still
    /// offered in that case** — a row queued while New and since started
    /// must remain dequeueable from the row it's actually on.
    pub(crate) fn drive_queue_menu_items_for_pipeline_row(
        &self,
        issue_number: Option<u64>,
        repo_name: Option<&str>,
        offer_add: bool,
    ) -> Vec<ContextMenuItem> {
        let (Some(issue), Some(repo)) = (issue_number, repo_name) else {
            return Vec::new();
        };
        if repo.is_empty() {
            return Vec::new();
        }
        if self.drive_queue_contains(repo, issue) {
            // Name the slot it currently holds — the operator's next question
            // after "it's already queued" is always "where in the queue?",
            // and this is the only place a Pipeline row can answer it
            // without opening the overlay.
            let label = match self.drive_queue_entry_for(repo, issue) {
                Some(e) => format!("Remove from drive queue (position {})", e.position),
                None => "Remove from drive queue".to_string(),
            };
            return vec![ContextMenuItem::action("drive-queue-row-remove", &label)];
        }
        if !offer_add {
            return Vec::new();
        }
        let mut items = vec![ContextMenuItem::action("drive-queue-add", "Add to drive queue")];
        // "on…" — pin the drive to one machine. Built from the board's own
        // machine list so it can never offer a machine this coordinator
        // doesn't route to; omitted entirely when the board has none (an
        // empty submenu is a dead end, not an affordance).
        let machines: Vec<ContextMenuItem> = self
            .data
            .machines
            .iter()
            .map(|m| {
                ContextMenuItem::action(&format!("drive-queue-add-on:{}", m.name), &m.name)
            })
            .collect();
        if !machines.is_empty() {
            items.push(ContextMenuItem::parent("Add to drive queue on…", machines));
        }
        items.push(ContextMenuItem::action(
            "drive-queue-add-after",
            "Add to drive queue after…",
        ));
        items
    }

    /// Per-row menu: Resume / Move up / Move down / Unblock / View in
    /// Pipeline / View on Board / Remove.
    /// End-of-queue moves are DISABLED with a reason rather than silently
    /// no-op'ing (#1598's `disabled_because` precedent).
    pub(crate) fn context_menu_items_for_drive_queue_row(
        &self,
        repo_name: &str,
        issue_number: i64,
        state: &str,
        position: i64,
        queue_len: usize,
        held: bool,
    ) -> Vec<ContextMenuItem> {
        let mut items = Vec::new();
        // #2375: the SAME "Run proposed fix" submenu the Pipeline row menu
        // offers for an open escalation (`dialogs.rs`'s `escalation_for`
        // branch) — the queue panel is arguably the more natural place to
        // decide a stuck row from, so reaching this action must not require
        // a context-switch to Pipeline to find the same row. Built through
        // the shared `escalation.rs` helper so the two panels can never
        // drift into rendering different children/ordering for one
        // (repo, issue). A row with no open escalation is unaffected — the
        // lookup returns `None` and this block is skipped entirely.
        if let Some(esc) = self.escalation_for(repo_name, issue_number.max(0) as u64) {
            items.push(Self::run_proposed_fix_menu_item(esc));
            items.push(ContextMenuItem::separator());
        }
        // #1757: first among the state-changing actions below (the escalation
        // block above, when present, still leads the whole menu — a driver
        // stuck on merge is a bigger deal than a deploy gate), and only on a
        // row that is actually holding the queue. When a gate has fired this
        // is the only action that changes anything — everything below it
        // just rearranges work that cannot start — so it must not be buried
        // under three moves.
        if held {
            items.push(
                ContextMenuItem::action("drive-queue-resume", "Resume (release deploy gate)")
                    .with_shortcut("r"),
            );
            items.push(ContextMenuItem::separator());
        }
        let mut up = ContextMenuItem::action("drive-queue-move-up", "Move up").with_shortcut("K");
        if position <= 0 {
            up = up.disabled_because("already first");
        }
        items.push(up);
        let mut down =
            ContextMenuItem::action("drive-queue-move-down", "Move down").with_shortcut("J");
        if queue_len == 0 || position as usize >= queue_len - 1 {
            down = down.disabled_because("already last");
        }
        items.push(down);
        // Only a `blocked` row has anything to unblock — offering it on a
        // waiting row would promise a state change that can't happen.
        if state == QUEUE_STATE_BLOCKED {
            items.push(
                ContextMenuItem::action("drive-queue-unblock", "Unblock").with_shortcut("u"),
            );
        }
        items.push(ContextMenuItem::separator());
        // #2016: navigate to this row's issue elsewhere in the app. Placed
        // after the state-changing actions and behind their own separator so
        // navigation never becomes the default-selected item on a row whose
        // primary action is Resume (#1757) — and before Remove, so the
        // destructive action stays last.
        //
        // "View in Pipeline" is disabled with a reason exactly when the jump
        // would not land — `pipeline_jump_target` is the SAME query
        // `jump_to_pipeline` uses to perform the jump (the #1598 regression:
        // an enabled entry that then fails to land is worse than a disabled
        // one).
        let jump_result = self.pipeline_jump_target(repo_name, issue_number.max(0) as u64);
        let mut view_in_pipeline =
            ContextMenuItem::action("drive-queue-view-in-pipeline", "View in Pipeline");
        if let Err(reason) = jump_result {
            view_in_pipeline = view_in_pipeline.disabled_because(reason.menu_hint());
        }
        items.push(view_in_pipeline);
        // "View on Board" is always enabled — a queue row always carries an
        // issue number, and `select_issue`'s existing no-op-if-not-loaded
        // behaviour is acceptable here (no new toast).
        items.push(ContextMenuItem::action(
            "drive-queue-view-on-board",
            "View on Board",
        ));
        items.push(ContextMenuItem::separator());
        items.push(
            ContextMenuItem::action("drive-queue-remove", "Remove from queue").with_shortcut("x"),
        );
        items
    }

    // ── write path (`coord drive-queue …`) ───────────────────────────────

    /// `coord drive-queue add <repo> <issue> [--machine M] [--after K]…`.
    ///
    /// Optimistically appends to `data.drive_queue` so the status-bar count
    /// and the Pipeline menu's Add⇄Remove swap update on the next paint
    /// rather than on the next board poll (the `dispatch_dismiss_escalation`
    /// precedent). The next `/board` refresh overwrites it with the truth,
    /// including a rejected add simply vanishing again.
    pub(crate) fn dispatch_drive_queue_add(
        &mut self,
        repo: &str,
        issue: u64,
        machine: Option<&str>,
        after: &[String],
    ) {
        let issue_str = issue.to_string();
        let mut args: Vec<String> = vec![
            "drive-queue".to_string(),
            "add".to_string(),
            repo.to_string(),
            issue_str,
        ];
        if let Some(m) = machine.filter(|m| !m.is_empty()) {
            args.push("--machine".to_string());
            args.push(m.to_string());
        }
        for a in after.iter().filter(|a| !a.is_empty()) {
            args.push("--after".to_string());
            args.push(a.clone());
        }
        let arg_refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        self.command_runner.spawn_queued(&arg_refs);

        if !self.drive_queue_contains(repo, issue) {
            let position = self
                .data
                .drive_queue
                .iter()
                .map(|e| e.position)
                .max()
                .map(|p| p + 1)
                .unwrap_or(0);
            self.data.drive_queue.push(BoardDriveQueueEntry {
                repo_name: repo.to_string(),
                issue_number: issue as i64,
                position,
                machine: machine.filter(|m| !m.is_empty()).map(str::to_string),
                after: after.to_vec(),
                state: QUEUE_STATE_WAITING.to_string(),
                ..BoardDriveQueueEntry::default()
            });
        }
        let pinned = match machine.filter(|m| !m.is_empty()) {
            Some(m) => format!(" on {m}"),
            None => String::new(),
        };
        let after_note = if after.is_empty() {
            String::new()
        } else {
            format!(" after {}", after.join(","))
        };
        self.push_toast(
            "Drive queue",
            &format!("queuing {repo} #{issue}{pinned}{after_note}…"),
            ToastSeverity::Info,
        );
    }

    /// `coord drive-queue remove <repo> <issue>`, with the same optimistic
    /// local removal `dispatch_dismiss_escalation` does.
    pub(crate) fn dispatch_drive_queue_remove(&mut self, repo: &str, issue: i64) {
        let issue_str = issue.to_string();
        self.command_runner
            .spawn_queued(&["drive-queue", "remove", repo, &issue_str]);
        self.data
            .drive_queue
            .retain(|e| !(e.repo_name == repo && e.issue_number == issue));
        // #1868: clamping the Queue panel's own selection after a removal is
        // `queue_remove_selected`'s job (it indexes a different, filtered row
        // set) — the drive-queue overlay this used to also clamp for is gone.
        self.push_toast(
            "Drive queue",
            &format!("removed {repo} #{issue} from the queue"),
            ToastSeverity::Info,
        );
    }

    /// `coord drive-queue move <repo> <issue> --to <position>`. Clamped
    /// client-side into `[0, len-1]` so the menu never sends a position the
    /// CLI would have to clamp for us (it does clamp, but a toast that says
    /// "→ 4" when the queue has 3 rows is a lie).
    pub(crate) fn dispatch_drive_queue_move(&mut self, repo: &str, issue: i64, to: i64) {
        let len = self.data.drive_queue.len() as i64;
        let to = to.clamp(0, (len - 1).max(0));
        let issue_str = issue.to_string();
        let to_str = to.to_string();
        self.command_runner
            .spawn_queued(&["drive-queue", "move", repo, &issue_str, "--to", &to_str]);
        self.push_toast(
            "Drive queue",
            &format!("moving {repo} #{issue} to position {to}…"),
            ToastSeverity::Info,
        );
    }

    /// "Unblock" — clear a `blocked` row back to `waiting`.
    ///
    /// There is deliberately **no** `coord drive-queue reset` (DQ-1 keeps run
    /// state out of the operator's write surface), so remove + re-add IS the
    /// reset — a fresh row is `waiting` with `attempts=0` and no `after`.
    /// This mirrors `_requeue_command`'s recipe in
    /// `coord/commands/drive_queue.py` exactly, except that the two halves
    /// are queued as separate `coord` invocations (FIFO, so ordered) rather
    /// than joined by a shell `&&` — `CommandRunner` spawns argv, not a
    /// shell.
    ///
    /// Dropping `after` is intentional and is the *point*: an unsatisfiable
    /// pre-req is one of the two things that blocks a row, and re-adding
    /// with the same `after` would re-block it immediately. The machine pin
    /// is preserved because that is an operator intent, not run state.
    pub(crate) fn dispatch_drive_queue_unblock(&mut self, repo: &str, issue: i64) {
        let machine = self
            .data
            .drive_queue
            .iter()
            .find(|e| e.repo_name == repo && e.issue_number == issue)
            .and_then(|e| e.machine.clone())
            .filter(|m| !m.is_empty());
        let issue_str = issue.to_string();
        self.command_runner
            .spawn_queued(&["drive-queue", "remove", repo, &issue_str]);
        let mut add: Vec<String> = vec![
            "drive-queue".to_string(),
            "add".to_string(),
            repo.to_string(),
            issue_str,
        ];
        if let Some(m) = &machine {
            add.push("--machine".to_string());
            add.push(m.clone());
        }
        let add_refs: Vec<&str> = add.iter().map(|s| s.as_str()).collect();
        self.command_runner.spawn_queued(&add_refs);
        // Optimistic: the row goes back to `waiting` with its counters
        // cleared, which is exactly what the re-add produces.
        for e in self.data.drive_queue.iter_mut() {
            if e.repo_name == repo && e.issue_number == issue {
                e.state = QUEUE_STATE_WAITING.to_string();
                e.attempts = 0;
                e.deferrals = 0;
                e.last_reason = String::new();
                e.after.clear();
            }
        }
        self.push_toast(
            "Drive queue",
            &format!("unblocking {repo} #{issue} (remove + re-add)…"),
            ToastSeverity::Info,
        );
    }

    /// `coord drive-queue resume <repo> <issue>` — release a fired deploy
    /// gate (#1757).
    ///
    /// The entry is NAMED rather than relying on the CLI's bare-`resume`
    /// "release whatever is held": the operator clicked a specific row, and a
    /// command that silently acts on a different one is the kind of surprise
    /// that ends with a deploy gate released before the deploy.
    ///
    /// Optimistic, the `dispatch_drive_queue_unblock` precedent: the row's
    /// `hold_state` goes `released` locally so the status bar drops out of
    /// HELD on the next paint rather than on the next board poll. The next
    /// `/board` refresh overwrites it with the truth, including a rejected
    /// resume simply reverting.
    pub(crate) fn dispatch_drive_queue_resume(&mut self, repo: &str, issue: i64) {
        let issue_str = issue.to_string();
        self.command_runner
            .spawn_queued(&["drive-queue", "resume", repo, &issue_str]);
        for e in self.data.drive_queue.iter_mut() {
            if e.repo_name == repo && e.issue_number == issue {
                e.hold_state = "released".to_string();
                e.hold_probes = 0;
            }
        }
        self.push_toast(
            "Drive queue",
            &format!("releasing the deploy gate on {repo} #{issue}…"),
            ToastSeverity::Info,
        );
    }

    /// Open the "Add to drive queue after…" prompt for (repo, issue).
    pub(crate) fn open_drive_queue_after_input(&mut self, repo: &str, issue: u64) {
        self.pending_drive_queue_after = Some(PendingDriveQueueAfter {
            repo_name: repo.to_string(),
            issue_number: issue,
            buf: String::new(),
        });
    }

    /// Submit the "after…" prompt: split the buffer on commas/whitespace,
    /// strip a leading `#`, and pass each spec through as its own `--after`.
    ///
    /// Deliberately does NOT validate the specs here — `coord drive-queue
    /// add` already refuses a self-edge or a cycle *before* writing anything
    /// (`validate_enqueue`), and duplicating that rule in Rust would be a
    /// second source of truth that can drift. A `REPO#N` spec is passed
    /// through untouched so a cross-repo pre-req still works.
    pub(crate) fn submit_drive_queue_after_input(&mut self, input: PendingDriveQueueAfter) {
        let after: Vec<String> = input
            .buf
            .split(|c: char| c == ',' || c.is_whitespace())
            .map(|s| s.trim().trim_start_matches('#').to_string())
            .filter(|s| !s.is_empty())
            .collect();
        if after.is_empty() {
            self.push_toast(
                "Drive queue",
                "no pre-req issues given — nothing queued.",
                ToastSeverity::Warning,
            );
            return;
        }
        self.dispatch_drive_queue_add(&input.repo_name, input.issue_number, None, &after);
    }

    // ── #1866 (Q-1): the Queue panel ─────────────────────────────────────
    //
    // A live `DataTable` grid over `data.drive_queue`. Everything below is
    // *presentation and routing*: the entries, the predicates, the colours
    // and — crucially — the write seam are all the ones already defined
    // above in this module (originally shared with the drive-queue overlay;
    // #1868 retired that surface, leaving the panel as the one place they
    // are consumed).
    //
    // **No fetch.** `/board` already carries `drive_queue` and the existing
    // `start_data_load` poll refreshes it every `settings.refresh_cadence`.
    // Unlike Reports (#1741) and Audit (#1039) there is deliberately no
    // view-gated fetch block in `settings_ui.rs::run_periodic_work` and no
    // `spawn_*` for this panel — that would be a second source of truth for
    // data already in memory.

    /// The Queue grid's columns. Index order **is** the `DataTable` column
    /// order and the index `queue_sort` refers to, so this table and the
    /// `QUEUE_COL_*` constants below must move together.
    ///
    /// Shape taken from `drive-queue-status` (`coord/reports.py`) minus what
    /// a live *pending* view has no use for. `Reason` carries by far the
    /// heaviest weight on purpose: on a stalled entry `last_reason` is the
    /// whole story, and a column too narrow to read it turns the panel back
    /// into "go and run the CLI".
    pub(crate) const QUEUE_COLUMNS: &'static [(&'static str, f32, ColumnAlign)] = &[
        ("#", 0.5, ColumnAlign::Right),
        ("Issue", 1.6, ColumnAlign::Left),
        ("Title", 3.0, ColumnAlign::Left),
        ("State", 1.0, ColumnAlign::Left),
        ("Machine", 1.2, ColumnAlign::Left),
        ("#Work", 0.7, ColumnAlign::Right),
        ("#Smoke", 0.8, ColumnAlign::Right),
        ("#Review", 0.9, ColumnAlign::Right),
        ("After", 1.4, ColumnAlign::Left),
        ("Hold", 0.9, ColumnAlign::Left),
        ("Reason", 4.0, ColumnAlign::Left),
    ];

    /// #2043: the Queue grid never squeezes its columns below this many
    /// characters; past it the grid scrolls horizontally instead
    /// (`render_queue_panel`'s `min_total_width`). In CHARACTERS —
    /// `min_total_width` itself is surface-native (px on GTK, cells on
    /// TUI, per `quadraui/src/primitives/data_table.rs:106-114`), so a bare
    /// `Some(120.0)` would mean 120 pixels (~20 characters) on GTK. It is
    /// multiplied by `Backend::char_width()` at the one call site that
    /// needs it (`render_queue_panel`) rather than promoted into a
    /// backend-independent length type in quadraui — an API change for two
    /// downstream consumers, for one multiplication. If a second consumer
    /// ever needs the same conversion, that's the moment to promote it.
    const QUEUE_MIN_WIDTH_CHARS: f32 = 120.0;

    /// Index of the `#` (position) column — sorted numerically.
    pub(crate) const QUEUE_COL_POSITION: usize = 0;
    /// Index of the `#Work` column — sorted numerically. #2524: replaced
    /// the single `Tries` column (`e.attempts`, the daemon's own
    /// crash/relaunch counter — orthogonal to leg-dispatch counts; see
    /// `queue_assignment_count`'s doc comment) with three per-stage
    /// dispatch counts.
    pub(crate) const QUEUE_COL_WORK: usize = 5;
    /// Index of the `#Smoke` column — sorted numerically.
    pub(crate) const QUEUE_COL_SMOKE: usize = 6;
    /// Index of the `#Review` column — sorted numerically.
    pub(crate) const QUEUE_COL_REVIEW: usize = 7;

    /// `DataTable` columns from [`Self::QUEUE_COLUMNS`].
    fn queue_columns() -> Vec<Column> {
        Self::QUEUE_COLUMNS
            .iter()
            .map(|(title, weight, align)| Column {
                title: (*title).to_string(),
                width: ColumnWidth::Flex(*weight),
                align: *align,
            })
            .collect()
    }

    /// The rows the Queue panel renders: every **non-terminal** entry, in
    /// `queue_sort` order (the queue's own run order when unsorted).
    ///
    /// `done` is the only state excluded — with one exception: a row whose
    /// deploy gate has fired (`is_holding`) is kept even though it is `done`
    /// by construction (`_resolve_holds` in `coord/drive_queue.py` only fires
    /// a gate the tick its entry reconciles to `done`; see `is_holding`'s own
    /// doc comment for the same rule applied to `summarize_drive_queue`).
    /// Dropping it here would make "r resume" — #1868's acceptance bar for
    /// this panel replacing the drive-queue overlay — unreachable: the exact
    /// row `queue_resume_selected` needs to act on would never appear to be
    /// selected. `blocked` — and any state a newer daemon invents, `failed`
    /// included — is non-terminal *to the operator*: those are precisely the
    /// entries that have stopped and need a human, and a live view that
    /// hides one is the same defect #1855 fixes in the report's summary
    /// line.
    pub(crate) fn queue_rows(&self) -> Vec<QueueRow> {
        let mut rows: Vec<QueueRow> = self
            .drive_queue_entries()
            .into_iter()
            .filter(|e| is_pending(e) || is_holding(e))
            .map(|e| self.queue_row(e))
            .collect();
        if let Some((col, dir)) = self.queue_sort {
            // Stable, so ties keep the queue's own run order — the
            // meaningful secondary key for every column here.
            rows.sort_by(|a, b| queue_compare_rows(a, b, col, dir));
        }
        rows
    }

    /// Project one wire entry into its rendered cells plus the raw values
    /// the grid sorts, reorders and builds a row menu from.
    fn queue_row(&self, e: &BoardDriveQueueEntry) -> QueueRow {
        let or_dash = |s: String| {
            if s.is_empty() {
                QUEUE_EMPTY_CELL.to_string()
            } else {
                s
            }
        };
        let work_count = self.queue_assignment_count(&e.repo_name, e.issue_number, "work");
        let smoke_count = self.queue_assignment_count(&e.repo_name, e.issue_number, "smoke");
        let review_count = self.queue_assignment_count(&e.repo_name, e.issue_number, "review");
        QueueRow {
            repo_name: e.repo_name.clone(),
            issue_number: e.issue_number,
            position: e.position,
            work_count,
            smoke_count,
            review_count,
            state: e.state.clone(),
            held: is_holding(e),
            cells: vec![
                e.position.to_string(),
                alias_queue_key(&e.key()),
                or_dash(self.queue_issue_title(&e.repo_name, e.issue_number)),
                or_dash(queue_state_cell(e)),
                or_dash(self.queue_live_machine(e).unwrap_or_default()),
                work_count.to_string(),
                smoke_count.to_string(),
                review_count.to_string(),
                or_dash(
                    e.after
                        .iter()
                        .map(|a| alias_queue_key(a))
                        .collect::<Vec<_>>()
                        .join(", "),
                ),
                queue_hold_cell(e),
                or_dash(queue_reason_cell(e)),
            ],
        }
    }

    /// Count of this entry's own dispatched legs whose `assignment_type`
    /// equals `atype` — `"work"` (the original dispatch plus any `[fix-N]`
    /// bounces, still `type="work"`, `coord/auto_loop.py:1144`/`:1199`),
    /// `"smoke"`, or `"review"`. Every leg dispatched for an issue shares
    /// its `(repo, issue_number)` key regardless of stage, so this is a
    /// plain filter over `data.assignments` — no new wire field (#2524).
    ///
    /// An assignment with no `assignment_type` on the wire is `"work"`
    /// (`.unwrap_or("work")`, the same default used throughout
    /// `app/sessions.rs`/`app/dialogs.rs`).
    ///
    /// Deliberately NOT `e.attempts` — that's the drive queue's own
    /// crash/relaunch counter (incremented only when a launched session
    /// *dies* and gets requeued, `coord/drive_queue.py:2609`), orthogonal
    /// to how many legs actually ran. A healthy entry that cleanly runs
    /// work → smoke → review → merge sits at `attempts == 0` throughout.
    fn queue_assignment_count(&self, repo: &str, issue_number: i64, atype: &str) -> i64 {
        let Ok(issue_number) = u64::try_from(issue_number) else {
            return 0;
        };
        self.data
            .assignments
            .iter()
            .filter(|a| a.repo == repo && a.issue_number == issue_number)
            .filter(|a| a.assignment_type.as_deref().unwrap_or("work") == atype)
            .count() as i64
    }

    /// The machine actually running this entry's current leg, so `Machine`
    /// reads live instead of blank on the rows an operator most wants to
    /// glance at (#2524). Same "prefer the running assignment" lookup as
    /// `open_watch_for_selected_issue` (`app/mod.rs:4528`) and the Stop
    /// picker (`app/mod.rs:4907`) — Board/Pipeline precedent, not a new
    /// pattern.
    ///
    /// Falls back to the operator's own `--machine` pin (`e.machine`) when
    /// nothing is currently running — a `waiting`/`blocked` row may still
    /// be usefully pinned — then to `None` (the caller renders the dash).
    fn queue_live_machine(&self, e: &BoardDriveQueueEntry) -> Option<String> {
        let issue_number = u64::try_from(e.issue_number).ok();
        self.data
            .assignments
            .iter()
            .find(|a| {
                Some(a.issue_number) == issue_number
                    && a.repo == e.repo_name
                    && a.status == "running"
            })
            .map(|a| a.machine.clone())
            .or_else(|| e.machine.clone())
    }

    /// Issue title for a queued entry, from whatever this client already
    /// holds: the Board's own issue cache, then the Pipeline roster, then
    /// the assignment list.
    ///
    /// Empty when none of them knows it — `drive_queue` is a raw table dump
    /// and carries no title, and adding a fetch for one would be exactly the
    /// second source of truth this panel exists to avoid. The caller renders
    /// an em dash for that case rather than a blank, so "we don't know" and
    /// "the column failed to paint" don't look identical.
    fn queue_issue_title(&self, repo: &str, issue: i64) -> String {
        let Ok(n) = u64::try_from(issue) else {
            return String::new();
        };
        for (r, groups) in &self.board_issues_cache {
            if r != repo {
                continue;
            }
            if let Some(g) = groups
                .iter()
                .find(|g| g.issue_number == n && !g.issue_title.is_empty())
            {
                return g.issue_title.clone();
            }
        }
        if let Some(p) = self.pipeline_issues.iter().find(|p| {
            p.number == n && p.coord_repo.as_deref() == Some(repo) && !p.title.is_empty()
        }) {
            return p.title.clone();
        }
        self.data
            .assignments
            .iter()
            .find(|a| a.repo == repo && a.issue_number == n && !a.issue_title.is_empty())
            .map(|a| a.issue_title.clone())
            .unwrap_or_default()
    }

    /// `DataTable` body rows, coloured by wire `state`.
    fn queue_data_rows(rows: &[QueueRow]) -> Vec<DataRow> {
        rows.iter()
            .map(|r| {
                // Straight from the overlay's own palette so two surfaces
                // onto one queue can never disagree about what red means.
                // An unrecognised state renders neutral rather than silently
                // green (#1485) — but it IS rendered.
                let (fg, _bg) = dq_state_colors(&r.state);
                DataRow {
                    cells: r
                        .cells
                        .iter()
                        .map(|c| StyledText {
                            spans: vec![StyledSpan::with_fg(c.clone(), fg)],
                        })
                        .collect(),
                    decoration: Decoration::Normal,
                }
            })
            .collect()
    }

    /// Sidebar for the Queue panel: the same aggregate reading
    /// [`summarize_drive_queue`] gives the status bar, spelled out one count
    /// per row. Read-only — every verb lives on the grid.
    pub(crate) fn queue_sidebar(&self) -> ListView {
        let s = summarize_drive_queue(&self.data.drive_queue);
        let pending = self.queue_rows().len();
        let mut items = vec![activity_item(
            &format!(
                "  {pending} pending entr{}",
                if pending == 1 { "y" } else { "ies" }
            ),
            Color::rgb(160, 160, 160),
        )];
        items.push(activity_item(
            &format!("  {} running", s.running),
            dq_state_colors(QUEUE_STATE_RUNNING).0,
        ));
        items.push(activity_item(
            &format!("  {} waiting ({} eligible)", s.waiting, s.eligible),
            dq_state_colors(QUEUE_STATE_WAITING).0,
        ));
        if s.blocked > 0 {
            items.push(activity_item(
                &format!("  {} blocked", s.blocked),
                dq_state_colors(QUEUE_STATE_BLOCKED).0,
            ));
        }
        // #2186: the two counts are kept on separate lines rather than
        // folded into one "N held" figure — an operator scanning this
        // sidebar needs to tell "one entry is waiting on its own gate" from
        // "the whole queue has stopped" at a glance, the same distinction
        // `render_plan`'s `[scope=fleet]` tag draws in `coord drive-queue
        // plan`'s CLI output.
        let entry_held = s.held.saturating_sub(s.fleet_held);
        if entry_held > 0 {
            items.push(activity_item(
                &format!("  {entry_held} held (deploy gate, {HOLD_SCOPE_ENTRY}-scoped)"),
                Color::rgb(255, 210, 100),
            ));
        }
        if s.fleet_held > 0 {
            items.push(activity_item(
                &format!(
                    "  {} held (deploy gate, fleet-wide — nothing will launch)",
                    s.fleet_held
                ),
                Color::rgb(255, 210, 100),
            ));
        }
        ListView {
            id: WidgetId::new("queue-sidebar"),
            title: Some(StyledText::plain(" QUEUE ")),
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

    /// #2017: minimum rows the grid may be squeezed to during a splitter
    /// drag — one header row plus two data rows, so it never shrinks to
    /// something that reads as "gone" the way a bare header would. The
    /// detail pane's own floor (`QUEUE_MIN_DETAIL_ROWS`) predates this and
    /// is the precedent this mirrors on the other side of the split.
    const QUEUE_MIN_GRID_ROWS: f32 = 3.0;

    /// #1867's original floor, unchanged: below 7 rows the detail pane
    /// reads as decorative rather than usable (no fixed header the way
    /// Audit's key/value list has, so it needs more headroom than the grid
    /// does).
    const QUEUE_MIN_DETAIL_ROWS: f32 = 7.0;

    /// #2017: clamp a candidate detail-pane height into
    /// `[detail floor, avail - grid floor]`, squeezing both floors evenly
    /// instead of letting one win outright when `avail` is too short to fit
    /// both (acceptance test 3: "both panes still render" even on a tiny
    /// terminal).
    ///
    /// Single source of truth for `render_queue_panel`'s paint (fed
    /// `avail * queue_split_frac`, the persisted intent) and
    /// `queue_update_split_drag`'s live recompute (fed the cursor's raw
    /// position) — so the two can never resolve the same drag to two
    /// different heights. Mirrors `drag_divider`'s both-sides clamp
    /// (`reports.rs::REPORTS_MIN_COLUMN_WIDTH`) applied to rows instead of
    /// columns.
    fn queue_clamp_detail_h(avail: f32, desired_detail_h: f32, lh: f32) -> f32 {
        let min_detail_h = (lh * Self::QUEUE_MIN_DETAIL_ROWS).min(avail).max(0.0);
        let min_grid_h = (lh * Self::QUEUE_MIN_GRID_ROWS).min(avail).max(0.0);
        let (min_detail_h, min_grid_h) = if min_detail_h + min_grid_h > avail {
            let half = (avail / 2.0).max(0.0);
            (half, half)
        } else {
            (min_detail_h, min_grid_h)
        };
        let max_detail_h = (avail - min_grid_h).max(min_detail_h);
        desired_detail_h.clamp(min_detail_h, max_detail_h)
    }

    /// #2017: the draggable separator between the grid and the detail pane
    /// — a single highlighted row. `ListItem` carries no per-row background
    /// override of its own, so this leans on the rasteriser's
    /// selected-row highlight (`has_focus: true` + `selected_idx: 0`) to
    /// paint a full-width bar rather than plain text sitting on the
    /// ordinary background — the boundary needs to read as a grabbable
    /// widget, not an implicit gap between two others.
    fn queue_separator_list() -> ListView {
        ListView {
            id: WidgetId::new("queue-splitter"),
            title: None,
            items: vec![ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        " ⋮⋮⋮ drag to resize ⋮⋮⋮ ",
                        Color::rgb(230, 230, 255),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            }],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: true,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Render the Queue grid, draggable separator, and detail pane into
    /// `rect`.
    pub(crate) fn render_queue_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        // Cleared FIRST and unconditionally. Both early returns below paint
        // no table (or split) at all, and a stale cache would let a later
        // click route a header/row/separator/scrollbar hit against geometry
        // that is no longer on screen.
        *self.queue_table_layout.borrow_mut() = None;
        self.queue_separator_rect.set(None);
        *self.queue_detail_scrollbar.borrow_mut() = None;

        let rows = self.queue_rows();
        if rows.is_empty() {
            // An empty grid renders as a bare header row, which reads like a
            // broken fetch. Say which of the two empties this actually is.
            // Nothing to select either, so no split/detail pane below it.
            let total = self.data.drive_queue.len();
            let message = if total == 0 {
                "  Drive queue is empty — nothing waiting or running.".to_string()
            } else {
                format!(
                    "  Nothing pending — all {total} queued entr{} have finished \
                     (done entries are not shown here).",
                    if total == 1 { "y" } else { "ies" },
                )
            };
            backend.draw_list(rect, &plain_list("queue-empty", &message, 0));
            return;
        }

        // #2017: the split is now operator-draggable (`queue_split_frac`,
        // persisted for the session) rather than the #1867 hardcoded 40%,
        // with a one-row separator carved out between the two panes so the
        // boundary is a real, grabbable widget instead of an implicit gap.
        // `avail` is what the split fraction actually divides — the panel
        // height minus that separator row.
        let sep_h = lh.max(1.0).min(rect.height);
        let avail = (rect.height - sep_h).max(0.0);
        let detail_h = Self::queue_clamp_detail_h(avail, avail * self.queue_split_frac, lh);
        let list_h = (avail - detail_h).max(0.0);
        let list_rect = Rect::new(rect.x, rect.y, rect.width, list_h);
        let sep_rect = Rect::new(rect.x, rect.y + list_h, rect.width, sep_h);
        let detail_rect = Rect::new(
            rect.x,
            rect.y + list_h + sep_h,
            rect.width,
            (rect.height - list_h - sep_h).max(0.0),
        );

        let mut table = DataTable {
            id: WidgetId::new("queue-grid"),
            columns: Self::queue_columns(),
            rows: Self::queue_data_rows(&rows),
            selected_idx: Some(self.queue_sel.min(rows.len() - 1)),
            scroll_offset: self.queue_scroll,
            // The ▲/▼ header indicator is drawn by the primitive itself —
            // the app only says which column and which way.
            sort: self.queue_sort,
            has_focus: true,
            show_scrollbar: true,
            // #2043: below `QUEUE_MIN_WIDTH_CHARS` the grid stops squeezing
            // and scrolls horizontally instead — `Reason` (`Flex(4.0)`) was
            // routinely truncated to uselessness before this floor existed.
            // `min_total_width` is surface-native (px on GTK, cells on TUI —
            // see `QUEUE_MIN_WIDTH_CHARS`'s own doc comment), hence the
            // `char_width()` multiply here rather than in the constant.
            min_total_width: Some(Self::QUEUE_MIN_WIDTH_CHARS * backend.char_width().max(1.0)),
            // #2043: was pinned at 0.0 — `DataTableLayout::hit_test` had no
            // concept of `h_scroll`, so a non-zero value shifted the painted
            // headers out from under the hit-test and routed a sort click to
            // the wrong column (the reasoning is written out in full at
            // `reports.rs::render_reports_result`, which still pins its own
            // table at 0.0 for the same reason). quadraui#550 fixed
            // `hit_test` itself to be `h_scroll`-aware, so this grid — the
            // first table in this crate to actually drive the field — can
            // now use it safely. `queue_h_scroll` only moves once the grid
            // has dropped below the floor above and started scrolling; see
            // its own doc comment.
            //
            // Placeholder here — clamped and overwritten just below, BEFORE
            // `draw_data_table` runs, so this frame is never painted with a
            // stale offset (see the clamp's own comment for why "clamp
            // after paint" isn't good enough).
            h_scroll: 0.0,
            // No column-resize drag on this table yet (#1853 covers that for
            // the Reports result table).
            column_overrides: Vec::new(),
            footer: None,
        };
        // #2043 fix: a resize (or anything else that shrinks `content_width`
        // back to/under the viewport — the min-width floor is the only way
        // that happens today, but nothing here assumes that) must not let a
        // stale `queue_h_scroll` from a previous, narrower paint go on
        // shifting this frame's columns left. `render_queue_panel` takes
        // `&self`, so a bare field couldn't self-correct even if it wanted
        // to — that's why `queue_h_scroll` is a `Cell` (see its own doc
        // comment).
        //
        // `data_table_layout` is a pure layout computation (no paint), so
        // probing it here to learn `content_width`/`viewport_width` before
        // the real `draw_data_table` call below costs nothing visually and
        // is what lets the clamp land BEFORE this frame paints, not after —
        // clamping post-paint would still ship one fully mis-rendered frame
        // per resize, exactly the bug this fixes.
        let probe = backend.data_table_layout(list_rect, &table);
        let visible_w = (probe.viewport_width - probe.scrollbar_width).max(1.0);
        let clamped_h_scroll =
            Self::queue_clamp_h_scroll(self.queue_h_scroll.get(), probe.content_width, visible_w);
        self.queue_h_scroll.set(clamped_h_scroll);
        table.h_scroll = clamped_h_scroll;
        let layout = backend.draw_data_table(list_rect, &table, None);
        // Cached WITH the rect it was painted into — the Reports pattern,
        // not Audit's. This table does not necessarily start at the main
        // panel's origin, so a bare `pos - main_b` would mis-hit-test.
        *self.queue_table_layout.borrow_mut() = Some((list_rect, layout));

        backend.draw_list(sep_rect, &Self::queue_separator_list());
        self.queue_separator_rect.set(Some(sep_rect));

        // #1867: stash the pane's painted width so `queue_issue_body_list`
        // word-wraps to the live viewport — the `last_issue_panel_cols`
        // pattern (`render.rs:175`), just under its own `Cell`.
        self.last_queue_detail_cols.set(detail_rect.width as usize);
        // #1867 fix: also stash the pane's OWN visible-row count (its rect
        // is only a fraction of `rect`, never the whole panel) so
        // `mouse_main_scroll`'s clamp doesn't use the full-panel `visible`
        // and saturate to 0 for any body shorter than the whole panel. See
        // `last_queue_detail_visible_rows`'s doc comment for the bug this
        // avoids. #2017: still correct after a splitter drag, since this is
        // read from the just-computed `detail_rect`, never a stale one.
        self.last_queue_detail_visible_rows
            .set(content_visible_rows(detail_rect, lh).max(1));
        // Built once here and reused for both the paint and the item-count
        // cache, so `mouse_main_scroll` can read `last_queue_detail_item_count`
        // instead of re-running this (a markdown re-render that also drains
        // the pending-fetch channel) on every wheel notch.
        //
        // #2017: `show_v_scrollbar: true` (set inside `queue_issue_body_list`
        // via `issue_body_list`'s new parameter) — this is the one place
        // among the three `issue_body_list` callers (Board, Pipeline, Queue)
        // where it's on, so only this panel gains the track.
        let detail_list = self.queue_issue_body_list();
        self.last_queue_detail_item_count
            .set(detail_list.items.len());
        // #2017: the detail pane's scrollbar geometry, from the SAME
        // `ListView` and rect the paint below uses — `Backend::
        // list_vscrollbar` is the single source of truth the rasteriser
        // consumes too, so a click can never hit-test against geometry the
        // paint disagrees with. `None` when the body fits (no track), which
        // `queue_detail_scrollbar_hit` / `queue_apply_detail_vscroll` both
        // already treat as "nothing to do".
        *self.queue_detail_scrollbar.borrow_mut() =
            backend.list_vscrollbar(detail_rect, &detail_list);
        backend.draw_list(detail_rect, &detail_list);
    }

    /// #1867 (Q-2): the selected Queue row's issue body, rendered through the
    /// shared `issue_body_list` helper (never a second renderer). Same
    /// layered lookup as `board_issue_body_list` (`render.rs:1754`) — this
    /// exists so a fetch stays a last resort, not the common path:
    ///
    /// 1. Synced row in `data.open_issues` — fast path, no I/O. `/board`
    ///    ships the full `issues` table, so this is the overwhelming case.
    /// 2. In-memory `fetched_issues_cache`, populated by a prior background
    ///    `gh issue view` for this session (shared with Board/Pipeline — one
    ///    fetch of an issue serves every panel that shows it).
    /// 3. An in-flight background fetch — show a "Fetching…" placeholder and
    ///    let the next render pick up the result.
    /// 4. No data yet — spawn `gh issue view` in the background (at most
    ///    once per issue: step 3's in-flight check makes every subsequent
    ///    frame a no-op fetch-wise until it resolves) and show a placeholder.
    pub(crate) fn queue_issue_body_list(&self) -> ListView {
        // #1867: use the pane width stashed at draw time for word-wrapping.
        let wrap_width = self.last_queue_detail_cols.get().max(40);
        let Some(row) = self.queue_selected_row() else {
            return issue_body_list(
                None,
                self.queue_detail_scroll,
                "queue-issue-body",
                wrap_width,
                true,
                None,
            );
        };
        let repo = row.repo_name.clone();
        let Ok(number) = u64::try_from(row.issue_number) else {
            return issue_body_list(
                None,
                self.queue_detail_scroll,
                "queue-issue-body",
                wrap_width,
                true,
                None,
            );
        };
        let key = (repo.clone(), number);
        // The queue's own title lookup (`queue_row`'s Title cell, minus the
        // em-dash-for-unknown substitution) — used below whenever the body
        // itself is a placeholder rather than the real fetched/synced issue.
        let title = self.queue_issue_title(&repo, row.issue_number);

        // 1. Synced row.
        if let Some(oi) = self
            .data
            .open_issues
            .iter()
            .find(|oi| oi.repo_name == repo && oi.number == number)
        {
            return issue_body_list(
                Some((oi.number, oi.title.as_str(), oi.body.as_str(), &oi.labels[..])),
                self.queue_detail_scroll,
                "queue-issue-body",
                wrap_width,
                true,
                Some(&repo),
            );
        }

        // 2. Drain any completed background fetch into the cache so step 3
        // picks it up. Keyed identically to (and shared with) Board/Pipeline
        // — a fetch either of them already made satisfies this panel too.
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
                    // the receiver so the cold path below re-spawns next
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
                self.queue_detail_scroll,
                "queue-issue-body",
                wrap_width,
                true,
                Some(&repo),
            );
        }

        // 4. Spawn if no fetch is already running — at most one background
        // fetch per issue, never one per frame.
        if !self.pending_issue_fetches.borrow().contains_key(&key) {
            let slug = self
                .data
                .pipeline_repos
                .iter()
                .find(|(local, _)| local == &repo)
                .map(|(_, slug)| slug.clone());
            if let Some(slug) = slug {
                let rx = spawn_issue_fetch(slug, repo.clone(), number);
                self.pending_issue_fetches
                    .borrow_mut()
                    .insert(key.clone(), rx);
            } else {
                // No slug → can't fetch. Show the title we have with a hint.
                return issue_body_list(
                    Some((
                        number,
                        title.as_str(),
                        "(no GitHub slug for this repo — add it to coordinator.yml.repos[].github)",
                        &[][..],
                    )),
                    self.queue_detail_scroll,
                    "queue-issue-body",
                    wrap_width,
                    true,
                    Some(&repo),
                );
            }
        }

        // Placeholder while the fetch is in flight.
        issue_body_list(
            Some((
                number,
                title.as_str(),
                "(fetching body via `gh issue view`…)",
                &[][..],
            )),
            self.queue_detail_scroll,
            "queue-issue-body",
            wrap_width,
            true,
            Some(&repo),
        )
    }

    /// Rows actually visible inside the grid's own viewport, from the
    /// last-painted layout. `None` before anything has been painted.
    pub(crate) fn queue_table_visible_rows(&self) -> Option<usize> {
        self.queue_table_layout
            .borrow()
            .as_ref()
            .map(|(_, layout)| layout.visible_rows)
    }

    /// Hit-test a click against the last-painted grid, or `None` when no
    /// grid is on screen.
    pub(crate) fn queue_table_hit(&self, pos: Point) -> Option<DataTableHit> {
        let n = self.queue_rows().len();
        let cache = self.queue_table_layout.borrow();
        let (rect, layout) = cache.as_ref()?;
        Some(layout.hit_test(pos.x - rect.x, pos.y - rect.y, self.queue_scroll, n))
    }

    /// Did a click land on either of the grid's scrollbar tracks?
    ///
    /// Checked BEFORE [`Self::queue_table_hit`] by every caller:
    /// `DataTableLayout::hit_test` has no concept of either scrollbar strip
    /// it reserves space for (the #1094 gap, and its horizontal twin), so
    /// without this a click on either thumb mis-resolves to whichever
    /// header/row sits under it.
    ///
    /// #2043: extended from a plain vertical-only `bool` to
    /// `Option<QueueScrollAxis>` the same way `audit_scrollbar_hit` reports
    /// both of Audit's tracks — same geometry the TUI rasteriser paints
    /// them at (`quadraui::tui::data_table::draw_data_table`: the vertical
    /// track occupies the rightmost `scrollbar_width` columns below the
    /// header row; the horizontal track occupies the bottom
    /// `h_scrollbar_height` row(s), left of the vertical track). Vertical
    /// takes priority in the bottom-right corner, matching Audit's own
    /// priority order.
    pub(crate) fn queue_scrollbar_hit(&self, pos: Point) -> Option<QueueScrollAxis> {
        let cache = self.queue_table_layout.borrow();
        let (rect, layout) = cache.as_ref()?;
        let x = pos.x - rect.x;
        let y = pos.y - rect.y;
        if x < 0.0 || y < 0.0 || x >= layout.viewport_width || y >= layout.viewport_height {
            return None;
        }
        if layout.scrollbar_width > 0.0
            && x >= layout.viewport_width - layout.scrollbar_width
            && y >= layout.header_height
        {
            return Some(QueueScrollAxis::Vertical);
        }
        if layout.h_scrollbar_height > 0.0 && y >= layout.viewport_height - layout.h_scrollbar_height
        {
            return Some(QueueScrollAxis::Horizontal);
        }
        None
    }

    /// Jump `queue_scroll` to the row implied by a click along the vertical
    /// scrollbar track. Mirrors `reports_apply_vscroll`.
    pub(crate) fn queue_apply_vscroll(&mut self, pos: Point) -> bool {
        let n = self.queue_rows().len();
        if n == 0 {
            return false;
        }
        let (track_y0, track_h, visible_rows) = {
            let cache = self.queue_table_layout.borrow();
            let Some((rect, layout)) = cache.as_ref() else {
                return false;
            };
            let track_y0 = rect.y + layout.header_height;
            let track_h = (layout.viewport_height
                - layout.header_height
                - layout.h_scrollbar_height)
                .max(1.0);
            (track_y0, track_h, layout.visible_rows.max(1))
        };
        let max_scroll = n.saturating_sub(visible_rows);
        self.queue_scroll = if max_scroll == 0 {
            0
        } else {
            let frac = ((pos.y - track_y0) / track_h).clamp(0.0, 1.0);
            (frac * max_scroll as f32).round() as usize
        };
        true
    }

    /// #2043: jump `queue_h_scroll` to the column offset implied by a
    /// click/drag along the grid's horizontal scrollbar track. Mirrors
    /// `queue_apply_vscroll` for the other axis (and `audit_apply_hscroll`
    /// for the other table) — over the cached `queue_table_layout` instead
    /// of a `main_b` handle, the same `rect`-carrying-cache reason
    /// `queue_table_hit` documents.
    pub(crate) fn queue_apply_hscroll(&mut self, pos: Point) -> bool {
        let (track_x0, track_w, content_w, visible_w) = {
            let cache = self.queue_table_layout.borrow();
            let Some((rect, layout)) = cache.as_ref() else {
                return false;
            };
            let visible_w = (layout.viewport_width - layout.scrollbar_width).max(1.0);
            (rect.x, visible_w, layout.content_width, visible_w)
        };
        let max_scroll = (content_w - visible_w).max(0.0);
        self.queue_h_scroll.set(if max_scroll <= 0.0 {
            0.0
        } else {
            let frac = ((pos.x - track_x0) / track_w).clamp(0.0, 1.0);
            frac * max_scroll
        });
        true
    }

    /// #2043: clamp `h_scroll` to `[0, content_width - visible_width]` —
    /// the single formula `queue_apply_hscroll`, `queue_apply_hwheel`, and
    /// `render_queue_panel`'s per-frame resize self-heal all defer to, so
    /// click, wheel, and a bare repaint can never disagree on where "all
    /// the way right" is (or, once the grid stops scrolling at all, that
    /// it's `0.0`).
    fn queue_clamp_h_scroll(h_scroll: f32, content_width: f32, visible_width: f32) -> f32 {
        let max_scroll = (content_width - visible_width).max(0.0);
        h_scroll.clamp(0.0, max_scroll)
    }

    /// #2043: step `queue_h_scroll` by one wheel notch — the horizontal
    /// twin of `mouse_main_scroll`'s vertical Queue-grid wheel handling,
    /// driven by `delta.x` (positive = scroll right, matching
    /// `ScrollDelta`'s `ScrollRight` convention — see
    /// `quadraui::tui::events`). Clamped to `[0, content_width -
    /// visible_width]`, the same bound `queue_apply_hscroll` computes for
    /// click-to-position, so wheel and track-click can never disagree on
    /// where "all the way right" is. A no-op (returns `false`) whenever
    /// the grid isn't actually scrolling horizontally — `max_scroll` is
    /// `<= 0.0` below the `QUEUE_MIN_WIDTH_CHARS` floor.
    pub(crate) fn queue_apply_hwheel(&mut self, delta_x: f32) -> bool {
        let (content_w, visible_w) = {
            let cache = self.queue_table_layout.borrow();
            let Some((_, layout)) = cache.as_ref() else {
                return false;
            };
            (
                layout.content_width,
                (layout.viewport_width - layout.scrollbar_width).max(1.0),
            )
        };
        let max_scroll = (content_w - visible_w).max(0.0);
        if max_scroll <= 0.0 {
            self.queue_h_scroll.set(0.0);
            return false;
        }
        const STEP: f32 = 4.0;
        let next = self.queue_h_scroll.get() + delta_x.signum() * STEP;
        self.queue_h_scroll.set(next.clamp(0.0, max_scroll));
        true
    }

    /// #2017: did a click/hover land on the draggable separator between the
    /// grid and the detail pane? Checked ahead of `queue_table_hit` by every
    /// caller, the same #1094-precedent reason `queue_scrollbar_hit` is: the
    /// separator is a real widget with its own painted rect
    /// (`queue_separator_rect`), not something `DataTableLayout::hit_test`
    /// or `ListViewLayout::hit_test` knows anything about.
    pub(crate) fn queue_separator_hit(&self, pos: Point) -> bool {
        let Some(r) = self.queue_separator_rect.get() else {
            return false;
        };
        pos.x >= r.x && pos.x < r.x + r.width && pos.y >= r.y && pos.y < r.y + r.height
    }

    /// #2017: continue an in-progress splitter drag (started by a
    /// `MouseDown` on the separator — see `queue_separator_hit` and
    /// `mouse_main_click`), recomputing `queue_split_frac` from the
    /// cursor's live `pos` so the separator tracks it. No-op (returns
    /// `false`) when no drag is in progress or `main_b` is degenerate.
    ///
    /// `main_b` is the panel rect `render_queue_panel` painted into as
    /// `rect` — Queue has no `panel_toolbar()` (see `sidebar.rs`), so
    /// `ctx.main_bounds()` at drag time is exactly that rect, the same
    /// assumption `mouse_main_scroll`'s Queue arm already relies on.
    ///
    /// Runs the SAME floor clamp (`queue_clamp_detail_h`) the paint uses,
    /// fed the cursor's raw position instead of the persisted fraction —
    /// this is what keeps a drag that hits a floor from "fighting" the next
    /// render rather than settling there.
    pub(crate) fn queue_update_split_drag(&mut self, pos: Point, main_b: Rect, lh: f32) -> bool {
        if !self.queue_split_drag || main_b.height <= 0.0 {
            return false;
        }
        let sep_h = lh.max(1.0).min(main_b.height);
        let avail = (main_b.height - sep_h).max(0.0);
        if avail <= 0.0 {
            return false;
        }
        // The cursor's y becomes the separator's top edge; everything below
        // it, minus the separator's own row, is the detail pane.
        let desired_detail_h = (main_b.y + main_b.height) - (pos.y + sep_h);
        let detail_h = Self::queue_clamp_detail_h(avail, desired_detail_h, lh);
        self.queue_split_frac = detail_h / avail;
        true
    }

    /// #2017: did a click land on the detail pane's vertical scrollbar
    /// track? Same #1094-precedent shape as `queue_scrollbar_hit`, over the
    /// `Scrollbar` geometry `render_queue_panel` cached from `Backend::
    /// list_vscrollbar` rather than a `DataTableLayout`.
    pub(crate) fn queue_detail_scrollbar_hit(&self, pos: Point) -> bool {
        let cache = self.queue_detail_scrollbar.borrow();
        let Some(sb) = cache.as_ref() else {
            return false;
        };
        let t = sb.track;
        pos.x >= t.x && pos.x < t.x + t.width && pos.y >= t.y && pos.y < t.y + t.height
    }

    /// #2017: jump `queue_detail_scroll` to the position implied by a
    /// click/drag along the detail pane's vertical scrollbar track. Mirrors
    /// `queue_apply_vscroll` / `reports_apply_vscroll`, over the cached
    /// `Scrollbar.track` instead of a `DataTableLayout`.
    ///
    /// Reads `last_queue_detail_item_count` / `last_queue_detail_visible_rows`
    /// — the same live, per-frame-refreshed pane geometry `mouse_main_scroll`'s
    /// wheel arm already uses (the #1867/#1910 lesson this issue's
    /// acceptance test 8 guards): both are re-stashed on every
    /// `render_queue_panel` call, so a splitter drag that just changed the
    /// pane's height is reflected immediately, never a stale value.
    pub(crate) fn queue_apply_detail_vscroll(&mut self, pos: Point) -> bool {
        let items = self.last_queue_detail_item_count.get();
        let visible = self.last_queue_detail_visible_rows.get().max(1);
        let max_scroll = items.saturating_sub(visible);
        let track = {
            let cache = self.queue_detail_scrollbar.borrow();
            match cache.as_ref() {
                Some(sb) => sb.track,
                None => return false,
            }
        };
        self.queue_detail_scroll = if max_scroll == 0 || track.height <= 0.0 {
            0
        } else {
            let frac = ((pos.y - track.y) / track.height).clamp(0.0, 1.0);
            (frac * max_scroll as f32).round() as usize
        };
        true
    }

    /// Click a grid column header: `None → ▲ → ▼ → None` for that column,
    /// switching straight to ▲ when a different column is clicked.
    ///
    /// Client-side sort is correct here for the same reason it is in Reports
    /// and wrong in Audit: the entry set on screen is the complete queue,
    /// not one server-paginated page of it. The third click clearing the
    /// sort is what makes the queue's own run order reachable again — and
    /// run order is the answer to a different question from any column sort.
    pub(crate) fn queue_sort_by_column(&mut self, col: usize) -> bool {
        if col >= Self::QUEUE_COLUMNS.len() {
            return false;
        }
        self.queue_sort = match self.queue_sort {
            Some((c, SortDirection::Ascending)) if c == col => {
                Some((col, SortDirection::Descending))
            }
            Some((c, SortDirection::Descending)) if c == col => None,
            _ => Some((col, SortDirection::Ascending)),
        };
        // The row that was under the viewport means something different now.
        self.queue_scroll = 0;
        // #1867: a re-sort can put an entirely different entry at the same
        // index `queue_sel` already points to — the detail pane's scroll
        // offset must not survive that.
        self.queue_detail_scroll = 0;
        true
    }

    /// The selected grid row, or `None` when the grid is empty.
    ///
    /// Clamped rather than returning `None` for an out-of-range
    /// `queue_sel`, and clamped **the same way the renderer clamps
    /// `selected_idx`** — the queue shrinks under this panel on every poll
    /// (an entry finishes and drops out), and a highlight sitting on the
    /// tail row while `J`/`x` silently no-op'd against a dangling index is
    /// the exact divergence between "the row I can see selected" and "the
    /// row the verb acts on" that makes a destructive menu dangerous.
    pub(crate) fn queue_selected_row(&self) -> Option<QueueRow> {
        let rows = self.queue_rows();
        let idx = self.queue_sel.min(rows.len().checked_sub(1)?);
        rows.into_iter().nth(idx)
    }

    /// #1867 (Q-2): set `queue_sel`, resetting `queue_detail_scroll` to 0
    /// whenever the selection actually changes.
    ///
    /// The single choke point every selection-mutating path (keyboard
    /// nav, Home/End, a grid click) routes through, so a short issue's body
    /// can never render at a long issue's scroll offset — which would read
    /// as "no body" rather than "this one is just short". Comparing indices
    /// rather than resetting unconditionally means re-landing on the
    /// already-selected row (e.g. a no-op `queue_move_selection` at either
    /// end of the grid) leaves an in-progress read of the body undisturbed.
    pub(crate) fn queue_set_sel(&mut self, idx: usize) {
        if idx != self.queue_sel {
            self.queue_detail_scroll = 0;
        }
        self.queue_sel = idx;
    }

    /// Move the grid selection by `delta` rows, clamped. No wraparound — a
    /// queue is short and ordered, and wrapping past the tail hides how
    /// close to the end you are.
    pub(crate) fn queue_move_selection(&mut self, delta: isize) {
        let len = self.queue_rows().len();
        if len == 0 {
            self.queue_set_sel(0);
            return;
        }
        let next = (self.queue_sel as isize + delta).clamp(0, len as isize - 1);
        self.queue_set_sel(next as usize);
    }

    /// Keep `queue_sel` inside the grid's own painted viewport.
    ///
    /// `fallback_visible` is only used for the one frame before anything has
    /// been painted — after that the real `DataTableLayout::visible_rows` is
    /// authoritative (the #1910 lesson: the panel's row count overcounts what
    /// the table itself shows).
    pub(crate) fn fix_queue_scroll(&mut self, fallback_visible: usize) {
        let visible = self
            .queue_table_visible_rows()
            .unwrap_or(fallback_visible)
            .max(1);
        if self.queue_sel < self.queue_scroll {
            self.queue_scroll = self.queue_sel;
        } else if self.queue_sel >= self.queue_scroll + visible {
            self.queue_scroll = self.queue_sel + 1 - visible;
        }
    }

    /// The right-click target for the selected grid row.
    ///
    /// Builds the *same* [`ContextMenuTarget::DriveQueueRow`] the overlay
    /// does, so the menu (`context_menu_items_for_drive_queue_row`,
    /// including its `disabled_because` reasons for end-of-queue moves) and
    /// every dispatcher behind it are shared verbatim.
    pub(crate) fn queue_context_target(&self) -> Option<ContextMenuTarget> {
        let queue_len = self.data.drive_queue.len();
        let r = self.queue_selected_row()?;
        Some(ContextMenuTarget::DriveQueueRow {
            repo_name: r.repo_name,
            issue_number: r.issue_number,
            state: r.state,
            position: r.position,
            queue_len,
            held: r.held,
        })
    }

    /// `J`/`K`: move the selected entry `delta` slots down/up the queue and
    /// follow it with the selection.
    ///
    /// Goes through the exact write seam the overlay and the row menu use —
    /// `coord drive-queue move` via `spawn_queued`, corrected by the next
    /// `/board` poll. Deliberately NOT `POST /drive-queue`: the CLI path
    /// re-validates (cycles, clamping, dense renumbering) and the HTTP path
    /// would bypass all of it.
    ///
    /// The move is expressed in `position` space over the WHOLE queue,
    /// `done` rows included — the position the CLI acts on is the one in the
    /// table, not the one in this filtered view.
    pub(crate) fn queue_selected_move(&mut self, delta: i64) {
        let Some(r) = self.queue_selected_row() else {
            return;
        };
        let len = self.data.drive_queue.len() as i64;
        let to = (r.position + delta).clamp(0, (len - 1).max(0));
        if to == r.position {
            return;
        }
        self.dispatch_drive_queue_move(&r.repo_name, r.issue_number, to);
        // Follow the entry so repeated `J` walks it down the queue instead
        // of walking the cursor off it. Only meaningful in the panel's
        // natural (run) order — under a column sort the row's new slot isn't
        // knowable until the next poll, so the selection stays put.
        if self.queue_sort.is_none() {
            self.queue_move_selection(delta as isize);
        }
    }

    /// `x` — remove the selected entry from the queue.
    pub(crate) fn queue_remove_selected(&mut self) {
        let Some(r) = self.queue_selected_row() else {
            return;
        };
        self.dispatch_drive_queue_remove(&r.repo_name, r.issue_number);
        // `dispatch_drive_queue_remove` no longer clamps a selection itself
        // (#1868 — that was the retired overlay's bookkeeping); this panel
        // clamps its own `queue_sel` below, into ITS (filtered) row set.
        //
        // #1867: `queue_move_selection(0)` is a delta-0 no-op on the index
        // itself, so `queue_set_sel` won't see a change to reset on — but
        // the entry now sitting at that index is a different one (the
        // removed row's neighbour shifted up), so the detail pane's scroll
        // offset is stale regardless. Reset explicitly.
        self.queue_detail_scroll = 0;
        self.queue_move_selection(0);
    }

    /// `u` — unblock the selected entry. Refuses on anything but a `blocked`
    /// row rather than promising a state change that cannot happen, exactly
    /// as the overlay's `u` does.
    pub(crate) fn queue_unblock_selected(&mut self) {
        match self
            .queue_selected_row()
            .filter(|r| r.state == QUEUE_STATE_BLOCKED)
        {
            Some(r) => self.dispatch_drive_queue_unblock(&r.repo_name, r.issue_number),
            None => self.push_toast(
                "Drive queue",
                "only a blocked entry can be unblocked.",
                ToastSeverity::Warning,
            ),
        }
    }

    /// `r` — release the selected entry's fired deploy gate (#1757). Refuses
    /// on any other row rather than falling back to "resume whatever is
    /// held" — see [`Self::dispatch_drive_queue_resume`].
    pub(crate) fn queue_resume_selected(&mut self) {
        match self.queue_selected_row().filter(|r| r.held) {
            Some(r) => self.dispatch_drive_queue_resume(&r.repo_name, r.issue_number),
            None => self.push_toast(
                "Drive queue",
                "only an entry whose deploy gate has fired can be resumed.",
                ToastSeverity::Warning,
            ),
        }
    }
}

/// #1866: one Queue-panel row — the rendered cell strings plus the raw
/// values the grid needs for sorting, reordering and the row menu.
///
/// Built once per frame by [`CoordApp::queue_rows`] and consumed by the
/// renderer AND by every hit-test / keyboard path, so what an operator sees
/// and what `J` moves can never come from two different passes over
/// `data.drive_queue`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct QueueRow {
    pub(crate) repo_name: String,
    pub(crate) issue_number: i64,
    /// Dense 0-based slot in the WHOLE queue (`done` rows included) — the
    /// number `coord drive-queue move --to` speaks in.
    pub(crate) position: i64,
    /// Count of this entry's `type="work"` assignments (original dispatch
    /// + `[fix-N]` bounces). #2524: replaced `attempts` (the daemon's
    /// crash/relaunch counter, not a leg-dispatch count — see
    /// `CoordApp::queue_assignment_count`'s doc comment).
    pub(crate) work_count: i64,
    /// Count of this entry's `type="smoke"` assignments.
    pub(crate) smoke_count: i64,
    /// Count of this entry's `type="review"` assignments.
    pub(crate) review_count: i64,
    /// Wire `state`, verbatim.
    pub(crate) state: String,
    /// `hold_state == "fired"` — this entry's deploy gate is holding the
    /// queue shut.
    pub(crate) held: bool,
    /// Rendered cells, one per [`CoordApp::QUEUE_COLUMNS`] entry.
    pub(crate) cells: Vec<String>,
}

/// What an absent value renders as. A blank cell and a failed paint look
/// identical; an em dash says "there is nothing here" out loud.
pub(crate) const QUEUE_EMPTY_CELL: &str = "—";

/// #2042: short alias for a repo name, used ONLY in the Queue grid's `Issue`
/// and `After` cells — the full name repeats on every row there and crowds
/// out `Reason`, the column that actually explains a stuck entry. Rule:
/// split on `-`, take each word's first character, uppercase, concatenate.
/// `claude-coordinator` → `CC`, `coord-portal` → `CP`, `vimcode` → `V`.
///
/// A repeated or trailing `-` yields empty words, which `filter_map` simply
/// drops — no panic, no empty-letter placeholder.
///
/// **Known scope limitation, not a bug to fix here:** this has no collision
/// handling. Two repo names that start the same way (`coord-portal` and a
/// hypothetical future `coord-proxy`) would both alias to `CP`. There is no
/// collision in the current fleet (`CC`, `CP`, `V`, `Q`), so this function
/// does not disambiguate — see #2042.
pub(crate) fn repo_alias(repo_name: &str) -> String {
    repo_name
        .split('-')
        .filter_map(|word| word.chars().next())
        .flat_map(|c| c.to_uppercase())
        .collect()
}

/// Render a `"repo#N"` queue key (`BoardDriveQueueEntry::key()`, or one of
/// `after`'s entries — same format) with the repo replaced by its
/// [`repo_alias`]. The one place both the `Issue` and `After` cells go
/// through, so the derivation isn't inlined twice (#2042).
pub(crate) fn alias_queue_key(key: &str) -> String {
    match key.split_once('#') {
        Some((repo, rest)) => format!("{}#{}", repo_alias(repo), rest),
        None => key.to_string(),
    }
}

/// #2589: appended to the `State` cell of a `blocked` row whose cause
/// [`is_pre_dispatch_block_reason`] matches — the row is `blocked` the same
/// as any other by [`QueueRow::state`] (sorting, colouring, and the `u`
/// unblock action all key off the WIRE state, unchanged), but the rendered
/// text says out loud what an operator otherwise has to read the `Reason`
/// column and do the classification themselves to learn: this row will
/// never self-heal, unlike an ordinary `blocked` row #2230's merge-gate
/// sweep (or #2362's `after=`-graph sweep) might still clear on its own.
pub(crate) const QUEUE_NEEDS_OPERATOR_SUFFIX: &str = " [NEEDS OPERATOR]";

/// The `State` cell for one entry — the wire `state` verbatim, plus
/// [`QUEUE_NEEDS_OPERATOR_SUFFIX`] when this `blocked` row's own cause has
/// no merge gate #2230's tick sweep could ever clear (#2589). Deliberately
/// only touches the CELL TEXT, never [`QueueRow::state`] itself — sorting,
/// row colour ([`dq_state_colors`]) and the row menu's `u`/unblock action
/// all still key off the plain wire state, exactly as before this column
/// existed.
fn queue_state_cell(e: &BoardDriveQueueEntry) -> String {
    if e.state == QUEUE_STATE_BLOCKED && is_pre_dispatch_block_reason(&e.last_reason) {
        format!("{}{}", e.state, QUEUE_NEEDS_OPERATOR_SUFFIX)
    } else {
        e.state.clone()
    }
}

/// The `Hold` cell for one entry — #1757's DEPLOY GATE, read verbatim off
/// `hold_state` (never re-derived, same posture as `state`).
fn queue_hold_cell(e: &BoardDriveQueueEntry) -> String {
    if is_holding(e) {
        // Upper-case because this row's own launch is dead: whatever this
        // entry's gate holds, IT will not launch until a human releases it.
        // #2186: that is no longer "the queue is dead" in general — only a
        // `[fleet]`-tagged row stops everything else too — so the scope is
        // spelled out here exactly the way `render_plan`'s `scope_tag` spells
        // it out in `coord/drive_queue.py`: silent for the default (`entry`),
        // named for the deliberately-opted-into case.
        return if stops_fleet(e) {
            "FIRED [fleet]".to_string()
        } else {
            "FIRED".to_string()
        };
    }
    if e.hold_after == 0 {
        return QUEUE_EMPTY_CELL.to_string();
    }
    if e.hold_state.is_empty() {
        "gate".to_string()
    } else {
        e.hold_state.clone()
    }
}

/// The `Reason` cell for one entry — `last_reason` age-stamped with
/// `reason_at` (#2133).
///
/// `last_reason` is a snapshot the tick (or a guard) wrote the instant it
/// observed the condition, never re-validated afterwards. Rendering it bare
/// lets an hours-old, no-longer-true observation read as a live diagnosis —
/// the #2104 incident this closes: `checks_failed` was still shown roughly
/// three hours after the named checks had gone green, while the actual
/// blocker (a later `request-changes` review) was nowhere in the panel.
/// Appending the age doesn't make the reason current, but it stops it from
/// being silently mistaken for current. No suffix when `reason_at` is
/// `None` — a row predating #2133's migration, or built without going
/// through `update_drive_queue_entry` — rather than guessing an age it
/// doesn't have.
fn queue_reason_cell(e: &BoardDriveQueueEntry) -> String {
    if e.last_reason.is_empty() {
        return String::new();
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let age = format_age(e.reason_at, now);
    if age.is_empty() {
        e.last_reason.clone()
    } else {
        format!("{} ({})", e.last_reason, age)
    }
}

/// Columns [`queue_compare_rows`] sorts as numbers rather than strings —
/// the #1762 "`10` sorts before `2`" fix (comparing the *rendered* strings
/// for `#`/`Tries` would sort `10` before `2`). #2524 grew this past the
/// original `#`/`Tries` pair when `Tries` became three per-stage counts; a
/// `contains()` check here is what keeps that from meaning three more
/// one-off match arms in `queue_compare_rows` itself.
const QUEUE_NUMERIC_COLS: [usize; 4] = [
    CoordApp::QUEUE_COL_POSITION,
    CoordApp::QUEUE_COL_WORK,
    CoordApp::QUEUE_COL_SMOKE,
    CoordApp::QUEUE_COL_REVIEW,
];

/// The typed value behind a numerically-sorted column (one of
/// [`QUEUE_NUMERIC_COLS`]) — mirrors of the `position`/`work_count`/
/// `smoke_count`/`review_count` fields on [`QueueRow`].
fn queue_numeric_sort_key(row: &QueueRow, col: usize) -> i64 {
    match col {
        CoordApp::QUEUE_COL_POSITION => row.position,
        CoordApp::QUEUE_COL_WORK => row.work_count,
        CoordApp::QUEUE_COL_SMOKE => row.smoke_count,
        CoordApp::QUEUE_COL_REVIEW => row.review_count,
        _ => 0,
    }
}

/// Order two Queue rows by `col` — numeric for [`QUEUE_NUMERIC_COLS`],
/// case-insensitive text otherwise.
fn queue_compare_rows(
    a: &QueueRow,
    b: &QueueRow,
    col: usize,
    dir: SortDirection,
) -> std::cmp::Ordering {
    let base = if QUEUE_NUMERIC_COLS.contains(&col) {
        queue_numeric_sort_key(a, col).cmp(&queue_numeric_sort_key(b, col))
    } else {
        let empty = String::new();
        let av = a.cells.get(col).unwrap_or(&empty).to_lowercase();
        let bv = b.cells.get(col).unwrap_or(&empty).to_lowercase();
        av.cmp(&bv)
    };
    match dir {
        SortDirection::Ascending => base,
        SortDirection::Descending => base.reverse(),
    }
}

/// Row colour by wire `state`. An unrecognised state renders neutral — never
/// silently green (#1485's "absence must never read as healthy" applied to a
/// state string this build has never heard of).
pub(crate) fn dq_state_colors(state: &str) -> (Color, Color) {
    match state {
        QUEUE_STATE_RUNNING => (Color::rgb(120, 210, 120), Color::rgb(15, 50, 15)),
        QUEUE_STATE_BLOCKED => (Color::rgb(255, 120, 120), Color::rgb(60, 15, 15)),
        QUEUE_STATE_DONE => (Color::rgb(140, 140, 150), Color::rgb(30, 30, 40)),
        QUEUE_STATE_WAITING => (Color::rgb(210, 210, 220), Color::rgb(40, 40, 55)),
        _ => (Color::rgb(210, 210, 220), Color::rgb(40, 40, 55)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::fixtures::{make_assignment_typed, make_test_app};

    fn entry(
        issue: i64,
        position: i64,
        state: &str,
        after: &[&str],
    ) -> BoardDriveQueueEntry {
        BoardDriveQueueEntry {
            repo_name: "myrepo".to_string(),
            issue_number: issue,
            position,
            state: state.to_string(),
            after: after.iter().map(|s| s.to_string()).collect(),
            ..BoardDriveQueueEntry::default()
        }
    }

    fn machine(name: &str) -> Machine {
        Machine {
            name: name.to_string(),
            host: String::new(),
            reachable: true,
            active_count: 0,
            repos: vec!["myrepo".to_string()],
            version: None,
            worktree_bytes: 0,
        }
    }

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

    // ── drive_queue_status_text: the four states ─────────────────────────

    #[test]
    fn status_text_empty_says_empty_not_nothing() {
        assert_eq!(drive_queue_status_text(&[]), "QUEUE: empty");
    }

    #[test]
    fn status_text_normal_counts_running_and_waiting() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_RUNNING, &[]),
            entry(2, 1, QUEUE_STATE_WAITING, &[]),
            entry(3, 2, QUEUE_STATE_WAITING, &[]),
            entry(4, 3, QUEUE_STATE_WAITING, &[]),
        ];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: 1 running · 3 waiting"
        );
        assert_eq!(summarize_drive_queue(&rows).level, DriveQueueLevel::Normal);
    }

    /// Three waiting entries and no eligible one — the epic's alert case.
    /// Each waits on the next, so nothing at the head can start either
    /// (#1 waits on #2 waits on #3 waits on #1: a cycle DQ-1 would refuse to
    /// create, used here purely to make every row ineligible at once).
    #[test]
    fn status_text_stalled_when_nothing_is_eligible() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_WAITING, &["myrepo#2"]),
            entry(2, 1, QUEUE_STATE_WAITING, &["myrepo#3"]),
            entry(3, 2, QUEUE_STATE_WAITING, &["myrepo#1"]),
        ];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: STALLED — 3 waiting, none eligible"
        );
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.level, DriveQueueLevel::Stalled);
        assert_eq!(s.eligible, 0);
    }

    #[test]
    fn stalled_renders_in_warn_colours() {
        let (fg, bg) = DriveQueueLevel::Stalled.colors();
        // Same amber pair `FleetSeverity::Warn` uses — the two adjacent
        // status-bar segments must never disagree about what amber means.
        assert_eq!(fg, Color::rgb(255, 210, 100));
        assert_eq!(bg, Color::rgb(70, 45, 10));
    }

    #[test]
    fn blocked_renders_in_crit_colours() {
        let (fg, bg) = DriveQueueLevel::Blocked.colors();
        assert_eq!(fg, Color::rgb(255, 255, 255));
        assert_eq!(bg, Color::rgb(150, 30, 30));
    }

    /// Blocked outranks stalled: a board that would otherwise read STALLED
    /// must read BLOCKED once any row is blocked.
    #[test]
    fn status_text_blocked_outranks_a_simultaneous_stall() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_BLOCKED, &[]),
            entry(2, 1, QUEUE_STATE_BLOCKED, &[]),
            // Waiting on a pre-req that is itself still in the queue →
            // ineligible, i.e. this board is ALSO stalled.
            entry(3, 2, QUEUE_STATE_WAITING, &["myrepo#1"]),
        ];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: BLOCKED 2 · 1 waiting"
        );
        assert_eq!(summarize_drive_queue(&rows).level, DriveQueueLevel::Blocked);
    }

    /// A queue that is moving is not stalled, even though its tail can't
    /// start yet — that's just a queue.
    #[test]
    fn running_entry_means_waiting_behind_it_is_not_a_stall() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_RUNNING, &[]),
            entry(2, 1, QUEUE_STATE_WAITING, &["myrepo#1"]),
        ];
        assert_eq!(summarize_drive_queue(&rows).level, DriveQueueLevel::Normal);
        assert_eq!(drive_queue_status_text(&rows), "QUEUE: 1 running · 1 waiting");
    }

    /// `done` rows are history — they must not keep the segment shouting.
    #[test]
    fn done_entries_do_not_count_as_pending() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_DONE, &[]),
            entry(2, 1, QUEUE_STATE_DONE, &[]),
        ];
        assert_eq!(drive_queue_status_text(&rows), "QUEUE: empty");
    }

    /// A pre-req that already finished satisfies the `after` edge.
    #[test]
    fn a_done_prereq_makes_the_dependent_entry_eligible() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_DONE, &[]),
            entry(2, 1, QUEUE_STATE_WAITING, &["myrepo#1"]),
        ];
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.eligible, 1);
        assert_eq!(s.level, DriveQueueLevel::Normal);
    }

    /// A pre-req that isn't in the queue at all is treated as satisfied —
    /// it may have landed long before this queue existed. The tick is the
    /// authority; the TUI must not invent a stall it can't substantiate.
    #[test]
    fn a_prereq_absent_from_the_queue_counts_as_satisfied() {
        let rows = vec![entry(2, 0, QUEUE_STATE_WAITING, &["myrepo#999"])];
        assert_eq!(summarize_drive_queue(&rows).eligible, 1);
    }

    /// An unrecognised state from a newer daemon is neither eligible nor a
    /// stall trigger — and never silently folded into a healthy bucket.
    #[test]
    fn unknown_state_neither_triggers_nor_masks_a_stall() {
        let rows = vec![entry(1, 0, "some-future-state", &[])];
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.waiting, 0);
        assert_eq!(s.eligible, 0);
        assert_eq!(s.level, DriveQueueLevel::Empty);
    }

    // ── status-bar segment ───────────────────────────────────────────────

    #[test]
    fn status_bar_segment_is_always_present_even_when_empty() {
        let app = make_test_app(BoardData::default());
        let seg = app.drive_queue_status_bar_segment();
        assert!(
            seg.text.contains("QUEUE: empty"),
            "an empty queue must SAY it's empty: {:?}",
            seg.text
        );
        assert!(seg.action_id.is_none(), "the segment carries no action_id");
        assert!(!seg.bold, "an idle queue must not compete for attention");
    }

    #[test]
    fn status_bar_segment_bolds_only_when_it_is_news() {
        let app = make_test_app(BoardData {
            drive_queue: vec![entry(1, 0, QUEUE_STATE_BLOCKED, &[])],
            ..BoardData::default()
        });
        let seg = app.drive_queue_status_bar_segment();
        assert!(seg.bold, "a blocked queue must be bold");
        assert_eq!(seg.bg, Color::rgb(150, 30, 30));
    }

    // ── menu items ───────────────────────────────────────────────────────

    #[test]
    fn status_bar_menu_offers_drive_queue() {
        let app = make_test_app(BoardData::default());
        let items = app.context_menu_items_for_drive_queue_segment();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].label, "Drive queue…");
        assert_eq!(items[0].action_id.as_deref(), Some("open-drive-queue-detail"));
        assert_eq!(
            items[0].shortcut.as_deref(),
            Some("q"),
            "the accelerator is shown INSIDE the menu, not as a bar letter row"
        );
    }

    #[test]
    fn pipeline_row_menu_offers_add_with_machine_submenu_and_after() {
        let app = make_test_app(BoardData {
            machines: vec![machine("precision"), machine("dellserver")],
            ..BoardData::default()
        });
        let items = app.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), true);
        let labels: Vec<&str> = items.iter().map(|i| i.label.as_str()).collect();
        assert_eq!(
            labels,
            vec![
                "Add to drive queue",
                "Add to drive queue on…",
                "Add to drive queue after…"
            ]
        );
        let submenu = items[1].submenu.as_ref().expect("machine submenu");
        assert_eq!(
            submenu.iter().map(|i| i.label.as_str()).collect::<Vec<_>>(),
            vec!["precision", "dellserver"],
            "the submenu lists the BOARD's machines"
        );
        assert_eq!(
            submenu[0].action_id.as_deref(),
            Some("drive-queue-add-on:precision"),
            "the chosen machine rides in the action id → --machine NAME"
        );
    }

    #[test]
    fn pipeline_row_menu_omits_the_machine_submenu_when_the_board_has_none() {
        let app = make_test_app(BoardData::default());
        let items = app.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), true);
        assert!(
            items.iter().all(|i| i.submenu.is_none()),
            "an empty submenu is a dead end, not an affordance"
        );
    }

    #[test]
    fn pipeline_row_menu_offers_remove_for_an_already_queued_row() {
        let app = make_test_app(BoardData {
            drive_queue: vec![entry(42, 3, QUEUE_STATE_WAITING, &[])],
            ..BoardData::default()
        });
        let items = app.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), true);
        assert_eq!(items.len(), 1, "Add variants are suppressed: {items:?}");
        assert_eq!(items[0].action_id.as_deref(), Some("drive-queue-row-remove"));
        assert!(
            items[0].label.contains("position 3"),
            "the label names the slot it holds: {:?}",
            items[0].label
        );
    }

    #[test]
    fn pipeline_row_menu_is_empty_for_an_unmapped_repo() {
        let app = make_test_app(BoardData::default());
        assert!(app
            .drive_queue_menu_items_for_pipeline_row(Some(42), None, true)
            .is_empty());
        assert!(app
            .drive_queue_menu_items_for_pipeline_row(None, Some("myrepo"), true)
            .is_empty());
    }

    #[test]
    fn in_progress_row_offers_remove_but_never_a_fresh_add() {
        let queued = make_test_app(BoardData {
            drive_queue: vec![entry(42, 0, QUEUE_STATE_WAITING, &[])],
            ..BoardData::default()
        });
        let items = queued.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), false);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].action_id.as_deref(), Some("drive-queue-row-remove"));

        let unqueued = make_test_app(BoardData::default());
        assert!(
            unqueued
                .drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), false)
                .is_empty(),
            "an in-progress row that is NOT queued offers nothing"
        );
    }

    #[test]
    fn done_rows_do_not_count_as_queued_for_the_menu_swap() {
        let app = make_test_app(BoardData {
            drive_queue: vec![entry(42, 0, QUEUE_STATE_DONE, &[])],
            ..BoardData::default()
        });
        assert!(!app.drive_queue_contains("myrepo", 42));
        let items = app.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), true);
        assert_eq!(items[0].action_id.as_deref(), Some("drive-queue-add"));
    }

    #[test]
    fn row_menu_gates_moves_at_the_ends_and_unblock_off_waiting() {
        let app = make_test_app(BoardData::default());
        let first =
            app.context_menu_items_for_drive_queue_row("myrepo", 42, QUEUE_STATE_WAITING, 0, 3, false);
        assert!(first[0].disabled, "'Move up' disabled at position 0");
        assert_eq!(first[0].disabled_reason.as_deref(), Some("already first"));
        assert!(!first[1].disabled, "'Move down' enabled mid-queue");
        assert!(
            !first.iter().any(|i| i.label == "Unblock"),
            "a waiting row has nothing to unblock"
        );

        let last =
            app.context_menu_items_for_drive_queue_row("myrepo", 42, QUEUE_STATE_BLOCKED, 2, 3, false);
        assert!(last[1].disabled, "'Move down' disabled at the tail");
        assert!(last.iter().any(|i| i.label == "Unblock"));
    }

    // ── "Run proposed fix" submenu on an escalated row (#2375) ───────────

    fn escalation(repo: &str, issue_number: u64) -> EscalationEntry {
        EscalationEntry {
            id: 1,
            repo_name: repo.to_string(),
            issue_number: issue_number as i64,
            stage: "merge".to_string(),
            assignment_id: Some("w1".to_string()),
            reason: "merge_status=NEEDS_ATTENTION — no number of retries changes this".to_string(),
            gate_readings:
                "merge_status=NEEDS_ATTENTION | pr_url=https://github.com/acme/myrepo/pull/9"
                    .to_string(),
            proposed_command: "gh pr merge 9 --rebase && coord reconcile-merges".to_string(),
            created_at: Some(1.0),
        }
    }

    #[test]
    fn queue_row_menu_unchanged_when_no_escalation_is_open() {
        let app = make_test_app(BoardData::default());
        let items = app.context_menu_items_for_drive_queue_row(
            "myrepo",
            42,
            QUEUE_STATE_WAITING,
            0,
            3,
            false,
        );
        assert!(
            !items
                .iter()
                .any(|i| i.label.starts_with("Run proposed fix")),
            "a row with no open escalation must render exactly as before #2375: {items:?}"
        );
        assert_eq!(
            items[0].action_id.as_deref(),
            Some("drive-queue-move-up"),
            "no escalation block inserted ahead of the moves"
        );
    }

    #[test]
    fn queue_row_menu_offers_run_proposed_fix_when_escalated() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        let items = app.context_menu_items_for_drive_queue_row(
            "myrepo",
            42,
            QUEUE_STATE_WAITING,
            0,
            3,
            false,
        );
        let parent = items
            .iter()
            .find(|i| i.label.starts_with("Run proposed fix"))
            .expect("'Run proposed fix' item not found in escalated queue row's menu");
        let children = parent
            .submenu
            .as_ref()
            .expect("'Run proposed fix' must be a pull-right submenu, not a flat action");
        assert_eq!(
            children
                .iter()
                .map(|c| c.action_id.as_deref())
                .collect::<Vec<_>>(),
            vec![Some("decide-escalation:0"), Some("decide-escalation:1")],
            "each submenu entry must call `coord decide` with its own option index"
        );
    }

    #[test]
    fn queue_row_menu_matches_pipeline_row_menu_for_the_same_escalation() {
        // #2375 acceptance: seeding one escalation + one drive-queue row for
        // the same (repo, issue) must produce the SAME "Run proposed fix"
        // submenu — same recommended command, same child ordering — from
        // both `context_menu_items_for_drive_queue_row` (this module) and
        // `context_menu_items_for_pipeline_row` (`dialogs.rs`).
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            drive_queue: vec![entry(42, 0, QUEUE_STATE_WAITING, &[])],
            ..BoardData::default()
        });
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];

        let queue_items = app.context_menu_items_for_drive_queue_row(
            "myrepo",
            42,
            QUEUE_STATE_WAITING,
            0,
            1,
            false,
        );
        let pipeline_items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &crate::app::types::PipelineRowLifecycle::New,
            Some("myrepo"),
        );

        let queue_parent = queue_items
            .iter()
            .find(|i| i.label.starts_with("Run proposed fix"))
            .expect("queue row menu missing 'Run proposed fix'");
        let pipeline_parent = pipeline_items
            .iter()
            .find(|i| i.label.starts_with("Run proposed fix"))
            .expect("pipeline row menu missing 'Run proposed fix'");

        assert_eq!(
            queue_parent.label, pipeline_parent.label,
            "both panels must name the same recommended command"
        );
        let queue_children = queue_parent.submenu.as_ref().expect("queue submenu");
        let pipeline_children = pipeline_parent.submenu.as_ref().expect("pipeline submenu");
        assert_eq!(
            queue_children
                .iter()
                .map(|c| (c.action_id.clone(), c.label.clone()))
                .collect::<Vec<_>>(),
            pipeline_children
                .iter()
                .map(|c| (c.action_id.clone(), c.label.clone()))
                .collect::<Vec<_>>(),
            "same action ids, same labels, same order on both panels"
        );
    }

    #[test]
    fn queue_row_decide_escalation_dispatch_resolves_repo_and_issue() {
        // `pipeline_menu_repo_issue` now also resolves a `DriveQueueRow`
        // target (#2375) — without this, the submenu's
        // `decide-escalation:<index>` action id would silently no-op when
        // clicked from the Queue panel.
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        let target = crate::app::types::ContextMenuTarget::DriveQueueRow {
            repo_name: "myrepo".to_string(),
            issue_number: 42,
            state: QUEUE_STATE_WAITING.to_string(),
            position: 0,
            queue_len: 1,
            held: false,
        };
        app.dispatch_context_menu_action("decide-escalation:0", &target);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "decide".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
                "0".to_string(),
            ]],
            "clicking the queue-panel submenu must dispatch `coord decide myrepo 42 0`"
        );
    }

    // ── dispatch: the `coord drive-queue …` argv ─────────────────────────

    #[test]
    fn dispatch_add_spawns_a_bare_add() {
        let mut app = make_test_app(BoardData::default());
        app.dispatch_drive_queue_add("myrepo", 42, None, &[]);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "drive-queue".to_string(),
                "add".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
            ]]
        );
        assert!(
            app.drive_queue_contains("myrepo", 42),
            "optimistically queued so the menu/segment update before the next poll"
        );
    }

    #[test]
    fn dispatch_add_passes_the_chosen_machine_as_a_flag() {
        let mut app = make_test_app(BoardData::default());
        app.dispatch_drive_queue_add("myrepo", 42, Some("dellserver"), &[]);
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec!["drive-queue", "add", "myrepo", "42", "--machine", "dellserver"]
        );
    }

    #[test]
    fn dispatch_add_repeats_after_once_per_prereq() {
        let mut app = make_test_app(BoardData::default());
        app.dispatch_drive_queue_add(
            "myrepo",
            42,
            None,
            &["1753".to_string(), "other#7".to_string()],
        );
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec![
                "drive-queue", "add", "myrepo", "42", "--after", "1753", "--after", "other#7"
            ]
        );
    }

    #[test]
    fn dispatch_remove_spawns_and_removes_optimistically() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(42, 0, QUEUE_STATE_WAITING, &[]),
                entry(43, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        app.dispatch_drive_queue_remove("myrepo", 42);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec!["drive-queue", "remove", "myrepo", "42"]]
        );
        assert_eq!(app.data.drive_queue.len(), 1);
        assert_eq!(app.data.drive_queue[0].issue_number, 43);
    }

    #[test]
    fn dispatch_move_clamps_into_range() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(42, 0, QUEUE_STATE_WAITING, &[]),
                entry(43, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        app.dispatch_drive_queue_move("myrepo", 42, 99);
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec!["drive-queue", "move", "myrepo", "42", "--to", "1"],
            "a position past the tail is clamped, not sent verbatim"
        );
    }

    /// There is deliberately no `coord drive-queue reset` — remove + re-add
    /// IS the reset (DQ-1 keeps run state out of the write surface).
    #[test]
    fn dispatch_unblock_is_remove_then_readd_preserving_the_machine_pin() {
        let mut blocked = entry(42, 0, QUEUE_STATE_BLOCKED, &["myrepo#41"]);
        blocked.machine = Some("dellserver".to_string());
        blocked.deferrals = 4;
        blocked.last_reason = "pre-req never landed".to_string();
        let mut app = make_test_app(BoardData {
            drive_queue: vec![blocked],
            ..BoardData::default()
        });
        app.dispatch_drive_queue_unblock("myrepo", 42);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec!["drive-queue", "remove", "myrepo", "42"]],
            "the remove starts immediately"
        );
        assert_eq!(
            app.command_runner.queue_depth(),
            1,
            "the re-add is FIFO-queued behind it, not joined by a shell `&&` \
             (CommandRunner spawns argv, not a shell)"
        );
        // Draining the first command starts the queued second one — the same
        // thing the app's per-tick `poll()` does.
        app.command_runner.poll().expect("no_spawn resolves synchronously");
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![
                vec!["drive-queue", "remove", "myrepo", "42"],
                vec!["drive-queue", "add", "myrepo", "42", "--machine", "dellserver"],
            ],
            "…and the re-add preserves the machine pin (an operator intent, \
             not run state)"
        );
        let row = &app.data.drive_queue[0];
        assert_eq!(row.state, QUEUE_STATE_WAITING);
        assert_eq!(row.deferrals, 0);
        assert!(row.last_reason.is_empty());
        assert!(
            row.after.is_empty(),
            "the unsatisfiable pre-req is dropped — re-adding it would re-block immediately"
        );
    }

    // ── after… prompt ────────────────────────────────────────────────────

    #[test]
    fn after_input_splits_on_commas_and_whitespace_and_strips_hashes() {
        let mut app = make_test_app(BoardData::default());
        app.submit_drive_queue_after_input(PendingDriveQueueAfter {
            repo_name: "myrepo".to_string(),
            issue_number: 42,
            buf: "#1753, 1754  other#7".to_string(),
        });
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec![
                "drive-queue", "add", "myrepo", "42", "--after", "1753", "--after", "1754",
                "--after", "other#7"
            ]
        );
    }

    #[test]
    fn after_input_with_no_prereqs_queues_nothing() {
        let mut app = make_test_app(BoardData::default());
        app.submit_drive_queue_after_input(PendingDriveQueueAfter {
            repo_name: "myrepo".to_string(),
            issue_number: 42,
            buf: "   ".to_string(),
        });
        assert!(
            app.command_runner.spawned_calls.is_empty(),
            "an empty after-list must not silently become a bare add"
        );
    }

    // ── drive_queue_entries (shared by the Queue panel) ──────────────────

    #[test]
    fn entries_are_returned_in_position_order() {
        let app = make_test_app(BoardData {
            drive_queue: vec![
                entry(9, 2, QUEUE_STATE_WAITING, &[]),
                entry(7, 0, QUEUE_STATE_RUNNING, &[]),
                entry(8, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        assert_eq!(
            app.drive_queue_entries()
                .iter()
                .map(|e| e.issue_number)
                .collect::<Vec<_>>(),
            vec![7, 8, 9]
        );
    }

    #[test]
    fn queue_context_target_carries_the_selected_row() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(1, 0, QUEUE_STATE_WAITING, &[]),
                entry(2, 1, QUEUE_STATE_BLOCKED, &[]),
            ],
            ..BoardData::default()
        });
        app.queue_set_sel(1);
        match app.queue_context_target().expect("a target") {
            ContextMenuTarget::DriveQueueRow {
                repo_name,
                issue_number,
                state,
                position,
                queue_len,
                held,
            } => {
                assert_eq!(repo_name, "myrepo");
                assert!(!held, "an unheld row must not offer Resume");
                assert_eq!(issue_number, 2);
                assert_eq!(state, QUEUE_STATE_BLOCKED);
                assert_eq!(position, 1);
                assert_eq!(queue_len, 2);
            }
            other => panic!("wrong target: {other:?}"),
        }
    }

    #[test]
    fn queue_context_target_is_none_for_an_empty_queue() {
        let app = make_test_app(BoardData::default());
        assert!(app.queue_context_target().is_none());
    }

    // ── TuiDriver black-box (#1755 acceptance) ───────────────────────────

    /// Build a `CoordApp` whose Pipeline view is populated and whose queue is
    /// `rows`, ready to hand to `driver_with_shell`.
    fn driver_app(rows: Vec<BoardDriveQueueEntry>) -> CoordApp {
        let mut app = make_test_app(BoardData {
            drive_queue: rows,
            machines: vec![machine("precision"), machine("dellserver")],
            ..BoardData::default()
        });
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.rebuild_pipeline_sidebar(None);
        app
    }

    /// The status bar renders the `QUEUE:` segment on first paint, in every
    /// one of the four states — the issue's headline acceptance bullets,
    /// asserted through a real render rather than the pure function alone.
    #[test]
    fn tuidriver_status_bar_renders_every_queue_state() {
        use quadraui::tui::testing::driver_with_shell;

        let cases: Vec<(Vec<BoardDriveQueueEntry>, &str)> = vec![
            (Vec::new(), "QUEUE: empty"),
            (
                vec![
                    entry(1, 0, QUEUE_STATE_RUNNING, &[]),
                    entry(2, 1, QUEUE_STATE_WAITING, &[]),
                    entry(3, 2, QUEUE_STATE_WAITING, &[]),
                    entry(4, 3, QUEUE_STATE_WAITING, &[]),
                ],
                "QUEUE: 1 running · 3 waiting",
            ),
            (
                vec![
                    entry(1, 0, QUEUE_STATE_WAITING, &["myrepo#2"]),
                    entry(2, 1, QUEUE_STATE_WAITING, &["myrepo#3"]),
                    entry(3, 2, QUEUE_STATE_WAITING, &["myrepo#1"]),
                ],
                "QUEUE: STALLED — 3 waiting, none eligible",
            ),
            (
                vec![
                    entry(1, 0, QUEUE_STATE_BLOCKED, &[]),
                    entry(2, 1, QUEUE_STATE_BLOCKED, &[]),
                    entry(3, 2, QUEUE_STATE_WAITING, &["myrepo#1"]),
                ],
                "QUEUE: BLOCKED 2 · 1 waiting",
            ),
        ];
        for (rows, expected) in cases {
            let driver = driver_with_shell(driver_app(rows), CoordApp::shell_config(), 200, 40);
            let screen = driver.screen();
            assert!(
                screen.contains(expected),
                "status bar must render {expected:?}:\n{screen}"
            );
        }
    }

    /// "The status bar ALWAYS shows a `QUEUE:` segment" has to hold at a
    /// real terminal width, not just a 200-column test canvas: the bar drops
    /// whole trailing `left` segments once the view hints claim the row, and
    /// appended *after* "FLEET: OK  (coord health for detail)" the QUEUE
    /// segment vanished outright below ~150 columns. Ordered ahead of it,
    /// both survive — and in the common (empty/normal) case fleet health
    /// keeps its full text too. See `status_bar()`'s ordering comment.
    #[test]
    fn tuidriver_queue_segment_survives_a_narrow_terminal() {
        use quadraui::tui::testing::driver_with_shell;

        for width in [140u16, 160, 200] {
            let driver = driver_with_shell(
                driver_app(Vec::new()),
                CoordApp::shell_config(),
                width,
                40,
            );
            let screen = driver.screen();
            assert!(
                screen.contains("QUEUE: empty"),
                "the QUEUE segment must survive a {width}-column terminal:\n{screen}"
            );
            assert!(
                screen.contains("FLEET:"),
                "…without displacing the #1631 fleet-health verdict:\n{screen}"
            );
        }

        // At 120 columns the bar is over-subscribed no matter what: even
        // BEFORE #1755 the fleet segment rendered clipped there ("FLEET: OK
        // (coord"). The rule that must not bend is the newer, actionable
        // one — the QUEUE verdict stays whole, and fleet health loses more
        // of its trailing "(coord health for detail)" CLI hint.
        let driver =
            driver_with_shell(driver_app(Vec::new()), CoordApp::shell_config(), 120, 40);
        let screen = driver.screen();
        assert!(
            screen.contains("QUEUE: empty"),
            "even at 120 columns the QUEUE verdict must be whole:\n{screen}"
        );
    }

    /// #1868 (Q-3): right-click the status bar → "Drive queue…" switches to
    /// the Queue panel — the modal overlay this item used to open is gone,
    /// and the panel is the one queue surface now. Every entry the overlay
    /// would have listed, including WHY a skipped one is waiting, is on the
    /// panel's grid.
    #[test]
    fn tuidriver_status_bar_menu_switches_to_the_queue_panel() {
        use quadraui::tui::testing::driver_with_shell;

        let mut skipped = entry(9, 1, QUEUE_STATE_WAITING, &["myrepo#7"]);
        skipped.last_reason = "pre-req myrepo#7 has not merged".to_string();
        let app = driver_app(vec![entry(7, 0, QUEUE_STATE_RUNNING, &[]), skipped]);
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 200, 44);

        let (x, y) = driver
            .find("QUEUE:")
            .unwrap_or_else(|| panic!("QUEUE segment not on screen:\n{}", driver.screen()));
        driver.dispatch(UiEvent::MouseDown {
            widget: None,
            button: MouseButton::Right,
            position: Point::new(x, y),
            modifiers: Modifiers::default(),
        });

        let menu = driver.screen();
        assert!(
            menu.contains("Drive queue…"),
            "right-clicking the status bar must offer 'Drive queue…':\n{menu}"
        );
        assert!(
            menu.contains("Fleet health…"),
            "…without displacing the fleet-health item that was already there:\n{menu}"
        );

        let (mx, my) = driver
            .find("Drive queue…")
            .unwrap_or_else(|| panic!("'Drive queue…' item not found:\n{menu}"));
        // Same fractional-anchor nudge `escalation.rs`'s TuiDriver test
        // documents — `find` returns the row centre, but the menu hit-tests
        // one item-height below where it visibly renders.
        driver.click(mx, my - 0.1);

        let panel = driver.screen();
        assert!(
            panel.contains("QUEUE"),
            "the click must land on the Queue panel (its sidebar header), \
             not the retired modal overlay:\n{panel}"
        );
        assert!(
            !panel.contains("Drive queue is empty"),
            "that string only ever appeared in the overlay's empty state — \
             its presence here would mean the overlay is still reachable:\n{panel}"
        );
        assert!(
            // #2042: the grid's Issue/After cells render the `M` alias, not
            // the full repo name — `Reason` (checked below) is untouched.
            panel.contains("M#7") && panel.contains("M#9"),
            "both entries must be on the panel's grid:\n{panel}"
        );
        assert!(
            panel.contains("pre-req myrepo#7 has not merged"),
            "…including WHY the waiting one hasn't started, in its Reason column:\n{panel}"
        );
    }

    /// Right-clicking a Pipeline row offers "Add to drive queue"; for a row
    /// already queued it offers "Remove from drive queue" instead.
    #[test]
    fn tuidriver_pipeline_row_menu_offers_add_then_remove() {
        use quadraui::tui::testing::driver_with_shell;

        fn open_row_menu(app: CoordApp) -> String {
            let mut driver = driver_with_shell(app, CoordApp::shell_config(), 200, 44);
            // The lone fixture issue has no milestone, so its row starts
            // collapsed under the "No milestone" bucket (same expansion
            // drive.rs/escalation.rs's TuiDriver tests need).
            let (label_x, label_y) = driver.find("No milestone").unwrap_or_else(|| {
                panic!("'No milestone' bucket header not found:\n{}", driver.screen())
            });
            driver.click((label_x - 2.0).max(0.0), label_y);
            let (x, y) = driver
                .find("#42")
                .unwrap_or_else(|| panic!("Pipeline row '#42' not found:\n{}", driver.screen()));
            driver.dispatch(UiEvent::MouseDown {
                widget: None,
                button: MouseButton::Right,
                position: Point::new(x, y),
                modifiers: Modifiers::default(),
            });
            driver.screen()
        }

        let unqueued = open_row_menu(driver_app(Vec::new()));
        assert!(
            unqueued.contains("Add to drive queue"),
            "an unqueued Pipeline row must offer 'Add to drive queue':\n{unqueued}"
        );
        assert!(
            !unqueued.contains("Remove from drive queue"),
            "…and not the Remove variant:\n{unqueued}"
        );

        let queued = open_row_menu(driver_app(vec![entry(42, 0, QUEUE_STATE_WAITING, &[])]));
        assert!(
            queued.contains("Remove from drive queue"),
            "a queued row must offer 'Remove from drive queue' instead:\n{queued}"
        );
        assert!(
            !queued.contains("Add to drive queue"),
            "…and NOT the Add variants:\n{queued}"
        );
    }

    // ── deploy gates (#1757) ─────────────────────────────────────────────
    //
    // The status-bar state and the overlay's Resume item are both reachable
    // through `driver_with_shell` + `make_test_app`, so per this repo's
    // CLAUDE.md they belong HERE, in-crate, not in a SMOKE_TESTS bullet.

    /// A fired-gate entry, as the wire delivers it: the gate fires when the
    /// entry lands, so the row is `done` AND `hold_state == "fired"` at
    /// once. `scope` is `HOLD_SCOPE_ENTRY` or `HOLD_SCOPE_FLEET` (#2186).
    fn fired_entry(issue: i64, position: i64, reason: &str, scope: &str) -> BoardDriveQueueEntry {
        BoardDriveQueueEntry {
            hold_after: 1,
            hold_reason: reason.to_string(),
            hold_state: HOLD_STATE_FIRED.to_string(),
            hold_scope: scope.to_string(),
            ..entry(issue, position, QUEUE_STATE_DONE, &[])
        }
    }

    /// A FLEET-scoped fired gate — the pre-#2186 "stops the whole queue"
    /// case. Kept under this name deliberately: every test below that was
    /// written before #2186 and calls `held_entry` is specifically
    /// exercising "the queue reads as stopped", which post-#2186 only a
    /// fleet-scoped gate does — see `fired_entry` for the entry-scoped
    /// counterpart the #2186 tests further down use.
    fn held_entry(issue: i64, position: i64, reason: &str) -> BoardDriveQueueEntry {
        fired_entry(issue, position, reason, HOLD_SCOPE_FLEET)
    }

    #[test]
    fn status_text_held_names_the_operators_own_reason() {
        let rows = vec![
            held_entry(1753, 0, "release + restart coord-serve"),
            entry(1754, 1, QUEUE_STATE_WAITING, &[]),
        ];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: HELD — release + restart coord-serve"
        );
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.level, DriveQueueLevel::Held);
        assert_eq!(s.held, 1);
    }

    /// A `--hold-after` entry with no `--hold-reason` still gets a sentence:
    /// a bar that reads only "HELD" makes the operator reconstruct why.
    #[test]
    fn status_text_held_falls_back_to_naming_the_entry() {
        let rows = vec![held_entry(1753, 0, "")];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: HELD — deploy gate after myrepo#1753"
        );
    }

    /// A probe that keeps failing must be visible on the bar — "held" and
    /// "held and the probe has failed 40 times" need different responses
    /// from the operator.
    #[test]
    fn status_text_held_surfaces_a_rising_probe_count() {
        let rows = vec![BoardDriveQueueEntry {
            hold_probes: 7,
            ..held_entry(1753, 0, "restart coord-serve")
        }];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: HELD — restart coord-serve (probe failed 7×)"
        );
    }

    /// The issue's explicit ranking rule: HELD outranks a SIMULTANEOUS
    /// stall. The stall is the symptom (nothing is eligible *because* the
    /// queue is held); showing it would send the operator hunting a
    /// dependency bug that does not exist.
    #[test]
    fn status_text_held_outranks_a_simultaneous_stall() {
        let rows = vec![
            held_entry(1753, 0, "deploy the release"),
            // Ineligible: waiting on a pre-req still in the queue → this
            // board is ALSO stalled by `summarize_drive_queue`'s own rule.
            entry(1754, 1, QUEUE_STATE_WAITING, &["myrepo#1755"]),
            entry(1755, 2, QUEUE_STATE_WAITING, &["myrepo#1754"]),
        ];
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.eligible, 0, "precondition: this board is stalled too");
        assert_eq!(s.level, DriveQueueLevel::Held);
        assert!(drive_queue_status_text(&rows).starts_with("QUEUE: HELD"));
    }

    // ── #2186: a fired gate no longer stops the fleet by default ───────────

    /// The issue's own black-box acceptance bar, in the TUI: a fired gate on
    /// entry A (entry-scoped, the default) must not stop unrelated entry B
    /// from reading as a normally-running/eligible row — the #2186 incident
    /// was exactly this read on the always-visible status bar for 8 hours
    /// while 3 machines sat idle.
    #[test]
    fn an_entry_scoped_fired_gate_does_not_hold_an_unrelated_eligible_entry() {
        let rows = vec![
            fired_entry(2146, 0, "", HOLD_SCOPE_ENTRY),
            entry(2170, 1, QUEUE_STATE_WAITING, &[]),
        ];
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.held, 1, "the gate itself is still counted as fired…");
        assert_eq!(s.fleet_held, 0, "…but it never stops the fleet");
        assert_eq!(
            s.level,
            DriveQueueLevel::Normal,
            "an unrelated entry must read as ordinary queue activity, not HELD"
        );
        assert_eq!(s.eligible, 1, "the unrelated entry stays eligible");
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: 1 waiting",
            "the status bar must not claim the whole queue is stopped"
        );
    }

    /// The dependent that actually names the fired gate in its own `after`
    /// is NOT eligible, even though the gate's entry has by then reconciled
    /// to `done` — `after_satisfied` must check `is_holding`, not just
    /// `state != done`, mirroring `_resolve_prereqs`' `held_gates` check in
    /// `coord/drive_queue.py` (checked before `facts.landed` there for the
    /// same reason).
    #[test]
    fn a_fired_gates_own_dependent_is_not_eligible_even_though_the_gate_is_done() {
        let rows = vec![
            fired_entry(2146, 0, "", HOLD_SCOPE_ENTRY),
            entry(2147, 1, QUEUE_STATE_WAITING, &["myrepo#2146"]),
            entry(2170, 2, QUEUE_STATE_WAITING, &[]),
        ];
        let s = summarize_drive_queue(&rows);
        assert_eq!(
            s.eligible, 1,
            "only the unrelated entry is eligible — the gated dependent is not"
        );
        assert_eq!(s.waiting, 2);
        assert_eq!(
            s.level,
            DriveQueueLevel::Normal,
            "the unrelated entry launching keeps this a normal tick, not a stall"
        );
    }

    /// A FLEET-scoped gate (`--scope=fleet`, explicitly opted into) still
    /// stops everything — the deliberate whole-queue case #2186 keeps
    /// available, distinct from the entry-scoped default above.
    #[test]
    fn a_fleet_scoped_fired_gate_still_holds_an_unrelated_entry() {
        let rows = vec![
            fired_entry(2146, 0, "deploy the release", HOLD_SCOPE_FLEET),
            entry(2170, 1, QUEUE_STATE_WAITING, &[]),
        ];
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.fleet_held, 1);
        assert_eq!(s.level, DriveQueueLevel::Held);
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: HELD — deploy the release"
        );
    }

    /// A malformed/absent `hold_scope` (a row predating the #2186 column, or
    /// a wire value this build has never heard of) fails CLOSED to
    /// entry-scope — never silently escalates to a fleet-wide stop.
    /// Mirrors `QueueEntry._normalize_hold_scope` in `coord/drive_queue.py`.
    #[test]
    fn an_unrecognised_hold_scope_fails_closed_to_entry_scope() {
        let rows = vec![
            BoardDriveQueueEntry {
                hold_scope: "".to_string(),
                ..fired_entry(2146, 0, "", HOLD_SCOPE_FLEET)
            },
            entry(2170, 1, QUEUE_STATE_WAITING, &[]),
        ];
        assert!(!stops_fleet(&rows[0]));
        assert_eq!(summarize_drive_queue(&rows).level, DriveQueueLevel::Normal);
    }

    /// The Hold cell names the scope only when it's the non-default,
    /// deliberately-opted-into one — mirrors `render_plan`'s `[scope=fleet]`
    /// tag in `coord/drive_queue.py`, which is silent for the default too.
    #[test]
    fn queue_hold_cell_names_fleet_scope_but_not_the_default() {
        let app = make_test_app(BoardData {
            drive_queue: vec![
                fired_entry(2146, 0, "", HOLD_SCOPE_ENTRY),
                fired_entry(2200, 1, "", HOLD_SCOPE_FLEET),
            ],
            ..BoardData::default()
        });
        let rows = app.queue_rows();
        // Hold is cells[9] post-#2524 (After/Hold/Reason each shifted two
        // slots right when Tries became #Work/#Smoke/#Review).
        let cell_for = |issue: i64| {
            rows.iter()
                .find(|r| r.issue_number == issue)
                .map(|r| r.cells[9].clone())
                .unwrap_or_else(|| panic!("row {issue} not found"))
        };
        assert_eq!(cell_for(2146), "FIRED");
        assert_eq!(cell_for(2200), "FIRED [fleet]");
    }

    /// …and BLOCKED still outranks HELD: a gate is the system working as
    /// designed, a blocked row is something that failed.
    #[test]
    fn status_text_blocked_outranks_a_simultaneous_hold() {
        let rows = vec![
            held_entry(1753, 0, "deploy"),
            entry(1754, 1, QUEUE_STATE_BLOCKED, &[]),
        ];
        assert_eq!(summarize_drive_queue(&rows).level, DriveQueueLevel::Blocked);
    }

    #[test]
    fn held_renders_in_warn_colours_and_bolds() {
        assert_eq!(
            DriveQueueLevel::Held.colors(),
            DriveQueueLevel::Stalled.colors(),
            "HELD and STALLED both mean 'stopped, needs you' — one amber"
        );
        let app = make_test_app(BoardData {
            drive_queue: vec![held_entry(1753, 0, "deploy")],
            ..BoardData::default()
        });
        let seg = app.drive_queue_status_bar_segment();
        assert!(seg.bold, "a held queue is news");
        assert!(seg.text.contains("HELD"));
    }

    /// An `armed` gate has NOT fired — the queue is running normally and
    /// must not read HELD until the entry actually lands.
    #[test]
    fn an_armed_gate_does_not_hold_the_queue() {
        let rows = vec![BoardDriveQueueEntry {
            hold_after: 1,
            hold_reason: "deploy".to_string(),
            hold_state: "armed".to_string(),
            ..entry(1753, 0, QUEUE_STATE_RUNNING, &[])
        }];
        assert_eq!(summarize_drive_queue(&rows).held, 0);
        assert_eq!(drive_queue_status_text(&rows), "QUEUE: 1 running");
    }

    /// …and a `released` gate stops holding it, even though the row keeps
    /// `hold_after=1` forever as run history.
    #[test]
    fn a_released_gate_stops_holding_the_queue() {
        let rows = vec![
            BoardDriveQueueEntry {
                hold_state: "released".to_string(),
                ..held_entry(1753, 0, "deploy")
            },
            entry(1754, 1, QUEUE_STATE_WAITING, &[]),
        ];
        assert_eq!(summarize_drive_queue(&rows).held, 0);
        assert_eq!(drive_queue_status_text(&rows), "QUEUE: 1 waiting");
    }

    #[test]
    fn row_menu_offers_resume_only_on_a_held_row() {
        let app = make_test_app(BoardData::default());
        let held = app.context_menu_items_for_drive_queue_row("myrepo", 42, QUEUE_STATE_DONE, 0, 2, true);
        assert_eq!(
            held[0].action_id.as_deref(),
            Some("drive-queue-resume"),
            "Resume is the only action that changes anything on a held queue, \
             so it must be first"
        );
        assert!(!held[0].disabled);

        let plain =
            app.context_menu_items_for_drive_queue_row("myrepo", 42, QUEUE_STATE_WAITING, 0, 2, false);
        assert!(
            !plain.iter().any(|i| i.action_id.as_deref() == Some("drive-queue-resume")),
            "an unheld row has no gate to release"
        );
    }

    /// #1868: a fired gate's entry is `done` by construction (see
    /// `is_holding`'s doc comment), and `queue_rows` must keep it anyway — a
    /// panel that filters it out the same way it filters every other `done`
    /// row would make it impossible to ever select the row `r` needs to act
    /// on.
    #[test]
    fn queue_rows_keeps_a_held_row_even_though_it_is_done() {
        let app = make_test_app(BoardData {
            drive_queue: vec![held_entry(1753, 0, "deploy")],
            ..BoardData::default()
        });
        let rows = app.queue_rows();
        assert_eq!(
            rows.len(),
            1,
            "a held row must survive the done filter, or Resume is unreachable"
        );
        assert!(rows[0].held);
    }

    // ── Q-4 (#2524): #Work/#Smoke/#Review counts + the live Machine ──────

    /// Two `type="work"` assignments (the original dispatch, `done`, and a
    /// `[fix-1]` bounce, `running`) plus one `done` `type="smoke"` — the
    /// exact shape the issue describes. Counts land in their own buckets,
    /// and `Machine` shows the RUNNING leg's machine, not the entry's own
    /// (here unset) `--machine` pin.
    #[test]
    fn queue_row_counts_per_stage_assignments_and_shows_the_live_machine() {
        let app = make_test_app(BoardData {
            drive_queue: vec![entry(42, 0, QUEUE_STATE_RUNNING, &[])],
            assignments: vec![
                Assignment {
                    id: "a-work-done".to_string(),
                    machine: "alpha".to_string(),
                    ..make_assignment_typed("done", 42, "myrepo", Some("work"))
                },
                Assignment {
                    id: "a-work-running".to_string(),
                    machine: "bravo".to_string(),
                    ..make_assignment_typed("running", 42, "myrepo", Some("work"))
                },
                Assignment {
                    id: "a-smoke-done".to_string(),
                    machine: "alpha".to_string(),
                    ..make_assignment_typed("done", 42, "myrepo", Some("smoke"))
                },
            ],
            ..BoardData::default()
        });
        let rows = app.queue_rows();
        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!(
            row.work_count, 2,
            "the original dispatch + the [fix-1] bounce are both type=\"work\""
        );
        assert_eq!(row.smoke_count, 1);
        assert_eq!(row.review_count, 0);
        assert_eq!(row.cells[CoordApp::QUEUE_COL_WORK], "2");
        assert_eq!(row.cells[CoordApp::QUEUE_COL_SMOKE], "1");
        assert_eq!(row.cells[CoordApp::QUEUE_COL_REVIEW], "0");
        assert_eq!(
            row.cells[4], "bravo",
            "Machine must show the currently-RUNNING leg's machine, not \
             stay blank or show a stale finished leg's machine"
        );
    }

    /// A freshly-enqueued entry with nothing dispatched yet: zero counts,
    /// no panic, and the dash placeholder (not a blank cell) for `Machine`.
    #[test]
    fn queue_row_zero_assignments_renders_zero_counts_and_dash_machine() {
        let app = make_test_app(BoardData {
            drive_queue: vec![entry(99, 0, QUEUE_STATE_WAITING, &[])],
            ..BoardData::default()
        });
        let rows = app.queue_rows();
        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!(row.work_count, 0);
        assert_eq!(row.smoke_count, 0);
        assert_eq!(row.review_count, 0);
        assert_eq!(row.cells[CoordApp::QUEUE_COL_WORK], "0");
        assert_eq!(row.cells[CoordApp::QUEUE_COL_SMOKE], "0");
        assert_eq!(row.cells[CoordApp::QUEUE_COL_REVIEW], "0");
        assert_eq!(row.cells[4], QUEUE_EMPTY_CELL);
    }

    /// #2589: the pure classifier matches both pre-dispatch shapes (mirrors
    /// `coord.drive_queue.is_pre_dispatch_block_reason`'s own two cases) and
    /// nothing else — an ordinary drive-session death has real board state
    /// behind it (dispatched fine, died at a later stage), which is exactly
    /// what still might self-heal.
    #[test]
    fn is_pre_dispatch_block_reason_matches_only_the_two_pre_dispatch_shapes() {
        assert!(is_pre_dispatch_block_reason(
            "drive exited (exit_code=1): acceptance author 8e5acd6b589f exited \
             ADVISORY with no commits on its branch — nothing was authored, so \
             there is no slice to land."
        ));
        assert!(is_pre_dispatch_block_reason(
            "drive session died without landing the work (2/2 attempts) — \
             giving up — no assignment was ever created for this run (#2273): \
             likely an infrastructure/dispatch-layer failure, not a code defect"
        ));
        assert!(!is_pre_dispatch_block_reason(
            "drive exited: merge attempted 3 times without landing"
        ));
        assert!(!is_pre_dispatch_block_reason(
            "pre-req myrepo#7 is queued but blocked — it will never satisfy"
        ));
        assert!(!is_pre_dispatch_block_reason(""));
    }

    /// The exact claude-coordinator#2589 shape: two `blocked` rows that look
    /// identical by state alone — one exhausted its attempts against a
    /// pre-dispatch cause with no merge gate to ever re-check (needs a
    /// human), the other died for an ordinary reason a later tick's #2230
    /// sweep might still clear on its own. The `State` cell must tell them
    /// apart; sorting/colouring/the row menu must not (`QueueRow::state`
    /// stays the plain wire value for both).
    #[test]
    fn queue_row_marks_a_pre_dispatch_blocked_row_needs_operator_but_leaves_an_ordinary_one_plain()
    {
        let needs_operator = BoardDriveQueueEntry {
            attempts: 6,
            last_reason: "acceptance author 8e5acd6b589f exited ADVISORY with no \
                           commits on its branch — nothing was authored"
                .to_string(),
            ..entry(2531, 0, QUEUE_STATE_BLOCKED, &[])
        };
        let self_healing = BoardDriveQueueEntry {
            attempts: 1,
            last_reason: "pre-req myrepo#2531 is queued but blocked — it will \
                           never satisfy"
                .to_string(),
            ..entry(2533, 1, QUEUE_STATE_BLOCKED, &["myrepo#2531"])
        };
        let app = make_test_app(BoardData {
            drive_queue: vec![needs_operator, self_healing],
            ..BoardData::default()
        });
        let rows = app.queue_rows();
        assert_eq!(rows.len(), 2);
        let row_2531 = rows.iter().find(|r| r.issue_number == 2531).unwrap();
        let row_2533 = rows.iter().find(|r| r.issue_number == 2533).unwrap();

        assert!(
            row_2531.cells[3].ends_with(QUEUE_NEEDS_OPERATOR_SUFFIX),
            "cells: {:?}",
            row_2531.cells
        );
        assert!(
            !row_2533.cells[3].ends_with(QUEUE_NEEDS_OPERATOR_SUFFIX),
            "cells: {:?}",
            row_2533.cells
        );
        assert_ne!(row_2531.cells[3], row_2533.cells[3]);

        // Both rows are still, unambiguously, `blocked` on the wire — this
        // is presentation only. Sorting, `dq_state_colors`, and the row
        // menu's `u`/unblock action all key off `QueueRow::state`, which
        // must never carry the suffix.
        assert_eq!(row_2531.state, QUEUE_STATE_BLOCKED);
        assert_eq!(row_2533.state, QUEUE_STATE_BLOCKED);
    }

    /// A `waiting` entry that's pinned but has nothing currently running
    /// (and, to prove the fallback is "nothing running", one unrelated
    /// `done` assignment on a THIRD machine) must still show the pin, not
    /// a dash and not the done leg's machine.
    #[test]
    fn queue_row_machine_falls_back_to_the_pin_when_nothing_is_running() {
        let app = make_test_app(BoardData {
            drive_queue: vec![BoardDriveQueueEntry {
                machine: Some("pinned-box".to_string()),
                ..entry(7, 0, QUEUE_STATE_WAITING, &[])
            }],
            assignments: vec![Assignment {
                id: "a-work-done".to_string(),
                machine: "finished-on-this-one".to_string(),
                ..make_assignment_typed("done", 7, "myrepo", Some("work"))
            }],
            ..BoardData::default()
        });
        let rows = app.queue_rows();
        assert_eq!(
            rows[0].cells[4], "pinned-box",
            "no assignment is RUNNING, so the operator's own pin wins over \
             a finished leg's machine"
        );
    }

    /// The three new count columns must sort as numbers, not as rendered
    /// strings — the same #1762 defect ("`10` sorts before `2`") the
    /// original `#`/`Tries` pair was fixed against.
    #[test]
    fn queue_new_count_columns_sort_numerically_not_lexically() {
        let assignments_for = |issue: u64, n: usize| -> Vec<Assignment> {
            ["work", "smoke", "review"]
                .iter()
                .flat_map(|atype| {
                    (0..n).map(move |i| Assignment {
                        id: format!("{issue}-{atype}-{i}"),
                        ..make_assignment_typed("done", issue, "myrepo", Some(atype))
                    })
                })
                .collect()
        };
        let mut assignments = assignments_for(1, 2);
        assignments.extend(assignments_for(10, 10));
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(1, 0, QUEUE_STATE_WAITING, &[]),
                entry(10, 1, QUEUE_STATE_WAITING, &[]),
            ],
            assignments,
            ..BoardData::default()
        });
        for col in [
            CoordApp::QUEUE_COL_WORK,
            CoordApp::QUEUE_COL_SMOKE,
            CoordApp::QUEUE_COL_REVIEW,
        ] {
            assert!(app.queue_sort_by_column(col));
            let order: Vec<i64> = app.queue_rows().iter().map(|r| r.issue_number).collect();
            assert_eq!(
                order,
                vec![1, 10],
                "column {col}: the entry with 2 must sort before the one with \
                 10, not lexically after it"
            );
        }
    }

    #[test]
    fn queue_context_target_marks_a_held_row_as_held() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![held_entry(1753, 0, "deploy")],
            ..BoardData::default()
        });
        app.queue_set_sel(0);
        match app.queue_context_target().expect("a target") {
            ContextMenuTarget::DriveQueueRow { held, .. } => assert!(held),
            other => panic!("wrong target: {other:?}"),
        }
    }

    #[test]
    fn dispatch_resume_names_the_entry_and_clears_the_hold_optimistically() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![held_entry(1753, 0, "deploy")],
            ..BoardData::default()
        });
        app.dispatch_drive_queue_resume("myrepo", 1753);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec!["drive-queue", "resume", "myrepo", "1753"]]
        );
        // Optimistic: the bar leaves HELD on the next paint, not the next poll.
        assert_eq!(app.data.drive_queue[0].hold_state, "released");
        assert_eq!(
            drive_queue_status_text(&app.data.drive_queue),
            "QUEUE: empty"
        );
    }

    #[test]
    fn queue_resume_selected_resumes_a_held_row_and_refuses_any_other() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(1, 0, QUEUE_STATE_WAITING, &[]),
                held_entry(1753, 1, "deploy"),
            ],
            ..BoardData::default()
        });

        app.queue_set_sel(0);
        app.queue_resume_selected();
        assert!(
            app.command_runner.spawned_calls.is_empty(),
            "`r` on a row with no fired gate must spawn nothing"
        );

        app.queue_set_sel(1);
        app.queue_resume_selected();
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec!["drive-queue", "resume", "myrepo", "1753"]]
        );
    }

    /// #1868: the Queue panel — the drive-queue overlay's replacement — must
    /// SAY what the operator has to do, not just that something is held, and
    /// must still let them act on it once the overlay is gone.
    #[test]
    fn tuidriver_queue_panel_shows_the_gate_and_offers_resume() {
        use quadraui::tui::testing::driver_with_shell;

        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                BoardDriveQueueEntry {
                    hold_probes: 3,
                    ..held_entry(1753, 0, "release + restart coord-serve")
                },
                entry(1754, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        app.active_view = SidebarView::Queue;

        // Wide enough that the status bar's HELD sentence — reason AND
        // probe count — survives whole, the same headroom
        // `tuidriver_status_bar_renders_every_queue_state` above gives it;
        // the #1755 fixed-column driver_app repo/machine names push the
        // other segments wider than that test's fixture does.
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 240, 40);
        let screen = driver.screen();
        assert!(
            screen.contains("HELD"),
            "the status bar must read HELD while a gate is fired:\n{screen}"
        );
        assert!(
            screen.contains("release + restart coord-serve"),
            "…and name the operator's own hold reason:\n{screen}"
        );
        assert!(
            screen.contains("failed 3"),
            "a failing probe must show its rising attempt count:\n{screen}"
        );
        assert!(
            // #2042: the grid renders `M#1753` (the alias), not the full
            // `myrepo#1753` key.
            screen.contains("M#1753"),
            "the held row itself must be selectable on the panel's grid, \
             despite being `done`:\n{screen}"
        );

        let (x, y) = driver
            .find("M#1753")
            .unwrap_or_else(|| panic!("held row not found:\n{}", driver.screen()));
        driver.dispatch(UiEvent::MouseDown {
            widget: None,
            button: MouseButton::Right,
            position: Point::new(x, y),
            modifiers: Modifiers::default(),
        });
        let menu = driver.screen();
        assert!(
            menu.contains("Resume"),
            "right-click on a held row must offer Resume:\n{menu}"
        );
    }

    // ── #2043 fix: `queue_clamp_h_scroll` resize self-heal ─────────────────
    //
    // `TuiDriver` has no way to change a running driver's terminal width
    // (`TuiDriver::new` bakes it into the `TestBackend` for the driver's
    // whole life — see `quadraui::tui::testing`), so there is no in-crate
    // black-box way to drive an actual "narrow, scroll, then widen" resize
    // through `dispatch_handle` the way the other #2043 acceptance tests
    // drive clicks/drags/wheel. These are direct unit tests of the pure
    // clamp formula `render_queue_panel` calls before every paint instead —
    // see that call site's doc comment for why the clamp has to run BEFORE
    // `draw_data_table`, not after. The real end-to-end resize scenario
    // (narrow → scroll → widen) is called out in SMOKE_TESTS for manual
    // verification against a live terminal.

    #[test]
    fn queue_clamp_h_scroll_is_a_no_op_within_bounds() {
        assert_eq!(CoordApp::queue_clamp_h_scroll(20.0, 200.0, 100.0), 20.0);
    }

    #[test]
    fn queue_clamp_h_scroll_pulls_a_too_large_offset_down_to_the_new_max() {
        // The exact resize-back-above-the-floor shape the bug report
        // describes: `h_scroll` was 20 while the grid was narrow and
        // scrolling (content_width=200, visible=100 ⇒ max_scroll=100), the
        // terminal widened so the SAME table now has content_width=100 at
        // visible=100 — no more room to scroll at all, so the offset must
        // collapse to 0, not stay pinned at a now-meaningless 20.
        assert_eq!(CoordApp::queue_clamp_h_scroll(20.0, 100.0, 100.0), 0.0);
        // A partial shrink — still scrolling, but the old offset now
        // overshoots the new (smaller) max — clamps down to the new max
        // rather than staying past the end of the content.
        assert_eq!(CoordApp::queue_clamp_h_scroll(80.0, 150.0, 100.0), 50.0);
    }

    #[test]
    fn queue_clamp_h_scroll_never_goes_negative() {
        assert_eq!(CoordApp::queue_clamp_h_scroll(-5.0, 200.0, 100.0), 0.0);
    }
}
