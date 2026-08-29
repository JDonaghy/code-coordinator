//! Async fetch/parse free-function layer extracted from `app/mod.rs` (#743).
//!
//! Network I/O, subprocess spawns, parse helpers.  No quadraui rendering types
//! appear here — and, since #2895, no database access either: every board read
//! and write goes through the `coord serve` daemon over HTTP.
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use super::types::*;

/// #2895: shown when `resolve_board_service()` finds nothing — no
/// `COORD_SERVICE_URL`, no `board_service` in `~/.coord/client.toml`, and no
/// `~/.coord/serve_token` marking this box as the daemon host.  coord-tui has
/// no local-database fallback any more, so this is fatal to loading a board;
/// it is pinned in the status bar (see `CoordApp::status_bar`) rather than
/// left to look like an empty board.
pub(crate) const NO_BOARD_SERVICE_ERROR: &str =
    "no board service configured — set board_service in ~/.coord/client.toml";

/// Port `coord serve` listens on (see CLAUDE.md's "Conventions": agent 7433,
/// dashboard 7434, board daemon 7435).  Only used for the #2895 daemon-host
/// auto-detection in [`resolve_board_service`]; every other caller gets a full
/// URL from config.
pub(crate) const DAEMON_PORT: u16 = 7435;

/// Messages sent from the background SSE watch thread to the main thread.
pub(crate) enum SseWatchMsg {
    /// New log text arrived; `last_id` is the byte-offset after this chunk
    /// (used as `Last-Event-Id` on reconnect to resume without refetching).
    Lines { last_id: u64, text: String },
    /// Stream ended cleanly (agent sent `event: end`). No reconnect needed.
    Done { last_id: u64 },
    /// Connection or read error. The main thread decides whether to reconnect.
    Error(String),
    /// #2064: the agent answered the initial connect with HTTP 404 — there is
    /// no log for this assignment and there never will be (e.g. a terminal
    /// `chat`/advisory row with no live session). This is categorically
    /// different from `Error`: retrying can't fix a resource that doesn't
    /// exist, so the main thread treats it as terminal rather than feeding it
    /// into the transient-failure/backoff/reconnect machinery.
    NotFound,
    /// SSE keepalive comment received. Used to detect when the receiver has
    /// been dropped (cancel signal): if `tx.send` fails, the thread exits.
    Heartbeat,
}

/// Maximum number of concurrent SSE watch sessions held in `CoordApp.watch_pool`.
/// When adding a new session would exceed this limit the least-recently-focused
/// entry is evicted (dropping its `Receiver` cancels the background thread).
pub(crate) const WATCH_POOL_CAP: usize = 8;

/// State for the live SSE log-stream connection backing the watch overlay.
///
/// Held inside `WatchContext` in the `watch_pool` map.  Dropped (and thus the
/// background thread cancelled) when the context is evicted from the pool.
pub(crate) struct WatchSseState {
    /// Receive end of the channel from the background SSE thread.
    pub(crate) rx: std::sync::mpsc::Receiver<SseWatchMsg>,
    /// Accumulated raw log lines, appended as `Lines` messages arrive.
    pub(crate) lines: Vec<String>,
    /// Wall-clock arrival time for each entry in `lines` (parallel vec).
    /// Used to compute per-turn elapsed time in the watch overlay.
    pub(crate) line_times: Vec<Instant>,
    /// Count of `"type":"assistant"` events seen so far — drives the
    /// live turn-count badge on the Active stage box.
    pub(crate) current_turn: usize,
    /// Byte offset of the last received event, for `Last-Event-Id` on reconnect.
    pub(crate) last_event_id: u64,
    /// Number of connection failures in the current 10-second window.
    pub(crate) fail_count: u32,
    /// When the first failure in the current window occurred, for TTL reset.
    pub(crate) first_fail_at: Option<Instant>,
    /// True once a clean `end` event arrives or the failure limit is hit.
    /// When true, no further reconnect attempts are made.
    pub(crate) done: bool,
    /// Machine hostname, stored here so reconnect doesn't need to look up the
    /// machine list again.
    pub(crate) host: String,
    /// Partial trailing line carried over between SSE chunks. The agent reads
    /// the log in fixed 4 KB chunks (events.LOG_CHUNK_SIZE), so a long JSON
    /// line (e.g. a `{"type":"result"...}` event with the full review body)
    /// can be split mid-line. Without reassembly the client would parse two
    /// broken halves and lose `total_cost_usd` / `stop_reason` from the
    /// metrics line. Held here until the next chunk arrives.
    pub(crate) pending_tail: String,
}

/// #2572: this machine's own live `agent_venv` H-1 check result, parsed
/// straight off `/health`'s `health.results[]` (`coord.agent.AgentServer.
/// health`'s `"health"` key — `coord/health/checks/agent_install.py`'s
/// `agent_venv` check, cache-refreshed on `coord-agent.service`'s own TTL,
/// completely independent of whatever `coord serve`'s own fleet-health
/// snapshot says). See `merge_live_agent_venv_health`'s doc comment for why
/// this exists on top of `BoardData::fleet_health`, which already carries
/// (a possibly stale, possibly empty) `agent_venv` reading of its own.
#[derive(Clone, Debug)]
pub(crate) struct AgentVenvHealth {
    /// `"ok"` | `"warn"` | `"crit"` | `"unknown"` — verbatim from the wire,
    /// same posture `fleet_health.rs`'s doc comment insists on for every
    /// other severity string this app renders: never re-derived here.
    pub(crate) severity: String,
    /// Human-readable one-liner (`CheckResult.headroom`), e.g. "editable
    /// 0.5.240 from ~/.coord/worktrees/9c9cc8b694bd".
    pub(crate) headroom: String,
}

/// Parsed fields from a successful `/health` HTTP response.
pub(crate) struct MachineHealthResult {
    pub(crate) version: String,
    pub(crate) worktree_bytes: u64,
    /// `None` when `/health`'s `health.results[]` carries no `agent_venv`
    /// entry at all (an old agent that predates #1630, or a `checkout`/
    /// `fleet`-scope-only build) — never a fabricated "ok".
    pub(crate) agent_venv: Option<AgentVenvHealth>,
}

/// Spawn a background thread that fetches `/health` from a remote agent and
/// parses the version + worktree_bytes fields (plus, #2572, the live
/// `agent_venv` check result — see `AgentVenvHealth`).  Returns a
/// `Receiver` that yields `Ok(result)` or `Err(error_string)`.
pub(crate) fn spawn_machine_health(
    host: &str,
    port: u16,
) -> std::sync::mpsc::Receiver<Result<MachineHealthResult, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:{}/health", host, port);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(2))
            .timeout(std::time::Duration::from_secs(2))
            .build();
        let result = match agent.get(&url).call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match serde_json::from_str::<serde_json::Value>(&body) {
                    Ok(v) => {
                        let version = v
                            .get("version")
                            .and_then(|x| x.as_str())
                            .unwrap_or("?")
                            .to_string();
                        let worktree_bytes = v
                            .get("worktree_bytes")
                            .and_then(|x| x.as_u64())
                            .unwrap_or(0);
                        let agent_venv = parse_agent_venv_health(&v);
                        Ok(MachineHealthResult {
                            version,
                            worktree_bytes,
                            agent_venv,
                        })
                    }
                    Err(e) => Err(format!("json: {}", e)),
                },
                Err(e) => Err(e.to_string()),
            },
            Err(e) => Err(e.to_string()),
        };
        let _ = tx.send(result);
    });
    rx
}

/// Pull the `agent_venv` entry out of `/health`'s `health.results[]` array,
/// if present (#2572). A free function (not inlined into the closure above)
/// so it is reachable from `#[cfg(test)]` with a hand-built JSON value,
/// without needing a live HTTP fetch.
pub(crate) fn parse_agent_venv_health(health_response: &serde_json::Value) -> Option<AgentVenvHealth> {
    let results = health_response.get("health")?.get("results")?.as_array()?;
    let entry = results
        .iter()
        .find(|item| item.get("check_id").and_then(|c| c.as_str()) == Some("agent_venv"))?;
    Some(AgentVenvHealth {
        severity: entry
            .get("severity")
            .and_then(|s| s.as_str())
            .unwrap_or("unknown")
            .to_string(),
        headroom: entry
            .get("headroom")
            .and_then(|s| s.as_str())
            .unwrap_or("")
            .to_string(),
    })
}

/// Rank order for the four severity strings this app ever renders — mirrors
/// `fleet_health.rs`'s `FleetSeverity`'s `#[derive(Ord)]` declaration order
/// exactly (`Ok < Unknown < Warn < Crit`: an absent/unrecognised signal
/// outranks a claimed-healthy one, but never a genuine warning or worse —
/// see that enum's own doc comment for why). Duplicated here rather than
/// imported: `fleet_health.rs` is the rendering module and this is the
/// data-assembly one, and the two already stay in sync "by hand, keep in
/// sync" per that module's own doc comment for the exact same counting
/// rule — one more small duplication in the same spirit, not a new pattern.
fn severity_rank(severity: &str) -> u8 {
    match severity {
        "crit" => 3,
        "warn" => 2,
        "ok" => 0,
        // "unknown", empty, or anything unrecognised — never silently "ok".
        _ => 1,
    }
}

/// The synthesized `FleetHealthCheckResult` row for a live `agent_venv`
/// reading — used only by `merge_live_agent_venv_health` below.
fn live_agent_venv_check_result(machine: &str, av: &AgentVenvHealth) -> FleetHealthCheckResult {
    FleetHealthCheckResult {
        key: format!("{machine}:agent_venv:live"),
        check_id: "agent_venv".to_string(),
        title: "agent venv".to_string(),
        label: format!("agent venv ({machine})"),
        subject: Some(machine.to_string()),
        severity: av.severity.clone(),
        headroom: av.headroom.clone(),
        threshold: String::new(),
        detail: "live reading via this machine's own /health (#2572) — \
                  coord serve's fleet-health snapshot did not already \
                  report this machine at least as severely"
            .to_string(),
    }
}

/// Fold each machine's own LIVE `agent_venv` health-check result (from
/// `/health`'s `health.results[]` — see [`AgentVenvHealth`]) into
/// *existing* (`coord serve`'s own fleet-health snapshot — see
/// `BoardData::fleet_health`'s doc comment).
///
/// This exists on top of the daemon-computed snapshot for the same reason
/// #2572 exists at all: `existing` can be **empty by design** (the
/// local-SQLite read path has no daemon in-process to poll agent `/health`
/// or run the fleet-scope registry at all — see `load_data`'s own comment
/// on this field) or simply **stale**, because it is computed by
/// `coord serve`, a process that can share the *exact* failure domain the
/// check itself is reporting on (#2569/#2570: an editable `~/.coord-venv`
/// broke `coord-drive-queue.service` and `coord-notify.service`, both of
/// which exec that same venv — a `coord serve` sharing it would have frozen
/// its own snapshot at whatever it last computed, which reads as "healthy,
/// last measured a while ago" rather than "CRIT"). The live per-machine
/// `/health` fetch this function consumes runs on a *different* systemd
/// unit (`coord-agent.service`) and answers fresh, on this exact poll — so
/// even if the daemon's own snapshot is wrong or absent, this cannot be.
///
/// **Never downgrades.** A machine already reported by `existing` at least
/// as severely (by [`severity_rank`]) is left completely untouched — this
/// only ever ADDS a machine `existing` has no entry for, or REPLACES an
/// entry whose severity is weaker than what was just observed live. A
/// live `ok`/`unknown` reading is never worth synthesizing a row for at
/// all (an `agent_venv` check `existing` already has an equal-or-worse
/// opinion on is not this function's business, and there is nothing useful
/// to say about a machine this probe found healthy that `existing` didn't
/// already know).
fn merge_live_agent_venv_health(
    mut existing: FleetHealthBlock,
    live: &[(String, Option<AgentVenvHealth>)],
    now: f64,
) -> FleetHealthBlock {
    for (name, probe) in live {
        let Some(av) = probe else { continue };
        let live_rank = severity_rank(&av.severity);
        if live_rank < severity_rank("warn") {
            continue;
        }
        match existing.machine_health.iter_mut().find(|m| &m.machine == name) {
            Some(slot) if severity_rank(&slot.severity) >= live_rank => {
                // `existing` already has an equal-or-worse reading for this
                // machine (possibly for a DIFFERENT check entirely) — leave
                // it exactly as `coord serve` reported it.
            }
            Some(slot) => {
                slot.severity = av.severity.clone();
                slot.stale = false;
                slot.checked_at = Some(now);
                slot.results = vec![live_agent_venv_check_result(name, av)];
            }
            None => {
                existing.machine_health.push(FleetMachineHealth {
                    machine: name.clone(),
                    state: String::new(),
                    severity: av.severity.clone(),
                    stale: false,
                    checked_at: Some(now),
                    results: vec![live_agent_venv_check_result(name, av)],
                });
            }
        }
    }
    existing
}

/// How many samples to keep per machine (5 min @ 5 s/sample).
pub(crate) const METRICS_HISTORY: usize = 60;
/// How often to poll each reachable machine's `/metrics` endpoint.
pub(crate) const METRICS_CADENCE: Duration = Duration::from_secs(5);

/// One `/metrics` snapshot from a remote agent.
#[derive(Clone, Copy)]
pub(crate) struct MetricSample {
    pub(crate) cpu: f32,
    pub(crate) mem: f32,
}

/// In-flight metrics fetch for one machine.
pub(crate) struct PendingMetrics {
    pub(crate) machine: String,
    pub(crate) rx: std::sync::mpsc::Receiver<Result<MetricSample, String>>,
}

/// Spawn a background thread that fetches `/metrics` from a remote agent.
pub(crate) fn spawn_machine_metrics(host: &str, port: u16, machine: String) -> PendingMetrics {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:{}/metrics", host, port);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(2))
            .timeout(std::time::Duration::from_secs(3))
            .build();
        let result = match agent.get(&url).call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match serde_json::from_str::<serde_json::Value>(&body) {
                    Ok(v) => {
                        let cpu = v
                            .get("cpu_percent")
                            .and_then(|x| x.as_f64())
                            .unwrap_or(0.0) as f32;
                        let mem = v
                            .get("mem_percent")
                            .and_then(|x| x.as_f64())
                            .unwrap_or(0.0) as f32;
                        Ok(MetricSample { cpu, mem })
                    }
                    Err(e) => Err(format!("json: {}", e)),
                },
                Err(e) => Err(e.to_string()),
            },
            Err(e) => Err(e.to_string()),
        };
        let _ = tx.send(result);
    });
    PendingMetrics { machine, rx }
}

/// One file entry from the agent's `/artifact/<repo>/<branch>` manifest.
/// Fields are parsed from JSON for completeness; the current UI uses only
/// the count and `ArtifactManifest::total_bytes` for the badge line.
#[derive(Clone)]
pub(crate) struct ArtifactFile {
    #[allow(dead_code)]
    pub(crate) name: String,
    #[allow(dead_code)]
    pub(crate) size: u64,
}

/// Parsed manifest returned by `GET /artifact/<repo>/<branch>` on an agent.
#[derive(Clone)]
pub(crate) struct ArtifactManifest {
    pub(crate) files: Vec<ArtifactFile>,
    pub(crate) total_bytes: u64,
    /// The assignment that produced this stash (may differ from the current
    /// work assignment when the branch was rebuilt on a later push).
    pub(crate) built_by_assignment_id: Option<String>,
}

/// Reason why no artifact manifest is available after a completed fetch.
/// Used to surface a human-readable explanation in the TUI rather than
/// silently hiding the `[a]` badge when artifacts are absent.
#[derive(Debug, Clone)]
pub(crate) enum ArtifactAbsence {
    /// HTTP 404 — worker did not stash any artifacts.  Likely causes: no
    /// `artifact_paths` configured for this repo in `coordinator.yml`, or the
    /// build produced no files matching the configured globs.
    NotStashed,
    /// HTTP 200 but the `files` array in the manifest was empty.
    ManifestEmpty,
    /// Could not reach the agent at all (connection refused, timeout, DNS
    /// failure, or a JSON-parse error on the response body).
    AgentUnreachable(String),
}

/// Whether a change for this issue/repo is *expected* to produce the
/// configured build artifact.  claude-coordinator's only `artifact_paths`
/// entry is `tui/target/debug/coord-tui`, produced solely by `tui/` changes;
/// a `coord/**` CLI/Python change never builds it (titled `coord:` vs the
/// `coord-tui:` convention for TUI work).  For such a change an empty stash
/// is *expected*, not a failure — the test path is a branch checkout, not an
/// artifact pull.  Other repos are build-centric, so every change produces
/// their artifact.
pub(crate) fn issue_produces_build_artifact(repo: &str, title: &str) -> bool {
    if repo == "claude-coordinator" {
        title.to_lowercase().contains("coord-tui")
    } else {
        true
    }
}

/// Actionable explanation for an empty/absent artifact stash, told apart by
/// whether the change was expected to build an artifact at all.  Stops an
/// empty stash from reading as a generic failure (#563/#569): a CLI change
/// has nothing to pull (test the branch directly); a build-producing change
/// with an empty stash means the session exited without a successful build.
pub(crate) fn artifact_absence_body(produces_artifact: bool, branch: &str) -> String {
    if produces_artifact {
        format!(
            "No build artifact stashed for branch `{branch}`.\n\
             A build was expected but the session exited without producing \
             the configured artifact (no successful build, or nothing matched \
             artifact_paths).\n\n\
             To test, check out the branch and build it locally:\n  \
             git fetch origin && git checkout {branch}\n  \
             # then the project's build (for coord-tui: `cd tui && cargo build \
             && cp target/debug/coord-tui ~/.local/bin/coord-tui`)"
        )
    } else {
        format!(
            "No artifact for branch `{branch}` — and none is expected.\n\
             This is a coord/ CLI/Python change; it doesn't build the coord-tui \
             binary, so there is nothing to pull.\n\n\
             Test it from the branch instead:\n  \
             git fetch origin && git checkout {branch}   # then run `coord ...`\n  \
             (or `pip install -e <worktree>` in a throwaway venv)"
        )
    }
}

/// A cached manifest entry with a fetch timestamp for 30-second TTL eviction.
pub(crate) struct ArtifactCacheEntry {
    pub(crate) fetched_at: Instant,
    /// `Some` = stash present and non-empty.  `None` = fetch completed but no
    /// artifacts are available; see `absence_reason` for the specific cause.
    pub(crate) manifest: Option<ArtifactManifest>,
    /// Explains why `manifest` is `None` when set.  Always `Some` when the
    /// fetch has completed without finding a non-empty manifest.
    pub(crate) absence_reason: Option<ArtifactAbsence>,
}

/// #1337: one hydrated (or failed) full-findings detail fetch — see
/// `CoordApp::findings_detail_cache`.
pub(crate) struct FindingsDetailEntry {
    pub(crate) fetched_at: Instant,
    /// `Some(raw)` = the full `review_findings` JSON string from
    /// `GET /assignment/{id}`.  `None` = the fetch failed; re-armed after a
    /// 30 s back-off so a down daemon isn't hammered every tick.
    pub(crate) full: Option<String>,
}

/// #2497: one hydrated (or failed) full-issue-body detail fetch — see
/// `CoordApp::issue_detail_cache`. Mirrors [`FindingsDetailEntry`] (#1337)
/// for `issues.body` instead of `assignments.review_findings`.
pub(crate) struct IssueDetailEntry {
    pub(crate) fetched_at: Instant,
    /// `Some(body)` = the full issue body from `GET /issue/{repo}/{number}`.
    /// `None` = the fetch failed; re-armed after a 30 s back-off so a down
    /// daemon isn't hammered every tick.
    pub(crate) full: Option<String>,
}

/// #336: Sanitize a git branch name for use as a URL path component.
///
/// Mirrors Python's `coord.agent._sanitize_branch`: replaces runs of
/// characters that are not alphanumeric, `.`, `_`, or `-` with a single dash,
/// then strips any leading/trailing dashes from the result.
pub(crate) fn sanitize_branch(branch: &str) -> String {
    let mut result = String::with_capacity(branch.len());
    let mut in_run = false;
    for c in branch.chars() {
        if c.is_alphanumeric() || c == '.' || c == '_' || c == '-' {
            in_run = false;
            result.push(c);
        } else if !in_run {
            result.push('-');
            in_run = true;
        }
    }
    result.trim_matches('-').to_string()
}

/// #349: Read the current HEAD SHA for a git branch by examining the local
/// `.git` directory directly — fast (just file I/O) and safe to call from
/// the render thread.  Returns `None` when the file doesn't exist, is
/// unreadable, or the branch is not yet known locally.
///
/// Handles both loose refs (`refs/heads/<branch>`) and packed refs
/// (`packed-refs` file).
pub(crate) fn read_git_branch_head(repo_dir: &std::path::Path, branch: &str) -> Option<String> {
    use std::fs;
    // First try the loose ref file: .git/refs/heads/<branch>.
    // Branch names may contain slashes (feature/foo), which map to subdirs.
    let loose = repo_dir
        .join(".git")
        .join("refs")
        .join("heads")
        .join(branch);
    if let Ok(content) = fs::read_to_string(&loose) {
        let sha = content.trim().to_string();
        if !sha.is_empty() {
            return Some(sha);
        }
    }
    // Fall back to .git/packed-refs.  Format: "<sha> refs/heads/<branch>"
    let packed = repo_dir.join(".git").join("packed-refs");
    if let Ok(content) = fs::read_to_string(&packed) {
        let needle = format!("refs/heads/{}", branch);
        for line in content.lines() {
            if line.starts_with('#') || line.starts_with('^') {
                continue;
            }
            let mut parts = line.splitn(2, ' ');
            let sha = parts.next()?;
            let refname = parts.next()?;
            if refname.trim() == needle {
                return Some(sha.trim().to_string());
            }
        }
    }
    None
}

/// Outcome of a single `GET /artifact/<repo>/<branch>` request to a remote
/// agent.  Returned via channel from `spawn_artifact_fetch` so the TUI can
/// surface a specific reason when the `[a]` badge is absent rather than
/// silently hiding it.
pub(crate) enum ArtifactFetchOutcome {
    /// HTTP 200 with at least one file — the artifact badge should be shown.
    Found(ArtifactManifest),
    /// HTTP 404 — no stash exists for this (repo, branch) pair on the agent.
    NotStashed,
    /// HTTP 200 but the `files` array in the manifest was empty.
    Empty,
    /// Network / parse error — the agent could not be reached or returned
    /// an unexpected response.
    Unreachable(String),
}

/// #336: Spawn a background thread that queries `GET /artifact/<repo>/<branch>`
/// on a remote agent.  Returns a channel that delivers an [`ArtifactFetchOutcome`]
/// so the caller can distinguish 404, empty manifest, and network errors.
pub(crate) fn spawn_artifact_fetch(
    host: &str,
    repo: &str,
    branch: &str,
) -> std::sync::mpsc::Receiver<ArtifactFetchOutcome> {
    let url = format!("http://{}:7433/artifact/{}/{}", host, repo, branch);
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(3))
            .timeout(std::time::Duration::from_secs(5))
            .build();
        let outcome = match agent.get(&url).call() {
            Err(ureq::Error::Status(404, _)) => ArtifactFetchOutcome::NotStashed,
            Err(e) => ArtifactFetchOutcome::Unreachable(e.to_string()),
            Ok(resp) => match resp.into_string() {
                Err(e) => ArtifactFetchOutcome::Unreachable(e.to_string()),
                Ok(body) => match serde_json::from_str::<serde_json::Value>(&body) {
                    Err(e) => ArtifactFetchOutcome::Unreachable(format!("json: {e}")),
                    Ok(v) => {
                        let files: Vec<ArtifactFile> = v
                            .get("files")
                            .and_then(|f| f.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|item| {
                                        let name = item.get("name")?.as_str()?.to_string();
                                        let size =
                                            item.get("size").and_then(|s| s.as_u64()).unwrap_or(0);
                                        Some(ArtifactFile { name, size })
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        let total_bytes =
                            v.get("total_bytes").and_then(|t| t.as_u64()).unwrap_or(0);
                        let built_by_assignment_id = v
                            .get("built_by_assignment_id")
                            .and_then(|b| b.as_str())
                            .map(|s| s.to_string());
                        if files.is_empty() {
                            ArtifactFetchOutcome::Empty
                        } else {
                            ArtifactFetchOutcome::Found(ArtifactManifest {
                                files,
                                total_bytes,
                                built_by_assignment_id,
                            })
                        }
                    }
                },
            },
        };
        let _ = tx.send(outcome);
    });
    rx
}

/// Outcome of a single `GET /audit` request (#1039), delivered via channel
/// from `spawn_audit_fetch`.
pub(crate) enum AuditFetchOutcome {
    /// HTTP 200 with a parsed `AuditPage` (may have zero entries).
    Page(AuditPage),
    /// No board service is configured (`resolve_board_service` returned
    /// `None`) — the Audit panel has nothing to fetch from in this mode
    /// (local-SQLite-mode has no `/audit` HTTP surface). Rendered the same
    /// as "fetch not yet completed" (contract §4b empty state).
    NoBoardService,
    /// Network / parse error, or a non-2xx HTTP status.
    Unreachable(String),
}

/// How often to re-fetch `/audit` while the Audit panel is visible (#1039).
/// Mirrors the 30 s TTL `ArtifactCacheEntry` uses for the Pipeline Test
/// stage — audit entries are append-only and low-volume, so a slightly
/// slower cadence than the 5 s machine-metrics poll is plenty responsive.
pub(crate) const AUDIT_FETCH_TTL: Duration = Duration::from_secs(15);

/// #1039/#1040: spawn a background thread that fetches the first page of
/// `GET /audit` from the configured board service (`resolve_board_service`),
/// mirroring `spawn_artifact_fetch`'s thread-per-request pattern. Armed by
/// the caller only while `active_view == SidebarView::Audit` (see the poll
/// loop in `settings_ui.rs`), same gating discipline as the Machines-panel
/// metrics poll above.
///
/// `since`/`category`/`event_type`/`tier` are the Audit panel's current
/// filter selection (contract §8/§9/§11, `tests/acceptance/ms-33/
/// contract.md`, plus the #2653 tier filter which predates any contract
/// pin) — `None`/empty means "no filter", matching `/audit`'s own
/// optional-param semantics (#1037). Values ride as `ureq` query params
/// (not manually interpolated into the URL) so free-text `event_type` input
/// never needs its own percent-encoding.
pub(crate) fn spawn_audit_fetch(
    since: Option<f64>,
    category: Option<&str>,
    event_type: Option<&str>,
    tier: Option<&str>,
) -> std::sync::mpsc::Receiver<AuditFetchOutcome> {
    let (tx, rx) = std::sync::mpsc::channel();
    let Some((url, token)) = resolve_board_service() else {
        let _ = tx.send(AuditFetchOutcome::NoBoardService);
        return rx;
    };
    let category = category.map(|s| s.to_string());
    let event_type = event_type.map(|s| s.to_string());
    let tier = tier.map(|s| s.to_string());
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            .timeout(std::time::Duration::from_secs(8))
            .build();
        let mut req = agent.get(&format!("{url}/audit"));
        if let Some(since) = since {
            req = req.query("since", &since.to_string());
        }
        if let Some(category) = &category {
            req = req.query("category", category);
        }
        if let Some(event_type) = &event_type {
            req = req.query("type", event_type);
        }
        if let Some(tier) = &tier {
            req = req.query("tier", tier);
        }
        if let Some(t) = &token {
            req = req.set("Authorization", &format!("Bearer {t}"));
        }
        let outcome = match req.call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match serde_json::from_str::<AuditPage>(&body) {
                    Ok(page) => AuditFetchOutcome::Page(page),
                    Err(e) => AuditFetchOutcome::Unreachable(format!("json: {e}")),
                },
                Err(e) => AuditFetchOutcome::Unreachable(e.to_string()),
            },
            Err(e) => AuditFetchOutcome::Unreachable(e.to_string()),
        };
        let _ = tx.send(outcome);
    });
    rx
}

// ── #1741: Reports panel fetches (`GET /report`, `GET /report/{id}`) ──────
//
// Both mirror `spawn_audit_fetch` above: thread-per-request, `ureq` query
// params (never hand-interpolated — a free-text `repo` param would otherwise
// need its own percent-encoding), and armed by the caller only while
// `active_view == SidebarView::Reports` (see the poll loop in
// `settings_ui.rs`), so no background thread runs while the operator is
// elsewhere.

/// Outcome of the one-per-session `GET /report` catalogue fetch (#1741).
pub(crate) enum ReportsCatalogueOutcome {
    /// HTTP 200 with a parsed catalogue (may legitimately be empty).
    Catalogue(Vec<ReportDef>),
    /// No board service configured — nothing to fetch from (the report
    /// engine is a daemon-side surface; there is no local-SQLite path).
    NoBoardService,
    /// Network / parse error, or a non-2xx status. A daemon predating #1742
    /// answers 404/405 here, which is exactly why the message is surfaced
    /// in the panel rather than swallowed.
    Unreachable(String),
}

/// Outcome of one `GET /report/{id}` run (#1741).
pub(crate) enum ReportRunOutcome {
    /// HTTP 200 with a parsed `ReportResult` (may have zero rows — that is a
    /// real answer, not an error).
    Result(Box<ReportResult>),
    NoBoardService,
    /// Network / parse error, or a non-2xx status. The engine answers 400
    /// (bad param) / 404 (unknown report) / 503 (run failed) with a JSON
    /// `{"error": ...}` body, which `report_http_error` unwraps so the
    /// operator sees the engine's own message.
    Unreachable(String),
}

/// Turn a `ureq` failure into the message the panel shows. A non-2xx status
/// from the report routes carries a JSON `{"error": "..."}` body written to
/// read well on its own (`coord/reports.py`'s `ReportError` docstring says
/// so explicitly) — surface that rather than a bare "HTTP 400".
fn report_http_error(err: ureq::Error) -> String {
    match err {
        ureq::Error::Status(code, resp) => {
            let body = resp.into_string().unwrap_or_default();
            let detail = serde_json::from_str::<serde_json::Value>(&body)
                .ok()
                .and_then(|v| {
                    v.get("error")
                        .and_then(|e| e.as_str().map(|s| s.to_string()))
                })
                .unwrap_or_else(|| body.trim().to_string());
            if detail.is_empty() {
                format!("HTTP {code}")
            } else {
                format!("HTTP {code}: {detail}")
            }
        }
        other => other.to_string(),
    }
}

/// Shared `ureq` agent settings for both report requests. A report run reads
/// the audit trail over a window, so it gets a longer read timeout than
/// `/audit`'s 8s page fetch.
fn report_agent(read_timeout_secs: u64) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(read_timeout_secs))
        .build()
}

/// Fetch the report catalogue. Static metadata, so the caller fetches it
/// once per session rather than on a TTL.
pub(crate) fn spawn_reports_catalogue_fetch() -> std::sync::mpsc::Receiver<ReportsCatalogueOutcome>
{
    let (tx, rx) = std::sync::mpsc::channel();
    let Some((url, token)) = resolve_board_service() else {
        let _ = tx.send(ReportsCatalogueOutcome::NoBoardService);
        return rx;
    };
    std::thread::spawn(move || {
        let mut req = report_agent(8).get(&format!("{url}/report"));
        if let Some(t) = &token {
            req = req.set("Authorization", &format!("Bearer {t}"));
        }
        let outcome = match req.call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match serde_json::from_str::<ReportCatalogue>(&body) {
                    Ok(cat) => ReportsCatalogueOutcome::Catalogue(cat.reports),
                    Err(e) => ReportsCatalogueOutcome::Unreachable(format!("json: {e}")),
                },
                Err(e) => ReportsCatalogueOutcome::Unreachable(e.to_string()),
            },
            Err(e) => ReportsCatalogueOutcome::Unreachable(report_http_error(e)),
        };
        let _ = tx.send(outcome);
    });
    rx
}

/// Run one report. `params` is `(param_id, value)` straight from the
/// catalogue's own param list — this function knows nothing about which
/// parameters any report has.
pub(crate) fn spawn_report_run(
    report_id: &str,
    params: Vec<(String, String)>,
) -> std::sync::mpsc::Receiver<ReportRunOutcome> {
    let (tx, rx) = std::sync::mpsc::channel();
    let Some((url, token)) = resolve_board_service() else {
        let _ = tx.send(ReportRunOutcome::NoBoardService);
        return rx;
    };
    let report_id = report_id.to_string();
    std::thread::spawn(move || {
        let mut req = report_agent(30).get(&format!("{url}/report/{report_id}"));
        for (key, value) in &params {
            // An empty value means "the server's default" — `resolve_params`
            // treats absent and empty identically, so don't send it.
            if !value.is_empty() {
                req = req.query(key, value);
            }
        }
        if let Some(t) = &token {
            req = req.set("Authorization", &format!("Bearer {t}"));
        }
        let outcome = match req.call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match serde_json::from_str::<ReportResult>(&body) {
                    Ok(result) => ReportRunOutcome::Result(Box::new(result)),
                    Err(e) => ReportRunOutcome::Unreachable(format!("json: {e}")),
                },
                Err(e) => ReportRunOutcome::Unreachable(e.to_string()),
            },
            Err(e) => ReportRunOutcome::Unreachable(report_http_error(e)),
        };
        let _ = tx.send(outcome);
    });
    rx
}

/// Outcome of one CSV export (#1765): fetch `?format=csv`, write the bytes.
pub(crate) enum ReportExportOutcome {
    /// Written. Carries the destination and the byte count, both of which
    /// the panel shows — "saved" with no path is barely better than silence.
    Written { path: std::path::PathBuf, bytes: usize },
    NoBoardService,
    /// The fetch failed, or the write did. Both are reported the same way
    /// (a visible message in the notes area) because both mean "there is no
    /// file where you asked for one".
    Failed(String),
}

/// Fetch one report as CSV and write it to `dest` (#1765).
///
/// The CSV is produced **server-side** — this asks the daemon for
/// `?format=csv` rather than formatting the `ReportResult` the panel already
/// holds. That is the whole point of the feature: the panel's cells are
/// display strings (`13h ago`, `dellserver, precision`) rendered through
/// `column_meta`, so a client-side CSV would export the formatting instead
/// of the data, and its contents would depend on when Export was clicked.
/// Going back to the server also keeps these bytes identical to
/// `coord report run --format csv`.
///
/// Note the params are the ones sent to `/report/{id}`, not the ones that
/// produced the on-screen result: a re-fetch re-runs the report, so the
/// exported window is the one currently in the form. `reports_start_export`
/// only arms this for a report that has already been run, so the two agree
/// unless the operator edited a parameter without re-running.
pub(crate) fn spawn_report_export(
    report_id: &str,
    params: Vec<(String, String)>,
    dest: std::path::PathBuf,
) -> std::sync::mpsc::Receiver<ReportExportOutcome> {
    let (tx, rx) = std::sync::mpsc::channel();
    let Some((url, token)) = resolve_board_service() else {
        let _ = tx.send(ReportExportOutcome::NoBoardService);
        return rx;
    };
    let report_id = report_id.to_string();
    std::thread::spawn(move || {
        let mut req = report_agent(30).get(&format!("{url}/report/{report_id}"));
        for (key, value) in &params {
            if !value.is_empty() {
                req = req.query(key, value);
            }
        }
        req = req.query("format", "csv");
        if let Some(t) = &token {
            req = req.set("Authorization", &format!("Bearer {t}"));
        }
        let outcome = match req.call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match std::fs::write(&dest, body.as_bytes()) {
                    Ok(()) => ReportExportOutcome::Written {
                        path: dest,
                        bytes: body.len(),
                    },
                    Err(e) => {
                        ReportExportOutcome::Failed(format!("write {}: {e}", dest.display()))
                    }
                },
                Err(e) => ReportExportOutcome::Failed(e.to_string()),
            },
            Err(e) => ReportExportOutcome::Failed(report_http_error(e)),
        };
        let _ = tx.send(outcome);
    });
    rx
}

/// #315: signal that `spawn_inject_post` sends to the main thread when
/// the /inject POST returns HTTP 409 ("assignment is `done`") or 410
/// (BrokenPipeError — worker stdin closed).  Both mean the worker exited
/// after submit_inject's `worker_done` check but before the HTTP request
/// landed — a race window of a few hundred ms.  The main thread reacts
/// by dispatching `coord chat-continue` so the message isn't lost.
#[derive(Clone)]
pub(crate) struct InjectFallback {
    pub(crate) aid: String,
    pub(crate) text: String,
    pub(crate) issue_number: u64,
}

/// #264: POST a chat user-turn to a remote agent's `/inject/{id}` endpoint
/// in a background thread.  Used by `submit_inject` to bypass the
/// single-slot `command_runner` so chat submits aren't blocked by the
/// auto-`coord notify` cycle (every 30 s while any assignment is running).
///
/// #315: on HTTP 409/410 (worker exited mid-flight), sends an
/// `InjectFallback` over `fallback_tx` so the main thread can transparently
/// trigger `coord chat-continue` — otherwise the typed message would be
/// silently lost when the racing worker-exit beats the inject POST.
pub(crate) fn spawn_inject_post(
    host: &str,
    assignment_id: &str,
    text: &str,
    issue_number: u64,
    fallback_tx: std::sync::mpsc::Sender<InjectFallback>,
) {
    let url = format!("http://{}:7433/inject/{}", host, assignment_id);
    let payload = serde_json::json!({ "text": text });
    let body = payload.to_string();
    let aid = assignment_id.to_string();
    let text_owned = text.to_string();
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            .timeout(std::time::Duration::from_secs(15))
            .build();
        match agent
            .post(&url)
            .set("Content-Type", "application/json")
            .send_string(&body)
        {
            Ok(_) => {}
            Err(ureq::Error::Status(code, _)) if code == 409 || code == 410 => {
                // Worker exited mid-flight — signal the main thread so it can
                // transparently fall back to `coord chat-continue`.  The user's
                // typed message is preserved on the channel.
                let _ = fallback_tx.send(InjectFallback {
                    aid,
                    text: text_owned,
                    issue_number,
                });
            }
            Err(e) => {
                eprintln!("[chat inject] POST {} failed: {}", url, e);
            }
        }
    });
}

/// #315: shell `coord [--config <path>] chat-continue <old_aid> <text>` in a
/// background thread.  Fire-and-forget — the TUI does not capture stdout here;
/// instead, `maybe_bind_pending_resume` polls `self.data.assignments` each tick
/// for the new row that `coord chat-continue` inserts into the coordinator DB.
///
/// Uses a raw thread rather than `CommandRunner` so the auto-`coord notify`
/// cycle (single-slot) is never blocked during an active chat session.
pub(crate) fn spawn_chat_continue(
    config_path: Option<std::path::PathBuf>,
    old_assignment_id: String,
    text: String,
) {
    std::thread::spawn(move || {
        let mut cmd = std::process::Command::new("coord");
        // Inject --config immediately after the subcommand name, mirroring the
        // CommandRunner pattern so `coord` finds coordinator.yml.
        cmd.arg("chat-continue");
        if let Some(ref cfg) = config_path {
            cmd.args(["--config", &cfg.to_string_lossy()]);
        }
        cmd.arg(&old_assignment_id);
        // #335: pass the whole message as a single argv entry. `Command` does
        // not go through a shell, so quotes/semicolons/dollar signs are already
        // literal. Splitting on whitespace was actively harmful: tokens
        // beginning with `-` (e.g. user types "claude -p" or "-v for verbose")
        // arrive at Click as unknown options and chat-continue aborts before
        // dispatching, silently dropping the user's turn.
        cmd.arg(&text);
        // #315: capture stderr (only) and surface non-zero exits.  Success
        // is silent; failure logs a single line so a future regression
        // can't disappear into /dev/null the way the original
        // fire-and-forget did.  Failures still surface to the user via
        // the bind-timeout toast even without this log.
        let aid_short: String = old_assignment_id.chars().take(6).collect();
        if let Ok(out) = cmd
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped())
            .output()
        {
            if !out.status.success() {
                let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
                eprintln!(
                    "[chat-continue] FAILED for old_aid={} status={:?}: {}",
                    aid_short,
                    out.status.code(),
                    stderr,
                );
            }
        }
    });
}

/// Extract the `<!-- coord:review ... -->` header from a review body.
/// Returns `None` when the header is missing or malformed.  Tolerates
/// extra whitespace and unknown tokens — only `verdict` is required.
pub(crate) fn parse_coord_review_header(body: &str) -> Option<CoordReviewHeader> {
    let start = body.find("<!--").and_then(|s| {
        // Find a `coord:review` token within the same comment.
        let rest = &body[s..];
        let end = rest.find("-->")?;
        let inside = &rest[4..end];
        let trimmed = inside.trim();
        if !trimmed.starts_with("coord:review") && !trimmed.starts_with("coord: review") {
            return None;
        }
        let body_after = trimmed.split_once("coord:review").map(|(_, b)| b)?;
        Some(body_after.trim().to_string())
    })?;

    let mut header = CoordReviewHeader::default();
    for token in start.split_whitespace() {
        let (k, v) = match token.split_once('=') {
            Some(pair) => pair,
            None => continue,
        };
        let k_lower = k.to_ascii_lowercase();
        match k_lower.as_str() {
            "verdict" => header.verdict = Some(v.to_string()),
            "blocking" => header.blocking = v.parse().ok(),
            "nonblocking" => header.nonblocking = v.parse().ok(),
            "nits" => header.nits = v.parse().ok(),
            "reviewer" => header.reviewer = Some(v.to_string()),
            "assignment" => header.assignment = Some(v.to_string()),
            _ => {}
        }
    }
    if header.verdict.is_some() {
        Some(header)
    } else {
        None
    }
}

/// Parse `gh issue view --json comments` output into a `Vec<SessionSummary>`.
/// Returns entries newest-first.  Comments without coord markers are skipped.
///
/// `assignments` is passed so we can promote the `assignment_type` from the
/// local DB (the comment marker only carries the id, not the type).
///
/// #876: The live Summary tab now uses `build_board_summary_list_view` instead
/// (board-layer data, no GH shellout).  This function is kept for unit tests
/// that verify the comment-parsing logic in isolation.
#[cfg(test)]
pub(crate) fn parse_session_summaries_from_comments(
    comments_json: &serde_json::Value,
    assignments: &[Assignment],
) -> Vec<SessionSummary> {
    let arr = match comments_json.as_array() {
        Some(a) => a,
        None => return Vec::new(),
    };

    let mut entries: Vec<SessionSummary> = Vec::new();

    for comment in arr {
        let body = comment
            .get("body")
            .and_then(|b| b.as_str())
            .unwrap_or("");
        let created_at_str = comment
            .get("createdAt")
            .and_then(|c| c.as_str())
            .unwrap_or("");
        // Parse ISO-8601 "YYYY-MM-DDTHH:MM:SSZ" to a rough numeric timestamp
        // for sort ordering.  We only need relative ordering so a lexicographic
        // parse is fine (strings already sort correctly).
        let created_at_ts: f64 = {
            // Convert "2024-01-15T12:34:56Z" → keep it as-is for sort.
            // Store len as a proxy so newer > older (longer dates are not
            // necessarily later, but ISO-8601 strings sort lexicographically).
            // Better: try parse via a simple epoch conversion.
            parse_iso8601_to_epoch(created_at_str).unwrap_or(0.0)
        };

        // Try to parse a `<!-- coord:review ... -->` header first.
        if let Some(review_header) = parse_coord_review_header(body) {
            let assignment_id = review_header.assignment.clone().unwrap_or_default();
            let machine = review_header.reviewer.clone().unwrap_or_default();
            let verdict = review_header.verdict.clone();

            // Look up the local assignment to get the type.
            let session_type = assignments
                .iter()
                .find(|a| a.id == assignment_id)
                .and_then(|a| a.assignment_type.as_deref())
                .unwrap_or("review")
                .to_string();

            // Extract the prose summary: first non-empty line that isn't the
            // machine-readable header.
            let summary_text = extract_review_summary(body);

            entries.push(SessionSummary {
                assignment_id,
                session_type,
                machine,
                status: "done".to_string(),
                verdict,
                summary_text,
                created_at_ts,
            });
            continue;
        }

        // Try to parse a `<!-- coord:event=completion ... -->` or
        // `<!-- coord:event=failure ... -->` or `<!-- coord:event=advisory ... -->` header.
        if let Some(event_summary) = parse_coord_event_comment(body, assignments, created_at_ts) {
            entries.push(event_summary);
        }
    }

    // Newest-first.
    entries.sort_by(|a, b| {
        b.created_at_ts
            .partial_cmp(&a.created_at_ts)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    entries
}

/// Parse a `<!-- coord:event=... -->` comment into a `SessionSummary`.
/// Returns `None` when the comment doesn't carry a recognised coord event.
/// #876: test-only (called exclusively from `parse_session_summaries_from_comments`).
#[cfg(test)]
pub(crate) fn parse_coord_event_comment(
    body: &str,
    assignments: &[Assignment],
    created_at_ts: f64,
) -> Option<SessionSummary> {
    // Locate the first <!-- coord:... --> marker.
    let marker_start = body.find("<!--")?;
    let rest = &body[marker_start..];
    let end = rest.find("-->")?;
    let inside = rest[4..end].trim();
    if !inside.starts_with("coord:") {
        return None;
    }
    let after_coord = inside.strip_prefix("coord:")?.trim();

    // Parse key=value tokens.
    let mut event = "";
    let mut assignment_id = String::new();
    let mut machine = String::new();
    let mut _exit_code: Option<i32> = None;

    for token in after_coord.split_whitespace() {
        if let Some((k, v)) = token.split_once('=') {
            match k {
                "event" => event = v,
                "assignment" => assignment_id = v.to_string(),
                "machine" => machine = v.to_string(),
                "exit_code" => _exit_code = v.parse().ok(),
                _ => {}
            }
        }
    }

    let status = match event {
        "completion" => "done",
        "failure" => "failed",
        "advisory" => "advisory",
        // Skip briefings, stuck, plan, etc. — not terminal summaries.
        _ => return None,
    };

    // Look up assignment type from local DB.
    let session_type = assignments
        .iter()
        .find(|a| a.id == assignment_id)
        .and_then(|a| a.assignment_type.as_deref())
        .unwrap_or("work")
        .to_string();

    let summary_text = extract_completion_summary(body);

    Some(SessionSummary {
        assignment_id,
        session_type,
        machine,
        status: status.to_string(),
        verdict: None,
        summary_text,
        created_at_ts,
    })
}

/// Extract the prose from a `### Summary` block in a completion comment.
/// Returns the trimmed block text (may be multi-line), or empty string.
/// #876: test-only helper.
#[cfg(test)]
pub(crate) fn extract_completion_summary(body: &str) -> String {
    // Find "### Summary" heading and collect text until the next heading or end.
    let lower = body.to_ascii_lowercase();
    let Some(start) = lower.find("### summary") else {
        return String::new();
    };
    let after = &body[start + "### summary".len()..];
    let text: String = after
        .lines()
        .skip(1) // blank line after heading
        .take_while(|l| !l.starts_with('#'))
        .collect::<Vec<_>>()
        .join("\n");
    text.trim().to_string()
}

/// Extract a one-line prose summary from a review comment body.
/// Skips the `<!-- coord:review ... -->` header line and returns the first
/// non-empty content line.
/// #876: test-only helper.
#[cfg(test)]
pub(crate) fn extract_review_summary(body: &str) -> String {
    // Find "REVIEW_BODY:" marker if present (the structured review format).
    let lower = body.to_ascii_lowercase();
    if let Some(pos) = lower.find("review_body:") {
        let after = &body[pos + "review_body:".len()..];
        // Collect up to "END_REVIEW".
        let end = after
            .to_ascii_lowercase()
            .find("end_review")
            .unwrap_or(after.len());
        let block = &after[..end];
        // Return first non-empty line.
        for line in block.lines() {
            let t = line.trim();
            if !t.is_empty() {
                let truncated: String = t.chars().take(200).collect();
                return truncated;
            }
        }
    }
    // Fallback: first non-empty, non-header line.
    for line in body.lines() {
        let t = line.trim();
        if t.is_empty()
            || t.starts_with("<!--")
            || t.starts_with('#')
            || t.starts_with("**")
        {
            continue;
        }
        let truncated: String = t.chars().take(200).collect();
        return truncated;
    }
    String::new()
}

/// Very small ISO-8601 → Unix epoch converter.  Only handles the
/// `YYYY-MM-DDTHH:MM:SSZ` format that GitHub returns.  Returns `None` on
/// parse failure (the caller falls back to 0.0).
pub(crate) fn parse_iso8601_to_epoch(s: &str) -> Option<f64> {
    // Expected: "2024-01-15T12:34:56Z" (20 chars minimum)
    if s.len() < 19 {
        return None;
    }
    let year: i64 = s[0..4].parse().ok()?;
    let month: i64 = s[5..7].parse().ok()?;
    let day: i64 = s[8..10].parse().ok()?;
    let hour: i64 = s[11..13].parse().ok()?;
    let min: i64 = s[14..16].parse().ok()?;
    let sec: i64 = s[17..19].parse().ok()?;

    // Rough Julian-day-number → seconds calculation (ignores leap seconds).
    // Good enough for sorting; no external crate needed.
    let a: i64 = (14 - month) / 12;
    let y: i64 = year + 4800 - a;
    let m: i64 = month + 12 * a - 3;
    let jdn: i64 =
        day + (153 * m + 2) / 5 + 365 * y + y / 4 - y / 100 + y / 400 - 32045;
    // Unix epoch starts at JDN 2440588.
    let epoch_days = jdn - 2440588;
    let epoch_secs = epoch_days * 86400 + hour * 3600 + min * 60 + sec;
    Some(epoch_secs as f64)
}

pub(crate) fn home_dir() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/root"))
}

pub(crate) fn coord_dir() -> PathBuf {
    home_dir().join(".coord")
}

/// TCP probe on port 7433 with a 150 ms deadline.
/// Hostname resolution is included in the deadline via a background thread.
pub(crate) fn tcp_probe(host: &str, port: u16) -> bool {
    use std::sync::mpsc;
    let host = host.to_string();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let addr_str = format!("{}:{}", host, port);
        let ok = addr_str
            .to_socket_addrs()
            .ok()
            .and_then(|mut it| it.next())
            .map(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(120)).is_ok())
            .unwrap_or(false);
        let _ = tx.send(ok);
    });
    rx.recv_timeout(Duration::from_millis(200)).unwrap_or(false)
}

/// #778: compute staging entries from data already in memory.
///
/// Mirrors `coord.merge_queue.staging_items()` but runs in Rust using the
/// assignments and merge-queue entries already loaded from SQLite (or received
/// in the remote payload).  This keeps the local-DB path working without
/// requiring a `coord serve` daemon.
///
/// Gate checks performed:
/// 1. **Review gate** (when `"review"` is in `pipeline_default_gates`): the
///    work assignment must have a sibling review assignment with
///    `review_verdict = "approve"`.  Items without an approved review are
///    silently excluded (they're still "in pipeline", not "staging").
/// 2. **Smoke gate** (when `"test"` is in `pipeline_default_gates`): the
///    work assignment must carry `test_state = "passed"` or `"skipped"`.
///    Items that fail this gate appear as BLOCKED with reason
///    `"test verdict missing"`.
///
/// #1640 scope note: this local path deliberately does NOT apply the #1479
/// freshness binding the server's `staging_items()` now does.  Deciding that
/// a recorded verdict is *stale* requires the target branch's CURRENT head
/// SHA, which only exists behind a live `gh` call — and this function is the
/// no-daemon fallback whose entire contract is "answer from what is already
/// in memory".  Consequence: without a daemon the staging section can show
/// READY for a verdict `coord merge` refuses as stale.  The daemon-backed
/// path (`merge_staging` in the `/board` payload) is authoritative and does
/// apply the check; prefer it when the two disagree.
///
/// Items already in the merge queue (any state) and items from issues that
/// already have a MERGED queue entry are excluded.
pub(crate) fn compute_staging_local(
    assignments: &[Assignment],
    merge_queue: &[MergeQueueEntry],
    pipeline_default_gates: &[String],
) -> Vec<StagingEntry> {
    let review_gate = pipeline_default_gates.iter().any(|g| g == "review");
    let smoke_gate = pipeline_default_gates.iter().any(|g| g == "test");

    // Fast-lookup sets.
    let queued_aids: std::collections::HashSet<&str> =
        merge_queue.iter().map(|e| e.assignment_id.as_str()).collect();
    // Branch-level dedup (#778): a fix worker dispatched after the original
    // work was enqueued shares the same branch but has a different
    // assignment_id.  Exclude any assignment whose branch is already in the
    // queue so staging doesn't oscillate for the fix.
    let queued_branches: std::collections::HashSet<&str> = merge_queue
        .iter()
        .filter_map(|e| e.branch.as_deref())
        .collect();
    // Issue numbers for which a MERGED queue entry already exists.  We key
    // on issue_number only (no repo cross-check) because in the local path
    // MergeQueueEntry carries repo_github (the GitHub slug) while Assignment
    // carries repo (the coord-local name) — there is no reliable mapping
    // between the two without loading config.  False positives (two repos
    // with the same issue number) are extremely rare and the penalty is only
    // a temporarily missing staging row, so this approximation is acceptable.
    let merged_issue_numbers: std::collections::HashSet<u64> = merge_queue
        .iter()
        .filter(|e| e.state == "merged")
        .filter_map(|e| e.issue_number)
        .collect();

    // Build a quick look-up: assignment_id → list of (review_verdict) for
    // reviews that point to it.  We need this to check the review gate.
    // Key: work assignment_id; Value: true when at least one "approve" exists.
    let mut approved_aids: std::collections::HashSet<String> =
        std::collections::HashSet::new();
    for a in assignments {
        if a.assignment_type.as_deref() != Some("review") {
            continue;
        }
        if a.review_verdict.as_deref() != Some("approve") {
            continue;
        }
        if let Some(ref of_aid) = a.review_of_assignment_id {
            approved_aids.insert(of_aid.clone());
        }
    }

    let mut result: Vec<StagingEntry> = Vec::new();

    for a in assignments {
        if a.assignment_type.as_deref() != Some("work") {
            continue;
        }
        if a.status != "done" {
            continue;
        }
        let branch = match a.branch.as_deref() {
            Some(b) if !b.is_empty() => b.to_string(),
            _ => continue,
        };

        // Skip items already in the queue (by assignment_id or branch).
        // Branch-level dedup catches fix workers that share a branch with an
        // already-queued original work assignment (#778).
        if queued_aids.contains(a.id.as_str())
            || a.branch
                .as_deref()
                .map(|b| queued_branches.contains(b))
                .unwrap_or(false)
        {
            continue;
        }

        // Skip items from issues already MERGED.
        if merged_issue_numbers.contains(&a.issue_number) {
            continue;
        }

        // Review gate.
        if review_gate && !approved_aids.contains(&a.id) {
            continue; // not approved → not a staging item
        }

        // Smoke gate.
        let (status, reason) = if smoke_gate
            && !matches!(a.test_state.as_deref(), Some("passed") | Some("skipped"))
        {
            ("blocked".to_string(), Some("test verdict missing".to_string()))
        } else {
            ("ready".to_string(), None)
        };

        result.push(StagingEntry {
            assignment_id: a.id.clone(),
            repo_name: a.repo.clone(),
            issue_number: a.issue_number as i64,
            issue_title: a.issue_title.clone(),
            branch,
            status,
            reason,
        });
    }

    result
}

/// #2895: the ONLY board read path — coord-tui is daemon-required.
///
/// Before this, a missing `board_service` fell through to opening
/// `~/.coord/coord.db` read-only and hand-rolling the board projection SQL
/// (raw column list and all) in Rust.  That path could not follow the store
/// service onto Postgres (#2894 Phase D), it was invisible to `coord/sql.py`'s
/// dialect seam and to #2768's ratchet (both AST walks over *Python*), and on
/// the daemon host it also read/wrote a file `coord serve` held open in WAL
/// mode, behind the daemon's back.
///
/// So it is gone.  No service resolves ⇒ a named, actionable error — NOT a
/// silently blank board, which is precisely what the old path produced when
/// its read-only SQLite open returned `Err`.
pub(crate) fn load_data() -> BoardData {
    match resolve_board_service() {
        Some((url, token)) => load_data_remote(&url, token.as_deref()),
        None => BoardData {
            load_error: Some(NO_BOARD_SERVICE_ERROR.to_string()),
            ..BoardData::default()
        },
    }
}

/// #584: run the machine reachability/health probes and assemble the final
/// [`BoardData`] from data already gathered by EITHER the local SQLite path
/// ([`load_data`]) or the remote `coord serve` /board path
/// ([`load_data_remote`]).
///
/// This is the shared tail of `load_data`: it spawns the per-machine TCP +
/// `/health` probes concurrently, derives `active_count` and the local-machine
/// name, and packs everything into `BoardData`.  Both callers feed it identical
/// inputs, so the probe + assembly behaviour is byte-identical regardless of
/// where the rows came from.
#[allow(clippy::too_many_arguments)]
pub(crate) fn assemble_board_data(
    assignments: Vec<Assignment>,
    machine_rows: Vec<(String, String, Vec<String>)>,
    open_issues: Vec<OpenIssue>,
    merge_queue: Vec<MergeQueueEntry>,
    merge_plan: Vec<PlannedMergeEntry>,
    proposals: Vec<Proposal>,
    plans: std::collections::HashMap<String, PlanData>,
    pipeline_default_gates: Vec<String>,
    pipeline_tracked_labels: Vec<String>,
    pipeline_repos: Vec<(String, String)>,
    pipeline_repo_run_cmds: std::collections::HashMap<String, String>,
    pipeline_repo_paths: std::collections::HashMap<String, String>,
    pipeline_acceptance_routes: std::collections::HashMap<String, Vec<String>>,
    pipeline_require_plan: bool,
    merge_staging: Vec<StagingEntry>,
    pipeline_models: Option<PipelineModels>,
    issue_stage_projection: Vec<IssueStageProjection>,
    milestone_work_orders: Vec<MilestoneWorkOrder>,
    epic_children: Vec<EpicChildren>,
    plan_roster: Vec<PlanRosterEntry>,
    plan_roster_supported: bool,
    goal_header: GoalHeader,
    audit_recent_count: u64,
    escalations: Vec<EscalationEntry>,
    fleet_health: FleetHealthBlock,
    drive_queue: Vec<BoardDriveQueueEntry>,
    roll_pending: Option<RollPending>,
    approved_submissions: Vec<ApprovedSubmission>,
) -> BoardData {
    // ── Machine reachability probes + health fetches ──────────────────────
    // Probe using the Tailscale host (fixes #121: machine name ≠ Tailscale hostname).
    // Spawn all TCP probes AND HTTP /health fetches concurrently so total
    // wall-clock time is bounded by the slowest machine, not N × timeout.
    let probes: Vec<(
        String,
        String,
        Vec<String>,
        std::sync::mpsc::Receiver<bool>,
        std::sync::mpsc::Receiver<Result<MachineHealthResult, String>>,
    )> = machine_rows
        .iter()
        .map(|(name, host, repos)| {
            use std::sync::mpsc;
            let h = host.clone();
            let (tcp_tx, tcp_rx) = mpsc::channel();
            std::thread::spawn(move || {
                let _ = tcp_tx.send(tcp_probe(&h, 7433));
            });
            let health_rx = spawn_machine_health(host, 7433);
            (name.clone(), host.clone(), repos.clone(), tcp_rx, health_rx)
        })
        .collect();

    // #2572: paired 1:1 with `machines` below — each machine's own live
    // `agent_venv` reading, captured alongside the `/health` fetch that's
    // already happening for `version`/`worktree_bytes`. Folded into
    // `fleet_health` after the loop (`merge_live_agent_venv_health`) rather
    // than inline here, so the merge policy stays one pure, testable
    // function instead of tangled into this probe-collection closure.
    let mut live_agent_venv: Vec<(String, Option<AgentVenvHealth>)> = Vec::new();
    let machines: Vec<Machine> = probes
        .into_iter()
        .map(|(name, host, repos, tcp_rx, health_rx)| {
            let tcp_reachable = tcp_rx
                .recv_timeout(Duration::from_millis(250))
                .unwrap_or(false);
            // Health fetch has a 2 s connect + read timeout baked in; we wait
            // up to 2.1 s here so we never block past the in-flight deadline.
            let health = health_rx
                .recv_timeout(Duration::from_millis(2100))
                .ok()
                .and_then(|r| r.ok());
            let reachable = tcp_reachable || health.is_some();
            let active_count = assignments
                .iter()
                .filter(|a| a.machine == name && a.status == "running")
                .count();
            live_agent_venv.push((name.clone(), health.as_ref().and_then(|h| h.agent_venv.clone())));
            Machine {
                name,
                host,
                reachable,
                active_count,
                repos,
                version: health.as_ref().map(|h| h.version.clone()),
                worktree_bytes: health.as_ref().map(|h| h.worktree_bytes).unwrap_or(0),
            }
        })
        .collect();

    // #2572: an `agent_venv` CRIT must be visible on the always-on status
    // bar (`fleet_health_status_bar_segment`, #1631) even when it is
    // learned from THIS live probe rather than `coord serve`'s own snapshot
    // — see `merge_live_agent_venv_health`'s doc comment for the incident
    // (#2569/#2570) this closes: a daemon-computed `fleet_health` that is
    // empty (the local-SQLite read path, by design — see `load_data`'s own
    // comment on this field) or simply stale must never be the only source
    // for a signal this load-bearing.
    let now_epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let fleet_health = merge_live_agent_venv_health(fleet_health, &live_agent_venv, now_epoch);

    // ── Determine which machine is local ──────────────────────────────────
    // Match the OS hostname against the `host` column in the machines table.
    // Hostnames are case-insensitive (DNS): the OS hostname is often mixed-case
    // (e.g. `john-HP-EliteBook-830-G7-Notebook-PC`) while coordinator.yml stores
    // it lower-case, so a case-sensitive compare never resolves the local
    // machine (#467 interactive launch broke on exactly this).
    let local_hostname = gethostname::gethostname().into_string().unwrap_or_default();
    let local_machine = machine_rows
        .iter()
        .find(|(_, host, _)| host.eq_ignore_ascii_case(&local_hostname))
        .map(|(name, _, _)| name.clone())
        .unwrap_or_default();

    // ── Client-side milestone join for merge_queue ────────────────────────
    // For each merge-queue entry, look up the milestone from open_issues on
    // (coord_repo_name, issue_number).  pipeline_repos maps coord repo name →
    // github slug; we reverse it to map entry.repo_github → coord repo name,
    // then scan open_issues for a matching row.
    let merge_queue: Vec<MergeQueueEntry> = merge_queue
        .into_iter()
        .map(|mut entry| {
            if let Some(issue_num) = entry.issue_number {
                let coord_repo = pipeline_repos
                    .iter()
                    .find(|(_, gh)| *gh == entry.repo_github)
                    .map(|(name, _)| name.as_str());
                if let Some(cr) = coord_repo {
                    if let Some(oi) = open_issues
                        .iter()
                        .find(|oi| oi.number == issue_num && oi.repo_name == cr)
                    {
                        entry.milestone_title = oi.milestone_title.clone();
                    }
                }
            }
            entry
        })
        .collect();

    BoardData {
        local_machine,
        assignments,
        open_issues,
        machines,
        merge_queue,
        merge_plan,
        proposals,
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
        issue_stage_projection,
        milestone_work_orders,
        epic_children,
        plan_roster,
        plan_roster_supported,
        goal_header,
        audit_recent_count,
        escalations,
        fleet_health,
        drive_queue,
        roll_pending,
        approved_submissions,
        // #2895: reaching `assemble_board_data` at all means a board was
        // successfully fetched — the hard "no board service" error is raised
        // by `load_data`, upstream of here.
        load_error: None,
    }
}

#[cfg(any(test, feature = "test-support"))]
thread_local! {
    /// #1087: test-only per-thread override for [`resolve_board_service`],
    /// set via [`set_test_board_service`]. Thread-local rather than a
    /// process env var deliberately: `resolve_board_service` runs
    /// synchronously on the *caller's* thread (before any network thread
    /// is spawned), so a thread-local override is visible exactly where a
    /// test needs it and invisible to every other test running
    /// concurrently on a different OS thread. `cargo test`'s default
    /// multi-threaded harness runs ~25 other Audit-panel tests
    /// (`tests.rs`, `SidebarView::Audit`) that also nudge
    /// `spawn_audit_fetch` via `run_periodic_work()` — a process-global
    /// `COORD_SERVICE_URL` mutation would let those transiently observe a
    /// mock URL they never opted into, an intermittent-failure trap this
    /// sidesteps entirely.
    static TEST_BOARD_SERVICE_OVERRIDE: std::cell::RefCell<Option<(String, Option<String>)>> =
        const { std::cell::RefCell::new(None) };
}

/// #1087: point `resolve_board_service()` at `url` (with optional bearer
/// `token`) for the remainder of the calling thread, so a test can exercise
/// `spawn_audit_fetch`'s real network path — e.g. against a
/// [`super::fixtures::MockBoardService`] — instead of the hard-coded
/// `AuditFetchOutcome::NoBoardService` every test build produced before
/// this seam existed. Returns an RAII guard that clears the override on
/// drop (including on test panic/assertion failure), so a `cargo test`
/// worker thread reused by a later, unrelated test never inherits stale
/// state.
///
/// `pub` (not `pub(crate)`) and re-exported from [`super::fixtures`]
/// (`coord_tui::fixtures::set_test_board_service` under the `test-support`
/// feature) so the same seam is available to an external integration-test
/// crate — e.g. a future sealed acceptance slice for #1087-shaped coverage
/// — exactly like `make_test_app` and friends already are.
#[cfg(any(test, feature = "test-support"))]
pub fn set_test_board_service(
    url: impl Into<String>,
    token: Option<String>,
) -> TestBoardServiceGuard {
    TEST_BOARD_SERVICE_OVERRIDE.with(|cell| *cell.borrow_mut() = Some((url.into(), token)));
    TestBoardServiceGuard(())
}

/// RAII guard returned by [`set_test_board_service`]; see its doc comment.
#[cfg(any(test, feature = "test-support"))]
pub struct TestBoardServiceGuard(());

#[cfg(any(test, feature = "test-support"))]
impl Drop for TestBoardServiceGuard {
    fn drop(&mut self) {
        TEST_BOARD_SERVICE_OVERRIDE.with(|cell| *cell.borrow_mut() = None);
    }
}

/// #584: resolve the board service URL + optional bearer token.
///
/// Precedence, highest first:
///
/// 1. environment — `COORD_SERVICE_URL` + `COORD_TOKEN`;
/// 2. `~/.coord/client.toml` — TOML keys `board_service` and optional `token`;
/// 3. **#2895, the daemon host** — `~/.coord/serve_token` exists, so
///    `coord serve` runs here: use `http://127.0.0.1:{DAEMON_PORT}` with that
///    token.
///
/// Rung 3 exists because #2895 deleted the local-SQLite fallback, and the
/// daemon host is exactly the machine that had no `client.toml` (it did not
/// need one — the local DB *was* canonical). Auto-detecting keeps it working
/// with zero config while staying scoped to this process: it does NOT make
/// the host's Python side think it is a thin client, which dropping a
/// `client.toml` there would (`_thin_client_local_board_guard`'s #615
/// warnings, `daemon_reroute_target` bouncing `coord merge`/`diagnose`/
/// `housekeeping` over HTTP to its own daemon). And because the resolved URL
/// is loopback, [`is_remote_board_service`] still reports `false` there, so
/// the TUI keeps auto-running the host-side `coord notify`/`coord sync`.
///
/// Returns `None` only when all three come up empty — a hard error now, see
/// [`NO_BOARD_SERVICE_ERROR`].
///
/// Any trailing `/` is stripped from the URL so callers can append `/board`.
pub(crate) fn resolve_board_service() -> Option<(String, Option<String>)> {
    // #1087: a test that opted in via `set_test_board_service` wins over the
    // hard test-mode short-circuit below — this is the seam that lets the
    // in-crate suite exercise `spawn_audit_fetch`'s real network path
    // end-to-end against a mock server. Checked first and scoped to this
    // thread only; see the doc comment on `set_test_board_service` for why
    // this isn't a `COORD_SERVICE_URL` env var instead.
    #[cfg(any(test, feature = "test-support"))]
    if let Some(value) = TEST_BOARD_SERVICE_OVERRIDE.with(|cell| cell.borrow().clone()) {
        return Some(value);
    }

    // In the test binary, treat the board service as absent.  This prevents
    // `record_test_verdict_remote`, `load_board_data_from_service`, and
    // `fetch_remote_config_to_cache` from firing real HTTP requests against
    // the production daemon during `cargo test`.  The `OnceLock` cache in
    // `is_remote_board_service()` would otherwise latch a developer-machine
    // value of `true` for the entire test process.
    //
    // #1039: also gated on `feature = "test-support"`, not just `cfg(test)`
    // — the sealed `tests/acceptance/**` suite (`tui/tests/acceptance.rs`,
    // the #1042 seam) is a separate integration-test binary, so the library
    // it links against is compiled *without* `cfg(test)` (only the
    // top-level test binary gets that cfg). Without this, a TuiDriver test
    // that navigates to the Audit panel would arm `spawn_audit_fetch` for
    // real against whatever `~/.coord/client.toml` happens to exist on the
    // machine running the suite — exactly the flaky/slow real-network trap
    // this function exists to avoid.  `cargo test --features test-support`
    // enables the feature crate-wide for that whole build (lib + every test
    // binary sharing the invocation), so this stays reliable there too.
    //
    // #1087: this is *not* bypassed by checking `COORD_SERVICE_URL` below —
    // a real board service is opted into via `set_test_board_service` above
    // instead, precisely to avoid a process-global env var a concurrently
    // running unrelated test could observe.
    #[cfg(any(test, feature = "test-support"))]
    return None;

    // Env first.
    #[allow(unreachable_code)]
    if let Ok(url) = std::env::var("COORD_SERVICE_URL") {
        let url = url.trim();
        if !url.is_empty() {
            let token = std::env::var("COORD_TOKEN")
                .ok()
                .map(|t| t.trim().to_string())
                .filter(|t| !t.is_empty());
            return Some((url.trim_end_matches('/').to_string(), token));
        }
    }

    // Then ~/.coord/client.toml.  A missing/unparsable file, or one with no
    // usable `board_service`, falls through to the daemon-host rung below
    // rather than short-circuiting to None (#2895).
    if let Some(found) = board_service_from_client_toml() {
        return Some(found);
    }

    // #2895: finally, the daemon host itself — `coord serve` writes
    // `~/.coord/serve_token`, so its presence is the marker for "the board
    // daemon lives on this machine".  See this function's doc comment for why
    // this is preferable to shipping a `client.toml` here.
    let token = std::fs::read_to_string(coord_dir().join("serve_token"))
        .ok()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())?;
    Some((format!("http://127.0.0.1:{DAEMON_PORT}"), Some(token)))
}

/// Rung 2 of [`resolve_board_service`]: `~/.coord/client.toml`'s
/// `board_service` (+ optional `token`).  Split out so a missing file, a
/// parse failure, or an empty/absent key all return `None` *to the caller*
/// (which then tries the daemon-host rung) instead of `?`-returning out of
/// `resolve_board_service` entirely.
fn board_service_from_client_toml() -> Option<(String, Option<String>)> {
    let text = std::fs::read_to_string(coord_dir().join("client.toml")).ok()?;
    let parsed: toml::Value = toml::from_str(&text).ok()?;
    let url = parsed.get("board_service")?.as_str()?.trim();
    if url.is_empty() {
        return None;
    }
    let token = parsed
        .get("token")
        .and_then(|v| v.as_str())
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty());
    Some((url.trim_end_matches('/').to_string(), token))
}

/// #584: true when the board service is on **another machine**, i.e. this
/// coord-tui is a genuine thin client.  Cached for the process lifetime — the
/// bootstrap (env / client.toml / daemon-host marker) doesn't change within a
/// session.  Thin clients must NOT auto-run host-side control commands
/// (`coord notify`, `coord sync`): they'd shell out locally against the
/// wrong/absent DB and only produce error toasts.  Routing these through the
/// daemon is the write-path story (#590).
///
/// #2895: "board service configured" and "thin client" used to be the same
/// predicate, which stopped being true once [`resolve_board_service`] started
/// auto-detecting the daemon host — a box that talks HTTP to its own loopback
/// daemon is still the host, and `coord notify`/`coord sync` are still
/// supposed to run there.  So the loopback case reports `false`.
pub(crate) fn is_remote_board_service() -> bool {
    use std::sync::OnceLock;
    static CACHE: OnceLock<bool> = OnceLock::new();
    *CACHE.get_or_init(|| {
        resolve_board_service()
            .map(|(url, _)| !url_is_loopback(&url))
            .unwrap_or(false)
    })
}

/// Whether *url*'s host is a loopback address — i.e. the board daemon it
/// points at runs on this same machine.  See [`is_remote_board_service`].
pub(crate) fn url_is_loopback(url: &str) -> bool {
    // Strip scheme, then path, then port, then IPv6 brackets.
    let rest = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
    let authority = rest.split(['/', '?', '#']).next().unwrap_or(rest);
    // Drop userinfo if present (`user:pass@host:port`).
    let hostport = authority.rsplit_once('@').map(|(_, h)| h).unwrap_or(authority);
    let host = if let Some(end) = hostport.find(']') {
        // Bracketed IPv6 literal: `[::1]` / `[::1]:7435`.
        hostport[..end].trim_start_matches('[')
    } else {
        hostport.split(':').next().unwrap_or(hostport)
    };
    // Parse as an IP so the whole 127.0.0.0/8 range (and `::1`) is covered
    // without a prefix match — `127.0.0.1.example.com` is a hostname on some
    // OTHER machine, and `starts_with("127.")` happily called it loopback.
    if let Ok(ip) = host.parse::<std::net::IpAddr>() {
        return ip.is_loopback();
    }
    host.eq_ignore_ascii_case("localhost")
}

/// #1012: shared JSON-POST-and-parse call to the board daemon, factored out
/// of `record_test_verdict_remote` (#590) so every subsequent direct-POST
/// mutation — starting with `apply_issue_labels_remote` — shares one
/// ureq agent/timeout/auth call site instead of hand-rolling it per
/// endpoint. Callers already resolved `url`/`token` via
/// [`resolve_board_service`] (they need the `Option` to decide whether to
/// fall back to a `coord` subprocess at all, so resolving here too would
/// just duplicate that branch).
///
/// `path` is appended verbatim to `url` (e.g. `"/issue-label"`); `body` is
/// serialized as the request JSON. Returns the parsed JSON response, or a
/// display-ready error string on any network/parse/non-2xx failure — `ureq`
/// (built without the `json` feature crate-wide here) already includes the
/// status code and a body snippet in its `Display` for non-2xx responses.
///
/// #1945: every call carries `X-Coord-Client`/`X-Coord-Client-Version` so the
/// daemon's deprecated-RPC-route telemetry can name coord-tui by its actual
/// running version when this hits a route `coord.serve_app`'s
/// `RPC_SUPERSEDED_BY_RESOURCE` marks deprecated (e.g. `apply_issue_labels_remote`'s
/// `"/issue-label"`) — evidence for retirement instead of a belief that
/// every locally-built binary in the fleet has upgraded.
pub(crate) fn post_daemon_json(
    url: &str,
    token: Option<&str>,
    path: &str,
    body: &serde_json::Value,
) -> Result<serde_json::Value, String> {
    let body_str = serde_json::to_string(body).map_err(|e| format!("{e}"))?;
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(30))
        .build();
    let mut req = agent
        .post(&format!("{url}{path}"))
        .set("Content-Type", "application/json")
        .set("X-Coord-Client", "coord-tui")
        .set("X-Coord-Client-Version", env!("CARGO_PKG_VERSION"));
    if let Some(t) = token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    let resp = req.send_string(&body_str).map_err(|e| format!("{e}"))?;
    let text = resp.into_string().map_err(|e| format!("{e}"))?;
    serde_json::from_str(&text).map_err(|e| format!("{path}: invalid JSON response ({e}): {text}"))
}

/// #584: a thin client has no local `coordinator.yml`.  Fetch it from the daemon
/// (`GET /config`) once at startup and cache it to
/// `~/.coord/coordinator.remote.yml`, so the "coordinator.yml not found" status
/// warning clears and any `coord` subcommand has a config to point at (the
/// daemon owns the single source — #591).  Returns the cached path on success,
/// `None` on any network/IO error (the caller then leaves config_path as-is).
pub(crate) fn fetch_remote_config_to_cache() -> Option<std::path::PathBuf> {
    let (url, token) = resolve_board_service()?;
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(5))
        .build();
    let mut req = agent.get(&format!("{url}/config"));
    if let Some(t) = token.as_deref() {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    let body = req.call().ok()?.into_string().ok()?;
    let dir = coord_dir();
    std::fs::create_dir_all(&dir).ok()?;
    let path = dir.join("coordinator.remote.yml");
    std::fs::write(&path, body).ok()?;
    Some(path)
}

/// #1563: fetch the set of paused machine names, background-thread version
/// of [`super::read_paused_machines`] for use in `refresh()`'s periodic
/// re-arm — mirrors `spawn_remote_tmux_sessions_fetch`/
/// `spawn_drive_sessions_fetch`'s "spawn a thread, hand back a receiver"
/// shape so the caller never blocks the render loop on the (possibly
/// remote) read.
///
/// On a thin client (board service configured) this reads through the
/// daemon's `GET /pause` instead of the local
/// `~/.coord/paused_machines.json` — mirrors
/// `coord.machine_pause.paused_set()`'s daemon-aware routing on the Python
/// side. Without this, a thin-client TUI's `read_paused_machines()` rescan
/// kept reading a copy of the file `coord pause` no longer writes once a
/// board service is configured (that now goes straight to the daemon, see
/// `coord/machine_pause.py`), so the TUI's badge/dispatch-candidate filter
/// silently reverted an operator's pause a few seconds after they set it.
///
/// `resolve_board_service()` is called HERE, on the caller's thread, before
/// spawning — not inside the spawned closure. `resolve_board_service`'s
/// test-mode override (`set_test_board_service`) is a thread-local
/// specifically so a mock server opted into by one test is invisible to
/// concurrently-running unrelated tests (see its doc comment); resolving
/// inside a freshly spawned `std::thread::spawn` closure would run on a
/// thread that never saw the caller's thread-local and would always
/// observe `None`. Mirrors `spawn_audit_fetch`'s identical
/// resolve-before-spawn shape.
pub(crate) fn spawn_paused_machines_fetch() -> std::sync::mpsc::Receiver<PausedFetch> {
    let (tx, rx) = std::sync::mpsc::channel();
    let resolved = resolve_board_service();
    std::thread::spawn(move || {
        let _ = tx.send(fetch_paused_machines_resolved(resolved));
    });
    rx
}

/// #1862: paired result of a paused-machine fetch. `paused` is the full
/// effective paused set (explicit `coord pause` UNION quiet-hours-covered
/// machines — unchanged from pre-#1862). `quiet` is the subset of `paused`
/// that's paused *specifically* because a `quiet_hours` window covers the
/// current moment, so the sidebar badge (`mod.rs`'s `machines_list`) can
/// tell a quiet-paused machine apart from a hand-paused one without a
/// second routing check — mirroring `coord.machine_pause.describe_pause_state`
/// on the Python side. `quiet` is always a subset of `paused`.
///
/// Only the daemon's `GET /pause` (thin-client path, `coord.serve_app.get_pause`)
/// can populate `quiet` — the local `~/.coord/paused_machines.json` file
/// has no notion of quiet hours (that lives in `coordinator.yml`, which
/// this process doesn't parse), so the local-file fallback in
/// [`fetch_paused_machines_resolved`] always reports `quiet` empty. That's
/// a pre-existing gap (the base `paused` fold from quiet hours is ALSO
/// daemon-only, see the comments on `read_paused_machines`/
/// `spawn_paused_machines_fetch`), not a regression introduced here.
///
/// #2101: `cordoned` is the third such subset — machines under a *release
/// cordon*, i.e. draining so `coord release propagate` can roll them onto a
/// released version. Same routing effect as a pause (which is why it is in
/// `paused` too), different owner: an operator's `coord unpause` does not
/// clear it and it lifts itself the moment the roll lands. Rendering it as
/// `[PAUSED]` would tell an operator that a human stopped that machine and
/// that `coord unpause` is the fix — both wrong. Like `quiet`, only the
/// daemon's `GET /pause` can populate it.
///
/// #2147: `quiet_hours` is a FOURTH, independent field — `{machine:
/// QuietHoursWindow}` for every machine that has a window, whether or not
/// it covers *now*. Unlike `quiet` (the currently-covered subset), this is
/// what the sidebar badge's schedule text and the "Set quiet hours…" dialog's
/// pre-fill read from. Deliberately never folded into `quiet`/`paused`
/// membership — see `poll_paused_machines`'s optimistic-update comment for
/// why a machine having a window does not mean it is paused right now.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct PausedFetch {
    pub(crate) paused: std::collections::HashSet<String>,
    pub(crate) quiet: std::collections::HashSet<String>,
    pub(crate) cordoned: std::collections::HashSet<String>,
    pub(crate) quiet_hours: std::collections::HashMap<String, QuietHoursWindow>,
}

/// #2147: one machine's effective quiet-hours schedule, parsed from the
/// daemon's `GET /pause` `quiet_hours` map (`{start, end, tz, source}` —
/// `coord.machine_pause.local_effective_quiet_hours` on the Python side).
/// `start`/`end` are `"HH:MM"` strings, `tz` an IANA zone name, `source` is
/// `"store"` (operator-set via `coord quiet-hours`/this dialog) or
/// `"config"` (a `coordinator.yml` `quiet_hours:` block) — mirrors
/// `coord.machine_pause.SOURCE_STORE`/`SOURCE_CONFIG` exactly so the badge
/// and dialog never need a third vocabulary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct QuietHoursWindow {
    pub(crate) start: String,
    pub(crate) end: String,
    pub(crate) tz: String,
    pub(crate) source: String,
}

/// #1563: the synchronous half of [`spawn_paused_machines_fetch`] — daemon
/// `GET /pause` when a board service is configured, otherwise the local
/// file via [`super::read_paused_machines`]. Fails soft (empty set) on any
/// network/parse error, matching `paused_set()`'s documented fail-open
/// contract for reads (a pause that can't be confirmed should never wedge
/// the read side the way it must fail loudly on the write side).
///
/// Safe to call directly on any thread (unlike `spawn_paused_machines_fetch`,
/// this resolves the board service itself rather than expecting it
/// pre-resolved). Only a test convenience today — every production call
/// site goes through `spawn_paused_machines_fetch` so the render loop never
/// blocks on the (possibly remote) read — hence `#[cfg(test)]` rather than
/// `pub(crate)` unconditionally, to avoid an unused-in-release warning.
#[cfg(test)]
pub(crate) fn fetch_paused_machines() -> PausedFetch {
    fetch_paused_machines_resolved(resolve_board_service())
}

/// Shared fetch body for [`fetch_paused_machines`] /
/// [`spawn_paused_machines_fetch`], parameterized on an already-resolved
/// board service so the latter can resolve on the caller's thread and pass
/// the result into its spawned closure (see that function's doc comment).
fn fetch_paused_machines_resolved(resolved: Option<(String, Option<String>)>) -> PausedFetch {
    let Some((url, token)) = resolved else {
        return PausedFetch {
            paused: super::read_paused_machines(),
            quiet: std::collections::HashSet::new(),
            cordoned: std::collections::HashSet::new(),
            // #2147: the local `~/.coord/paused_machines.json` file has no
            // notion of quiet-hours windows (that lives in `coordinator.yml`
            // / the daemon's store) — same gap `quiet`/`cordoned` document
            // above, not a regression here.
            quiet_hours: std::collections::HashMap::new(),
        };
    };
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(5))
        .build();
    let mut req = agent.get(&format!("{url}/pause"));
    if let Some(t) = token.as_deref() {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    (|| -> Option<PausedFetch> {
        let text = req.call().ok()?.into_string().ok()?;
        let v: serde_json::Value = serde_json::from_str(&text).ok()?;
        fn str_set(v: &serde_json::Value, key: &str) -> std::collections::HashSet<String> {
            v.get(key)
                .and_then(|x| x.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|x| x.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .unwrap_or_default()
        }
        // #2147: `{machine: {start, end, tz, source}}` — absent entirely on
        // a pre-#2146 daemon, and an individual malformed/incomplete record
        // (an older daemon's differently-shaped row, a half-written key) is
        // dropped rather than failing the whole map, exactly like
        // `machine_pause._stored_quiet_windows`'s per-row `continue` on the
        // Python side: this is read on every sidebar redraw, so one bad row
        // must not blank every other machine's schedule.
        fn quiet_hours_map(
            v: &serde_json::Value,
        ) -> std::collections::HashMap<String, QuietHoursWindow> {
            v.get("quiet_hours")
                .and_then(|x| x.as_object())
                .map(|obj| {
                    obj.iter()
                        .filter_map(|(name, row)| {
                            let start = row.get("start")?.as_str()?.to_string();
                            let end = row.get("end")?.as_str()?.to_string();
                            let tz = row.get("tz")?.as_str()?.to_string();
                            let source = row
                                .get("source")
                                .and_then(|s| s.as_str())
                                .unwrap_or("config")
                                .to_string();
                            Some((
                                name.clone(),
                                QuietHoursWindow { start, end, tz, source },
                            ))
                        })
                        .collect()
                })
                .unwrap_or_default()
        }
        // `paused` must be present to trust the response at all (matches
        // the pre-#1862 contract exactly); `quiet`, `cordoned` (#2101) and
        // `quiet_hours` (#2147) are newer, optional fields — their absence
        // (an older daemon) degrades to "no such distinction available"
        // rather than discarding the whole response.
        v.get("paused")?;
        Some(PausedFetch {
            paused: str_set(&v, "paused"),
            quiet: str_set(&v, "quiet"),
            cordoned: str_set(&v, "cordoned"),
            quiet_hours: quiet_hours_map(&v),
        })
    })()
    .unwrap_or_default()
}

/// #584: parse the pipeline_* keys out of a `board_meta` map fetched over the
/// /board wire.
///
/// Returns `(default_gates, tracked_labels, repos, require_plan,
/// repo_run_cmds, repo_paths, pipeline_models, acceptance_routes)` with the
/// documented fallbacks when a key is missing or unparseable: gates default
/// to `["review", "merge"]`, tracked labels to `["coord"]`, and everything
/// else to empty/`false`/`None`.  Repos are `(coord_name, github_slug)`
/// pairs.
///
/// #2895: this used to have a SQLite twin (`load_pipeline_meta`) reading the
/// same keys straight out of `board_meta` for coord-tui's local-DB path.
/// That path is gone, so this is the only reader and there is no longer a
/// pair of parsers to keep field-for-field in sync.
pub(crate) fn parse_pipeline_meta_from_map(
    meta: &std::collections::HashMap<String, String>,
) -> (
    Vec<String>,
    Vec<String>,
    Vec<(String, String)>,
    bool,
    std::collections::HashMap<String, String>,
    std::collections::HashMap<String, String>,
    Option<PipelineModels>,
    std::collections::HashMap<String, Vec<String>>,
) {
    fn read_map(
        meta: &std::collections::HashMap<String, String>,
        key: &str,
    ) -> std::collections::HashMap<String, String> {
        meta.get(key)
            .and_then(|v| serde_json::from_str::<serde_json::Value>(v).ok())
            .and_then(|val| match val {
                serde_json::Value::Object(map) => Some(
                    map.into_iter()
                        .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
                        .collect(),
                ),
                _ => None,
            })
            .unwrap_or_default()
    }

    // #1151: repo_name -> list of route `match` globs (only present for
    // repos with a routed acceptance driver, #1125).
    fn read_map_of_lists(
        meta: &std::collections::HashMap<String, String>,
        key: &str,
    ) -> std::collections::HashMap<String, Vec<String>> {
        meta.get(key)
            .and_then(|v| serde_json::from_str::<serde_json::Value>(v).ok())
            .and_then(|val| match val {
                serde_json::Value::Object(map) => Some(
                    map.into_iter()
                        .filter_map(|(k, v)| {
                            let list = v.as_array()?;
                            let strs: Vec<String> = list
                                .iter()
                                .filter_map(|e| e.as_str().map(str::to_string))
                                .collect();
                            Some((k, strs))
                        })
                        .collect(),
                ),
                _ => None,
            })
            .unwrap_or_default()
    }

    let default_gates: Vec<String> = meta
        .get("pipeline_default_gates")
        .and_then(|v| serde_json::from_str::<Vec<String>>(v).ok())
        .unwrap_or_else(|| vec!["review".to_string(), "merge".to_string()]);

    let tracked_labels: Vec<String> = meta
        .get("pipeline_tracked_labels")
        .and_then(|v| serde_json::from_str::<Vec<String>>(v).ok())
        .unwrap_or_else(|| vec!["coord".to_string()]);

    let repos: Vec<(String, String)> = meta
        .get("pipeline_repos")
        .and_then(|v| serde_json::from_str::<serde_json::Value>(v).ok())
        .and_then(|val| match val {
            serde_json::Value::Object(map) => Some(
                map.into_iter()
                    .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
                    .collect(),
            ),
            _ => None,
        })
        .unwrap_or_default();

    let require_plan: bool = meta
        .get("pipeline_require_plan")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    let repo_run_cmds = read_map(meta, "pipeline_repo_run_cmds");
    let repo_paths = read_map(meta, "pipeline_repo_paths");

    // #803: model config snapshot — None when the daemon is pre-#803.
    let pipeline_models: Option<PipelineModels> = meta
        .get("pipeline_models")
        .and_then(|v| serde_json::from_str::<PipelineModels>(v).ok());

    // #1151: repo_name → route match globs — empty when the daemon predates
    // this field.
    let acceptance_routes = read_map_of_lists(meta, "pipeline_acceptance_routes");

    (
        default_gates,
        tracked_labels,
        repos,
        require_plan,
        repo_run_cmds,
        repo_paths,
        pipeline_models,
        acceptance_routes,
    )
}

/// #1336: last-known `/board` `(ETag, raw body)` for conditional GETs — see
/// `load_data_remote`.  Process-wide (one daemon per TUI process); guarded by
/// a Mutex because refresh ticks run on short-lived background threads.
static BOARD_ETAG_CACHE: std::sync::Mutex<Option<(String, String)>> =
    std::sync::Mutex::new(None);

/// #1337: fetch one assignment's FULL `review_findings` raw JSON string from
/// the daemon's single-assignment detail endpoint (`GET /assignment/{id}`).
/// The `/board` collection wire carries only a bounded preview
/// (`review_findings_truncated`); the Review stage pane hydrates the full
/// body through this.  Thread-per-request + channel, mirroring
/// [`spawn_artifact_fetch`].  Sends `None` on any HTTP/parse failure (the
/// pane keeps showing the preview).
pub(crate) fn spawn_findings_detail_fetch(
    base_url: String,
    token: Option<String>,
    assignment_id: String,
) -> std::sync::mpsc::Receiver<Option<String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(3))
            .timeout(std::time::Duration::from_secs(5))
            .build();
        let mut req = agent.get(&format!("{base_url}/assignment/{assignment_id}"));
        if let Some(t) = token {
            req = req.set("Authorization", &format!("Bearer {t}"));
        }
        let out = match req.call() {
            Ok(resp) => resp
                .into_string()
                .ok()
                .and_then(|body| serde_json::from_str::<serde_json::Value>(&body).ok())
                .and_then(|v| {
                    v.get("review_findings")
                        .and_then(|f| f.as_str())
                        .map(|s| s.to_string())
                }),
            Err(_) => None,
        };
        let _ = tx.send(out);
    });
    rx
}

/// #2497: fetch one issue's FULL `body` from the daemon's single-issue
/// detail endpoint (`GET /issue/{repo_name}/{number}`). The `/board`
/// collection wire drops a closed (non-epic) issue's body to 0 chars
/// (`board_wire.bound_issue_row`, #1791); the Board/Pipeline Issue tab
/// hydrates the full body through this. Mirrors
/// [`spawn_findings_detail_fetch`] (#1337). Sends `None` on any HTTP/parse
/// failure (the pane keeps showing the truncation notice).
pub(crate) fn spawn_issue_detail_fetch(
    base_url: String,
    token: Option<String>,
    repo_name: String,
    number: u64,
) -> std::sync::mpsc::Receiver<Option<String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let out = fetch_issue_body_blocking(&base_url, token.as_deref(), &repo_name, number);
        let _ = tx.send(out);
    });
    rx
}

/// #1939: the synchronous core of [`spawn_issue_detail_fetch`], factored out
/// so a one-shot caller that isn't on the render-tick poll loop (e.g.
/// [`super::sessions::CoordApp::chat_briefing`]'s Chat-session briefing) can
/// block on the same `GET /issue/{repo}/{number}` request inline instead of
/// arming a receiver nothing will ever drain. `None` on any HTTP/parse
/// failure, same as the async path.
pub(crate) fn fetch_issue_body_blocking(
    base_url: &str,
    token: Option<&str>,
    repo_name: &str,
    number: u64,
) -> Option<String> {
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(3))
        .timeout(std::time::Duration::from_secs(5))
        .build();
    let mut req = agent.get(&format!("{base_url}/issue/{repo_name}/{number}"));
    if let Some(t) = token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    match req.call() {
        Ok(resp) => resp
            .into_string()
            .ok()
            .and_then(|body| serde_json::from_str::<serde_json::Value>(&body).ok())
            .and_then(|v| v.get("body").and_then(|f| f.as_str()).map(|s| s.to_string())),
        Err(_) => None,
    }
}

/// #584: fetch the read-only board projection from the `coord serve` daemon
/// over HTTP and assemble it into a [`BoardData`] via the shared
/// [`assemble_board_data`] tail (so the machine probes still run exactly as the
/// local path does).
///
/// On ANY error — network failure, non-2xx status, or JSON parse mismatch —
/// returns `BoardData::default()` rather than panicking; the TUI's 5 s refresh
/// loop simply retries.
pub(crate) fn load_data_remote(url: &str, token: Option<&str>) -> BoardData {
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(8))
        .timeout(std::time::Duration::from_secs(8))
        .build();
    let mut req = agent.get(&format!("{url}/board"));
    if let Some(t) = token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    // #1336 invariant 5: cache-validated polling.  Send the last ETag as
    // If-None-Match; a 304 means the board hasn't changed, so re-parse the
    // cached body instead of re-downloading megabytes over Tailscale every
    // poll.  (This runs on the background refresh thread — the reparse never
    // blocks the UI.)
    let cached: Option<(String, String)> = BOARD_ETAG_CACHE
        .lock()
        .ok()
        .and_then(|guard| guard.clone());
    if let Some((etag, _)) = &cached {
        req = req.set("If-None-Match", etag);
    }
    // ureq's `json` feature isn't enabled, so read the body as a string and
    // parse with serde_json (already a dependency).
    let payload: BoardPayload = match req.call() {
        Ok(resp) if resp.status() == 304 => {
            // Not modified — the daemon validated our cached copy.
            let Some((_, body)) = cached else {
                return BoardData::default();
            };
            match serde_json::from_str::<BoardPayload>(&body) {
                Ok(p) => p,
                Err(_) => return BoardData::default(),
            }
        }
        Ok(resp) => {
            let etag = resp.header("etag").map(|e| e.to_string());
            match resp.into_string() {
                Ok(body) => match serde_json::from_str::<BoardPayload>(&body) {
                    Ok(p) => {
                        if let Some(etag) = etag {
                            if let Ok(mut guard) = BOARD_ETAG_CACHE.lock() {
                                *guard = Some((etag, body));
                            }
                        }
                        p
                    }
                    Err(_) => return BoardData::default(),
                },
                Err(_) => return BoardData::default(),
            }
        }
        Err(_) => return BoardData::default(),
    };

    let mut assignments = payload.assignments;
    // Sort: running first, then failed, then done (most recent first within
    // groups) — identical to the SQLite path.
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

    let machine_rows: Vec<(String, String, Vec<String>)> = payload
        .machines
        .into_iter()
        .map(|m| (m.name, m.host, m.repos))
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

    // #778: prefer the server-computed staging list from the /board payload;
    // fall back to local computation so the panel still works if the daemon
    // is running an older version that doesn't emit merge_staging yet.
    let merge_staging = if payload.merge_staging.is_empty() {
        compute_staging_local(
            &assignments,
            &payload.merge_queue,
            &pipeline_default_gates,
        )
    } else {
        payload.merge_staging
    };

    assemble_board_data(
        assignments,
        machine_rows,
        payload.issues,
        payload.merge_queue,
        payload.merge_plan,
        payload.proposals,
        plans,
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_repo_run_cmds,
        pipeline_repo_paths,
        pipeline_acceptance_routes,
        pipeline_require_plan,
        merge_staging,
        pipeline_models,
        // #550: prefer the server-computed stage projection; empty when the
        // daemon predates #550 (`pipeline.rs`'s local functions fall back).
        payload.issue_stage_projection,
        // #795: server-computed work-order rank + frontier; empty when the
        // daemon predates #795.
        payload.milestone_work_orders,
        // #1195/#1197: server-computed per-epic children; empty when the
        // daemon predates #1195 (the Pipeline tree renders epics as
        // ordinary flat leaves in that case).
        payload.epic_children,
        // #975: server-computed plan roster; empty when the daemon predates
        // #975 (the Plans panel shows a placeholder in that case).
        payload.plan_roster,
        // #976: capability flag distinguishing "empty roster" from "daemon
        // predates #975/#976 and never computed one" — see
        // `BoardData::plan_roster_supported`.
        payload.plan_roster_supported,
        // #978: server-computed GOAL.md north-star header; `available: false`
        // (the `Default`) on daemons that predate #978.
        payload.goal_header,
        // #1037/#1039: 15-minute audit-recency count for the Audit panel's
        // sidebar badge; `0` (`#[serde(default)]`) on daemons that predate
        // #1037.
        payload.audit_recent_count,
        // #1505: server-computed driver-escalation records; empty on
        // daemons that predate #1505.
        payload.escalations,
        // #1631 (H-4): server-computed fleet-health aggregate; empty
        // (`FleetHealthBlock::default()`, via `#[serde(default)]`) on
        // daemons that predate #1630.
        payload.fleet_health,
        // #1753/#1755 (DQ-3): the drive queue in run order; empty (via
        // `#[serde(default)]`) on daemons that predate #1753, which never
        // emit this key at all.
        payload.drive_queue,
        // #2608: the machine-local roll-pending marker; `None` (via
        // `#[serde(default)]`) on daemons that predate #2608, or when no
        // roll is currently pending.
        payload.roll_pending,
        // #2532: server-computed approved-submissions list (repos already
        // resolved via #2531's project↔repo mapping); empty (via
        // `#[serde(default)]`) on daemons that predate #2532.
        payload.approved_submissions,
    )
}

/// Decode a JSON plan_data blob into a `PlanData`.  Mirrors
/// `coord.plan_parser.WorkerPlan.from_dict`; tolerant of missing fields.
pub(crate) fn parse_plan_data(v: &serde_json::Value) -> PlanData {
    fn s(v: &serde_json::Value, key: &str) -> String {
        v.get(key)
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string()
    }
    fn vs(v: &serde_json::Value, key: &str) -> Vec<String> {
        v.get(key)
            .and_then(|x| x.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|e| e.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default()
    }
    // smoke_tests is tri-state: missing/null → None, [] → Some(empty),
    // non-empty list → Some(bullets).
    let smoke_tests = match v.get("smoke_tests") {
        Some(serde_json::Value::Array(arr)) => Some(
            arr.iter()
                .filter_map(|e| e.as_str().map(|s| s.to_string()))
                .collect(),
        ),
        _ => None,
    };
    PlanData {
        plan: s(v, "plan"),
        files_modify: vs(v, "files_modify"),
        approach: s(v, "approach"),
        risks: s(v, "risks"),
        estimate: s(v, "estimate"),
        smoke_tests,
    }
}

/// Spawn a background thread that calls [`load_data`] and sends the result
/// over a channel.  The caller polls the returned [`Receiver`] without
/// blocking the UI thread.
pub(crate) fn start_data_load() -> std::sync::mpsc::Receiver<BoardData> {
    let (tx, rx) = std::sync::mpsc::channel();
    // In the test binary, immediately resolve with an empty payload so that
    // apply_pending_data()'s degraded-tick guard fires and preserves the
    // BoardData seeded by make_test_app().  Without this guard, refreshes
    // triggered by view-switches (maybe_kick_pipeline_loader → refresh) read
    // the real local SQLite DB and overwrite pipeline_issues / data.assignments
    // with whatever the developer's coord.db currently contains, making
    // TuiDriver tests non-deterministic and machine-dependent.
    //
    // #2284 (ms-65 §3, manifest finding 7): gated on `feature = "test-support"`
    // too, not just `cfg(test)` — the sealed `tests/acceptance/**` suite
    // (`tui/tests/acceptance.rs`, the #1042 seam) is a separate integration-test
    // crate, so the `coord_tui` it links against is built *without* `cfg(test)`
    // (only the top-level `cargo test` binary gets that cfg). Without this
    // widening, entering the Pipeline panel in that suite kicks
    // `maybe_kick_pipeline_loader` -> `refresh` -> this function, which fell
    // through to the real network/SQLite path and wholesale-replaced the
    // seeded fixture on the very next dispatched event (a RACE — whichever
    // finished first) — mirrors the exact same trap `resolve_board_service`
    // above already widened for (#1039).
    #[cfg(any(test, feature = "test-support"))]
    {
        let _ = tx.send(BoardData::default());
        return rx;
    }
    #[allow(unreachable_code)]
    std::thread::spawn(move || {
        let _ = tx.send(load_data());
    });
    rx
}

/// One running interactive session discovered from `coord sessions --json`.
///
/// Sessions are named `coord-<assignment_id>` and survive TUI crashes;
/// the operator can reattach via `coord reattach <assignment_id>` or by
/// opening the Pipeline Terminal tab for the matching issue.
#[derive(Clone, Debug)]
pub(crate) struct LiveTmuxSession {
    /// The coordinator assignment ID extracted from the session name.
    pub(crate) assignment_id: String,
    /// GitHub issue number, if the assignment record is in the local DB.
    pub(crate) issue_number: Option<u64>,
    /// Coordinator-local repo name, if known.
    pub(crate) repo_name: Option<String>,
    /// Issue title, if known (for display purposes).  Shown in the startup
    /// toast so the operator recognises which work was in progress.
    #[allow(dead_code)]
    pub(crate) issue_title: Option<String>,
    /// Machine the session is hosted on, from `coord sessions --json`
    /// (`machine` field) or derived from the assignment record.  `None`
    /// for sessions that pre-date the field or whose machine is unknown.
    pub(crate) machine: Option<String>,
    /// `true` when the session's pane process (claude) has exited but the
    /// tmux session is still up — the detach-and-abandon / dead-pane case
    /// (#491).  `false` while the pane is still running or status is unknown
    /// (sessions that pre-date the `pane_dead` field default to `false`).
    pub(crate) pane_dead: bool,
    /// #935 (Part A): number of discovery sweeps this optimistic `"pending-"`
    /// entry has survived without being covered by a real session.  Only used
    /// on entries whose `assignment_id` starts with `"pending-"`; always 0 on
    /// real discovery entries.  When the count exceeds the budget (2 sweeps),
    /// `poll_remote_sessions` drops the entry so a phantom "Live" badge cannot
    /// linger forever when a session never actually started.
    pub(crate) pending_sweep_count: u8,
    /// #1031: `true` when a client is currently attached to the tmux
    /// session, from `coord sessions --json`'s `attached` key. Mirrors
    /// `FleetTerminal::attached`'s `#{session_attached}` handling. `false`
    /// for sessions discovered by a probe that pre-dates this field.
    /// Surfaced as a `[attached]` tag in the #1032 Sessions-panel tree.
    pub(crate) attached: bool,
}

/// Fetch live `coord-*` tmux sessions by running `coord sessions --json`.
///
/// Returns an empty `Vec` when tmux is not running, `coord` is not on PATH,
/// or parsing fails.  This is called once at startup — it's cheap but
/// synchronous so it runs before the TUI is visible.
pub(crate) fn fetch_live_tmux_sessions() -> Vec<LiveTmuxSession> {
    let out = std::process::Command::new("coord")
        .args(["sessions", "--json"])
        .output()
        .ok();
    let out = match out {
        Some(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    parse_sessions_json(&String::from_utf8_lossy(&out.stdout))
}

/// Parse the `{"sessions": [...]}` JSON emitted by `coord sessions --json`.
/// Shared by the synchronous local fetch and the background remote fetch.
pub(crate) fn parse_sessions_json(text: &str) -> Vec<LiveTmuxSession> {
    let v: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let arr = match v.get("sessions").and_then(|s| s.as_array()) {
        Some(a) => a,
        None => return Vec::new(),
    };
    arr.iter()
        .filter_map(|entry| {
            let assignment_id = entry.get("assignment_id")?.as_str()?.to_string();
            let issue_number = entry
                .get("issue_number")
                .and_then(|n| n.as_u64());
            let repo_name = entry
                .get("repo_name")
                .and_then(|n| n.as_str())
                .map(|s| s.to_string());
            let issue_title = entry
                .get("issue_title")
                .and_then(|n| n.as_str())
                .map(|s| s.to_string());
            let machine = entry
                .get("machine")
                .and_then(|n| n.as_str())
                .map(|s| s.to_string());
            // #491: "1" = pane process has exited; "0" or absent = alive.
            let pane_dead = entry
                .get("pane_dead")
                .and_then(|v| v.as_str())
                .map(|s| s == "1")
                .unwrap_or(false);
            // #1031: `attached` is a JSON bool (not a "0"/"1" string like
            // `pane_dead`) — see `coord/commands/sessions.py`'s
            // `sessions_cmd`. Absent for a probe that pre-dates the field.
            let attached = entry
                .get("attached")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            Some(LiveTmuxSession {
                assignment_id,
                issue_number,
                repo_name,
                issue_title,
                machine,
                pane_dead,
                pending_sweep_count: 0,
                attached,
            })
        })
        .collect()
}

/// #486 Leg 4: fetch local + REMOTE coord-* sessions in the background.
///
/// Runs `coord sessions --json --remote` (which ssh-probes the fleet) off the
/// startup path so the TUI appears immediately; the result REPLACES the
/// local-only startup snapshot when it arrives (it is a superset).  A missing
/// config path lets `coord` fall back to its own discovery.
pub(crate) fn spawn_remote_tmux_sessions_fetch(
    config_path: Option<std::path::PathBuf>,
) -> std::sync::mpsc::Receiver<Vec<LiveTmuxSession>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut args: Vec<String> =
            vec!["sessions".into(), "--json".into(), "--remote".into()];
        if let Some(cfg) = config_path {
            args.push("--config".into());
            args.push(cfg.to_string_lossy().into_owned());
        }
        let out = std::process::Command::new("coord").args(&args).output().ok();
        let sessions = match out {
            Some(o) if o.status.success() => {
                parse_sessions_json(&String::from_utf8_lossy(&o.stdout))
            }
            _ => Vec::new(),
        };
        let _ = tx.send(sessions);
    });
    rx
}

/// #953: one persistent, free-floating `coord-term-*` terminal discovered via
/// `coord terminal list --json` (#952). Distinct from [`LiveTmuxSession`]
/// (`coord-<assignment_id>` interactive claude sessions): a `FleetTerminal`
/// carries no issue/repo/assignment — it's a plain shell, grouped in the
/// Terminal view's left-pane tree by the machine it runs on.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct FleetTerminal {
    /// The `coord-term-<slug>` slug (without the `coord-term-` prefix).
    pub(crate) name: String,
    /// Coordinator-local machine name (`coordinator.yml` `machines[].name`),
    /// matching `Machine.name` so the tree can group by parent.
    pub(crate) machine: String,
    /// `true` when a client currently has the tmux session attached.
    pub(crate) attached: bool,
    /// #954: `true` for an optimistic entry inserted by
    /// `create_and_attach_terminal` immediately on creation, before the next
    /// `coord terminal list` discovery sweep can confirm it. Always `false`
    /// on entries parsed from a real discovery result. Mirrors
    /// `LiveTmuxSession`'s `"pending-"`-prefix convention, but as an
    /// explicit field since `FleetTerminal` slugs carry no such prefix.
    pub(crate) pending: bool,
    /// #954: number of discovery sweeps a `pending` entry has survived
    /// without being covered by a real result. Only meaningful when
    /// `pending` is `true`; always 0 on real entries. Mirrors
    /// `LiveTmuxSession::pending_sweep_count` / `PENDING_SESSION_SWEEP_BUDGET`
    /// — `poll_remote_terminals` evicts the entry once this exceeds
    /// `PENDING_TERMINAL_SWEEP_BUDGET`, so a phantom entry that never
    /// becomes a real tmux session doesn't linger in the tree forever.
    pub(crate) pending_sweep_count: u8,
}

/// Fetch local `coord-term-*` terminals by running `coord terminal list --json`.
///
/// Mirrors [`fetch_live_tmux_sessions`]: returns an empty `Vec` when tmux is
/// not running, `coord` is not on PATH, or parsing fails. Called once at
/// startup — cheap but synchronous so it runs before the TUI is visible.
pub(crate) fn fetch_fleet_terminals() -> Vec<FleetTerminal> {
    let out = std::process::Command::new("coord")
        .args(["terminal", "list", "--json"])
        .output()
        .ok();
    let out = match out {
        Some(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    parse_fleet_terminals_json(&String::from_utf8_lossy(&out.stdout))
}

/// Parse the JSON emitted by `coord terminal list --json`.
///
/// Unlike `coord sessions --json`'s `{"sessions": [...]}` envelope, this is
/// a **bare JSON array** of `{"name","attached","machine","host",...}`
/// objects (see `coord/commands/terminal.py::terminal_list`). Shared by the
/// synchronous local fetch and the background remote fetch.
pub(crate) fn parse_fleet_terminals_json(text: &str) -> Vec<FleetTerminal> {
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
            let name = entry.get("name")?.as_str()?.to_string();
            let machine = entry
                .get("machine")
                .and_then(|m| m.as_str())
                .unwrap_or("")
                .to_string();
            let attached = entry
                .get("attached")
                .and_then(|a| a.as_bool())
                .unwrap_or(false);
            Some(FleetTerminal {
                name,
                machine,
                attached,
                pending: false,
                pending_sweep_count: 0,
            })
        })
        .collect()
}

/// #953: fetch local + REMOTE `coord-term-*` terminals in the background.
///
/// Mirrors [`spawn_remote_tmux_sessions_fetch`]: runs `coord terminal list
/// --json --remote` off the startup path so the TUI appears immediately;
/// the result REPLACES the local-only startup snapshot when it arrives.
pub(crate) fn spawn_remote_fleet_terminals_fetch(
    config_path: Option<std::path::PathBuf>,
) -> std::sync::mpsc::Receiver<Vec<FleetTerminal>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut args: Vec<String> = vec![
            "terminal".into(),
            "list".into(),
            "--json".into(),
            "--remote".into(),
        ];
        if let Some(cfg) = config_path {
            args.push("--config".into());
            args.push(cfg.to_string_lossy().into_owned());
        }
        let out = std::process::Command::new("coord").args(&args).output().ok();
        let terminals = match out {
            Some(o) if o.status.success() => {
                parse_fleet_terminals_json(&String::from_utf8_lossy(&o.stdout))
            }
            _ => Vec::new(),
        };
        let _ = tx.send(terminals);
    });
    rx
}

/// #603: fetch the EXACT fix briefing for `aid` (`coord fix-briefing <aid>`) off
/// the UI thread, so the fail→fix / rework confirm dialog can show the operator
/// what the fix worker will be briefed with.  stdout IS the briefing text; on
/// any failure a short human note is returned (the dialog still launches fine).
pub(crate) fn spawn_fix_briefing_fetch(
    aid: String,
    config_path: Option<std::path::PathBuf>,
) -> std::sync::mpsc::Receiver<String> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        // `--config` is a per-subcommand option → it must come AFTER `aid`.
        let mut args: Vec<String> = vec!["fix-briefing".into(), aid];
        if let Some(cfg) = config_path {
            args.push("--config".into());
            args.push(cfg.to_string_lossy().into_owned());
        }
        let out = std::process::Command::new("coord").args(&args).output().ok();
        let text = match out {
            Some(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).into_owned(),
            Some(o) => format!(
                "(could not resolve the fix briefing: {})",
                String::from_utf8_lossy(&o.stderr).trim()
            ),
            None => "(could not run `coord fix-briefing`)".to_string(),
        };
        let _ = tx.send(text);
    });
    rx
}

/// Return the version string of the local `coord` binary by running
/// `coord --version` synchronously.  Parses the last whitespace-separated
/// token from the first output line (e.g. "coord 0.4.1" → "0.4.1").
/// Returns `None` when `coord` is not found, exits non-zero, or returns
/// unparseable output.
pub(crate) fn fetch_local_coord_version() -> Option<String> {
    let out = std::process::Command::new("coord")
        .arg("--version")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines()
        .next()
        .and_then(|l| l.split_whitespace().last())
        .map(|s| s.to_string())
}

/// Spawn a background thread that fetches a remote agent log over HTTP.
///
/// Returns a `Receiver` that yields `Ok(raw_content)` or `Err(error_message)`.
/// The caller must parse the content with [`parse_log_content`] on the main
/// thread — keeping `ListItem` construction off the worker thread.
pub(crate) fn spawn_log_fetch(host: &str, id: &str) -> std::sync::mpsc::Receiver<Result<String, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:7433/logs/{}", host, id);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            .timeout(std::time::Duration::from_secs(5))
            .build();
        let result = match agent.get(&url).call() {
            Ok(resp) => resp.into_string().map_err(|e| e.to_string()),
            Err(e) => Err(e.to_string()),
        };
        let _ = tx.send(result);
    });
    rx
}

/// Spawn a `gh issue view` for a single issue and parse the response into a
/// [`FetchedIssue`]. Used by the Board Issue tab when the issue isn't in the
/// local `issues` table (e.g. closed >7d ago and pruned).
///
/// On success, also upserts the row into the local `issues` table so the
/// fetch becomes durable — the next `load_data` finds it and we don't repeat
/// the gh call on the next session. The upsert uses a writer connection with
/// busy_timeout=5s, the same pattern as the purge/test-verdict writers.
pub(crate) fn spawn_issue_fetch(
    repo_slug: String,
    repo_name: String,
    number: u64,
) -> std::sync::mpsc::Receiver<Result<FetchedIssue, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let output = std::process::Command::new("gh")
            .args([
                "issue",
                "view",
                &number.to_string(),
                "--repo",
                &repo_slug,
                "--json",
                "number,title,body,labels,state,milestone",
            ])
            .output();
        let result = match output {
            Ok(o) if o.status.success() => {
                match serde_json::from_slice::<serde_json::Value>(&o.stdout) {
                    Ok(v) => {
                        let labels: Vec<String> = v
                            .get("labels")
                            .and_then(|l| l.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|l| {
                                        l.get("name").and_then(|n| n.as_str()).map(String::from)
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        // #406: parse milestone {number, title} or null.
                        let milestone_obj = v.get("milestone");
                        let milestone_number = milestone_obj
                            .and_then(|m| m.get("number"))
                            .and_then(|n| n.as_i64());
                        let milestone_title = milestone_obj
                            .and_then(|m| m.get("title"))
                            .and_then(|t| t.as_str())
                            .map(String::from);
                        let issue = FetchedIssue {
                            number,
                            title: v
                                .get("title")
                                .and_then(|s| s.as_str())
                                .unwrap_or("")
                                .to_string(),
                            body: v
                                .get("body")
                                .and_then(|s| s.as_str())
                                .unwrap_or("")
                                .to_string(),
                            labels,
                            state: v
                                .get("state")
                                .and_then(|s| s.as_str())
                                .unwrap_or("open")
                                .to_ascii_lowercase(),
                            milestone_number,
                            milestone_title,
                        };
                        // Best-effort upsert into the shared issue cache via
                        // the daemon. Failures (daemon down, 5xx, etc.) are
                        // non-fatal — the in-memory cache still serves the
                        // body for the rest of the session.
                        let _ = upsert_issue_remote(&repo_name, &issue);
                        Ok(issue)
                    }
                    Err(e) => Err(format!("gh json parse failed: {}", e)),
                }
            }
            Ok(o) => Err(String::from_utf8_lossy(&o.stderr).trim().to_string()),
            Err(e) => Err(format!("could not run gh: {}", e)),
        };
        let _ = tx.send(result);
    });
    rx
}

/// #271 part 2 follow-up: spawn a background `gh pr view` to fetch the
/// PR title, body, and files-changed list for a single PR.  Mirrors
/// `spawn_issue_fetch`: same channel-receiver shape, same lifecycle in
/// the caching maps on `CoordApp` (`pending_pr_fetches` →
/// `fetched_prs_cache`).
pub(crate) fn spawn_pr_fetch(
    repo_slug: String,
    pr_number: i64,
) -> std::sync::mpsc::Receiver<Result<FetchedPr, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let output = std::process::Command::new("gh")
            .args([
                "pr",
                "view",
                &pr_number.to_string(),
                "--repo",
                &repo_slug,
                "--json",
                "title,body,files,reviews",
            ])
            .output();
        let result = match output {
            Ok(o) if o.status.success() => {
                match serde_json::from_slice::<serde_json::Value>(&o.stdout) {
                    Ok(v) => {
                        let files: Vec<String> = v
                            .get("files")
                            .and_then(|f| f.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|f| {
                                        f.get("path").and_then(|n| n.as_str()).map(String::from)
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        let reviews: Vec<FetchedReview> = v
                            .get("reviews")
                            .and_then(|r| r.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .map(|r| FetchedReview {
                                        state: r
                                            .get("state")
                                            .and_then(|s| s.as_str())
                                            .unwrap_or("")
                                            .to_string(),
                                        body: r
                                            .get("body")
                                            .and_then(|s| s.as_str())
                                            .unwrap_or("")
                                            .to_string(),
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        Ok(FetchedPr {
                            title: v
                                .get("title")
                                .and_then(|s| s.as_str())
                                .unwrap_or("")
                                .to_string(),
                            body: v
                                .get("body")
                                .and_then(|s| s.as_str())
                                .unwrap_or("")
                                .to_string(),
                            files,
                            reviews,
                        })
                    }
                    Err(e) => Err(format!("gh json parse failed: {}", e)),
                }
            }
            Ok(o) => Err(String::from_utf8_lossy(&o.stderr).trim().to_string()),
            Err(e) => Err(format!("could not run gh: {}", e)),
        };
        let _ = tx.send(result);
    });
    rx
}


/// Upsert a freshly-fetched issue into the shared `issues` cache.
///
/// #2895: this used to open its own read-write `rusqlite` connection to
/// `~/.coord/coord.db`. It now POSTs to the daemon's `/issue-upsert` route
/// (`coord.state._upsert_issue_local` behind it), so the row lands in
/// whichever engine the daemon owns — and, on the daemon host, no longer
/// races `coord serve` on the same file.
///
/// Mirrors the request/response shape of [`record_test_verdict_remote`]
/// (#590): the caller has already resolved the service, or there is nothing
/// to write to.
pub(crate) fn upsert_issue_remote(repo_name: &str, issue: &FetchedIssue) -> Result<(), String> {
    let (url, token) = resolve_board_service().ok_or(NO_BOARD_SERVICE_ERROR)?;
    let body = serde_json::json!({
        "repo_name": repo_name,
        "issue": {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "labels": issue.labels,
            "milestone_number": issue.milestone_number,
            "milestone_title": issue.milestone_title,
        },
    });
    post_daemon_json(&url, token.as_deref(), "/issue-upsert", &body)?;
    Ok(())
}

/// Spawn a background thread that opens a Server-Sent Events connection to
/// `http://{host}:7433/stream/{id}`, parses SSE events, and sends them over
/// the returned `Receiver`.
///
/// ## Resume support
/// Pass `last_event_id > 0` to resume from a previous byte-offset by sending
/// the `Last-Event-Id` header.  The agent's `/stream/{id}` endpoint uses the
/// byte offset as the event id, so the stream resumes from that position.
///
/// ## Cancellation
/// Drop the returned `Receiver` to signal the thread to exit.  The thread
/// detects this on the next `tx.send()` call (which returns `Err`).  Under
/// normal conditions this happens within 15 s (SSE keepalive interval); a
/// 20-second read timeout acts as a safety net if keepalives stop.
pub(crate) fn spawn_sse_watch(
    host: &str,
    id: &str,
    last_event_id: u64,
) -> std::sync::mpsc::Receiver<SseWatchMsg> {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:7433/stream/{}", host, id);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            // 20 s read timeout. The server sends SSE keepalives every 15 s so
            // this fires only when the connection is genuinely dead.
            .timeout_read(std::time::Duration::from_secs(20))
            .build();

        let mut builder = agent.get(&url);
        if last_event_id > 0 {
            builder = builder.set("Last-Event-Id", &last_event_id.to_string());
        }

        let resp = match builder.call() {
            Ok(r) => r,
            // #2064: a 404 on the initial connect means the agent has no log
            // for this assignment and never will (unknown assignment id, or
            // an ended session with no log_path) — distinct from a transport
            // failure, which is genuinely worth retrying.
            Err(ureq::Error::Status(404, _)) => {
                let _ = tx.send(SseWatchMsg::NotFound);
                return;
            }
            Err(e) => {
                let _ = tx.send(SseWatchMsg::Error(e.to_string()));
                return;
            }
        };

        use std::io::BufRead;
        let reader = std::io::BufReader::new(resp.into_reader());

        let mut current_id = last_event_id;
        let mut current_event = String::new();
        let mut current_data: Vec<String> = Vec::new();

        for line_result in reader.lines() {
            let line = match line_result {
                Ok(l) => l,
                Err(e) => {
                    // Read error (timeout, connection reset, etc.).
                    let _ = tx.send(SseWatchMsg::Error(e.to_string()));
                    return;
                }
            };

            // Empty line = dispatch the current accumulated event.
            if line.is_empty() {
                if !current_event.is_empty() || !current_data.is_empty() {
                    let text = current_data.join("\n");
                    let keep_going = match current_event.as_str() {
                        "log" => tx
                            .send(SseWatchMsg::Lines {
                                last_id: current_id,
                                text,
                            })
                            .is_ok(),
                        "end" => {
                            let _ = tx.send(SseWatchMsg::Done {
                                last_id: current_id,
                            });
                            return;
                        }
                        _ => true, // unknown event type — ignore
                    };
                    if !keep_going {
                        return; // receiver was dropped; exit cleanly
                    }
                    current_event.clear();
                    current_data.clear();
                }
                continue;
            }

            // SSE comment / keepalive — send a Heartbeat so the thread
            // discovers a dropped receiver (cancel) within one keepalive period.
            if line.starts_with(':') {
                if tx.send(SseWatchMsg::Heartbeat).is_err() {
                    return;
                }
                continue;
            }

            // SSE field lines.
            if let Some(v) = line.strip_prefix("id: ") {
                current_id = v.trim().parse().unwrap_or(current_id);
            } else if let Some(v) = line.strip_prefix("event: ") {
                current_event = v.trim().to_string();
            } else if let Some(v) = line.strip_prefix("data: ") {
                current_data.push(v.to_string());
            }
            // retry: lines are ignored — the main thread owns reconnect logic.
        }

        // EOF: connection closed without an explicit `end` event.
        let _ = tx.send(SseWatchMsg::Done {
            last_id: current_id,
        });
    });
    rx
}

/// Build a placeholder `WatchSseState` for assignments on the local machine
/// (no host ⇒ no SSE endpoint). The state starts as `done` so the watch
/// overlay falls back to the polling path without showing "Connecting…".
pub(crate) fn make_local_sse_state(_assignment_id: &str) -> WatchSseState {
    // Create a disconnected channel — we'll never use the receiver for real data.
    let (_tx, rx) = std::sync::mpsc::channel::<SseWatchMsg>();
    WatchSseState {
        rx,
        lines: Vec::new(),
        last_event_id: 0,
        fail_count: 0,
        first_fail_at: None,
        done: true, // Treat as done so the log fallback path is used.
        host: String::new(),
        pending_tail: String::new(),
        line_times: Vec::new(),
        current_turn: 0,
    }
}

#[cfg(test)]
mod tests {
    //! #2572: `parse_agent_venv_health` / `merge_live_agent_venv_health` —
    //! the TUI-side half of "surface `agent_venv` CRIT somewhere the live
    //! panel shows without being asked". See those functions' own doc
    //! comments for the incident (#2569/#2570) this closes: `coord serve`'s
    //! own `fleet_health` snapshot can be empty (the local-SQLite path, by
    //! design) or stale (computed by a daemon that can share the exact
    //! failure domain being checked), so a machine's own live `/health`
    //! response is folded in as a second, independent source that cannot
    //! be silently masked.
    use super::*;

    fn health_response(check_id: &str, severity: &str, headroom: &str) -> serde_json::Value {
        serde_json::json!({
            "version": "0.5.240",
            "worktree_bytes": 0,
            "health": {
                "schema": 1,
                "checked_at": 1000.0,
                "severity": severity,
                "counts": {},
                "skipped": [],
                "results": [
                    {
                        "check_id": check_id,
                        "severity": severity,
                        "headroom": headroom,
                    }
                ],
            }
        })
    }

    fn machine_health(machine: &str, severity: &str) -> FleetMachineHealth {
        FleetMachineHealth {
            machine: machine.to_string(),
            state: String::new(),
            severity: severity.to_string(),
            stale: false,
            checked_at: Some(500.0),
            results: vec![FleetHealthCheckResult {
                key: format!("{machine}:disk"),
                check_id: "disk".to_string(),
                severity: severity.to_string(),
                ..FleetHealthCheckResult::default()
            }],
        }
    }

    mod parse_agent_venv_health_tests {
        use super::*;

        #[test]
        fn finds_the_agent_venv_entry_among_others() {
            let v = serde_json::json!({
                "health": {
                    "results": [
                        {"check_id": "disk", "severity": "ok", "headroom": "50% free"},
                        {"check_id": "agent_venv", "severity": "crit", "headroom": "editable 0.5.240 from ~/x"},
                    ]
                }
            });
            let av = parse_agent_venv_health(&v).expect("agent_venv present");
            assert_eq!(av.severity, "crit");
            assert_eq!(av.headroom, "editable 0.5.240 from ~/x");
        }

        #[test]
        fn none_when_health_key_absent() {
            let v = serde_json::json!({"version": "0.5.240"});
            assert!(parse_agent_venv_health(&v).is_none());
        }

        #[test]
        fn none_when_no_results_array() {
            let v = serde_json::json!({"health": {}});
            assert!(parse_agent_venv_health(&v).is_none());
        }

        #[test]
        fn none_when_agent_venv_not_among_results() {
            let v = serde_json::json!({
                "health": {"results": [{"check_id": "disk", "severity": "ok", "headroom": ""}]}
            });
            assert!(parse_agent_venv_health(&v).is_none());
        }

        #[test]
        fn defaults_severity_to_unknown_when_missing() {
            let v = serde_json::json!({
                "health": {"results": [{"check_id": "agent_venv", "headroom": "?"}]}
            });
            let av = parse_agent_venv_health(&v).expect("agent_venv present");
            assert_eq!(av.severity, "unknown");
        }

        #[test]
        fn parses_a_realistic_full_response() {
            let v = health_response("agent_venv", "crit", "editable 0.5.242 from ~/.coord/worktrees/9c9cc8b694bd");
            let av = parse_agent_venv_health(&v).expect("agent_venv present");
            assert_eq!(av.severity, "crit");
            assert!(av.headroom.contains("editable"));
        }
    }

    mod severity_rank_tests {
        use super::*;

        #[test]
        fn ranks_in_the_documented_order() {
            assert!(severity_rank("ok") < severity_rank("unknown"));
            assert!(severity_rank("unknown") < severity_rank("warn"));
            assert!(severity_rank("warn") < severity_rank("crit"));
        }

        #[test]
        fn unrecognised_string_ranks_as_unknown_not_ok() {
            assert_eq!(severity_rank("bogus"), severity_rank("unknown"));
            assert_eq!(severity_rank(""), severity_rank("unknown"));
        }
    }

    mod merge_live_agent_venv_health_tests {
        use super::*;

        #[test]
        fn adds_a_row_for_a_machine_existing_has_none_for() {
            let existing = FleetHealthBlock::default();
            let live = vec![(
                "dellserver".to_string(),
                Some(AgentVenvHealth { severity: "crit".to_string(), headroom: "editable 0.5.240".to_string() }),
            )];

            let merged = merge_live_agent_venv_health(existing, &live, 1000.0);

            assert_eq!(merged.machine_health.len(), 1);
            let row = &merged.machine_health[0];
            assert_eq!(row.machine, "dellserver");
            assert_eq!(row.severity, "crit");
            assert!(!row.stale);
            assert_eq!(row.checked_at, Some(1000.0));
            assert_eq!(row.results[0].check_id, "agent_venv");
        }

        #[test]
        fn upgrades_an_existing_ok_entry_to_the_live_crit() {
            let existing = FleetHealthBlock {
                machine_health: vec![machine_health("dellserver", "ok")],
                fleet_checks: vec![],
            };
            let live = vec![(
                "dellserver".to_string(),
                Some(AgentVenvHealth { severity: "crit".to_string(), headroom: "editable 0.5.240".to_string() }),
            )];

            let merged = merge_live_agent_venv_health(existing, &live, 1000.0);

            assert_eq!(merged.machine_health.len(), 1);
            assert_eq!(merged.machine_health[0].severity, "crit");
            assert_eq!(merged.machine_health[0].checked_at, Some(1000.0));
        }

        #[test]
        fn never_downgrades_an_equal_or_worse_existing_reading() {
            // `existing` already says CRIT (for some OTHER check, e.g. disk)
            // — a live WARN on agent_venv must not overwrite it with a
            // weaker severity or erase the disk-check detail.
            let existing = FleetHealthBlock {
                machine_health: vec![machine_health("dellserver", "crit")],
                fleet_checks: vec![],
            };
            let live = vec![(
                "dellserver".to_string(),
                Some(AgentVenvHealth { severity: "warn".to_string(), headroom: "1 release behind".to_string() }),
            )];

            let merged = merge_live_agent_venv_health(existing, &live, 1000.0);

            assert_eq!(merged.machine_health.len(), 1);
            assert_eq!(merged.machine_health[0].severity, "crit");
            assert_eq!(merged.machine_health[0].results[0].check_id, "disk");
        }

        #[test]
        fn a_live_ok_reading_never_synthesizes_a_row() {
            let existing = FleetHealthBlock::default();
            let live = vec![(
                "dellserver".to_string(),
                Some(AgentVenvHealth { severity: "ok".to_string(), headroom: "pypi 0.5.242".to_string() }),
            )];

            let merged = merge_live_agent_venv_health(existing, &live, 1000.0);

            assert!(merged.machine_health.is_empty());
        }

        #[test]
        fn an_unreachable_machine_none_probe_is_a_no_op() {
            let existing = FleetHealthBlock::default();
            let live = vec![("dellserver".to_string(), None)];

            let merged = merge_live_agent_venv_health(existing, &live, 1000.0);

            assert!(merged.machine_health.is_empty());
        }

        #[test]
        fn only_touches_machines_the_live_probe_actually_names() {
            let existing = FleetHealthBlock {
                machine_health: vec![machine_health("elitebook", "ok")],
                fleet_checks: vec![],
            };
            let live = vec![(
                "dellserver".to_string(),
                Some(AgentVenvHealth { severity: "crit".to_string(), headroom: "editable".to_string() }),
            )];

            let merged = merge_live_agent_venv_health(existing, &live, 1000.0);

            assert_eq!(merged.machine_health.len(), 2);
            let elitebook = merged.machine_health.iter().find(|m| m.machine == "elitebook").unwrap();
            assert_eq!(elitebook.severity, "ok");
            let dellserver = merged.machine_health.iter().find(|m| m.machine == "dellserver").unwrap();
            assert_eq!(dellserver.severity, "crit");
        }

        #[test]
        fn fleet_scope_checks_pass_through_untouched() {
            let existing = FleetHealthBlock {
                machine_health: vec![],
                fleet_checks: vec![FleetHealthCheckResult {
                    check_id: "board_latency".to_string(),
                    severity: "warn".to_string(),
                    ..FleetHealthCheckResult::default()
                }],
            };
            let live = vec![(
                "dellserver".to_string(),
                Some(AgentVenvHealth { severity: "crit".to_string(), headroom: "editable".to_string() }),
            )];

            let merged = merge_live_agent_venv_health(existing, &live, 1000.0);

            assert_eq!(merged.fleet_checks.len(), 1);
            assert_eq!(merged.fleet_checks[0].check_id, "board_latency");
        }
    }
}
