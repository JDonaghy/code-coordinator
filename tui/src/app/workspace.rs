//! Project/Workspace model (#1326, A-1 of the Project-Scoped Cockpit
//! chassis — epic #1325; see `docs/COCKPIT.md`).
//!
//! Tracks *which repos ("projects") are open* and *which one is active* —
//! the foundation both the tab-strip UI (A-3) and per-view scoping (A-2)
//! build on. This module owns the model and its persistence only; it
//! renders nothing and changes no visible behavior on its own.
//!
//! Deliberately extracted into its own module — and its own field on
//! [`super::CoordApp`] — rather than two more flat fields on the god-struct
//! (`app/mod.rs`), chipping at #751.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[allow(unused_imports)]
use super::*;

// ─── Workspace model ─────────────────────────────────────────────────────────

/// Which repos are open in the workspace, and which one is active.
///
/// Persisted verbatim as JSON at `~/.coord/workspace.json` (mirrors the
/// existing client-side persistence convention — `TuiSettings` at
/// `~/.coord/settings.toml`, `client.toml` for the board-service seam).
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Workspace {
    /// Repo local-names currently open, in a stable (currently: sorted)
    /// order. No duplicates — every mutator dedups.
    open_projects: Vec<String>,
    /// The repo local-name currently active. `None` only when
    /// `open_projects` is empty.
    active_project: Option<String>,
}

impl Workspace {
    /// The currently active project, if any.
    pub fn active_project(&self) -> Option<&str> {
        self.active_project.as_deref()
    }

    /// All currently open projects, in display order.
    pub fn open_projects(&self) -> &[String] {
        &self.open_projects
    }

    /// Open `repo` (a no-op if already open). Activates it when it's the
    /// first project opened; otherwise leaves the current active project
    /// alone.
    pub fn open_project(&mut self, repo: &str) {
        if !self.open_projects.iter().any(|r| r == repo) {
            self.open_projects.push(repo.to_string());
        }
        if self.active_project.is_none() {
            self.active_project = Some(repo.to_string());
        }
    }

    /// Close `repo`. A no-op if it isn't open. If it was the active
    /// project, activates the project that took its place in the list
    /// (i.e. the next one, or the new last one if it was last), or `None`
    /// if it was the only open project.
    pub fn close_project(&mut self, repo: &str) {
        let Some(idx) = self.open_projects.iter().position(|r| r == repo) else {
            return;
        };
        self.open_projects.remove(idx);
        if self.active_project.as_deref() == Some(repo) {
            self.active_project = if self.open_projects.is_empty() {
                None
            } else {
                let next_idx = idx.min(self.open_projects.len() - 1);
                Some(self.open_projects[next_idx].clone())
            };
        }
    }

    /// Make `repo` the active project. Returns `false` (no-op) when `repo`
    /// isn't currently open — callers must `open_project` first.
    pub fn set_active(&mut self, repo: &str) -> bool {
        if self.open_projects.iter().any(|r| r == repo) {
            self.active_project = Some(repo.to_string());
            true
        } else {
            false
        }
    }

    /// Move the active project by `delta` positions through
    /// `open_projects`, wrapping around in either direction. A no-op when
    /// no projects are open.
    pub fn cycle_active(&mut self, delta: i32) {
        if self.open_projects.is_empty() {
            return;
        }
        let cur = self
            .active_project
            .as_deref()
            .and_then(|active| self.open_projects.iter().position(|r| r == active))
            .unwrap_or(0);
        let len = self.open_projects.len() as i32;
        let next = (cur as i32 + delta).rem_euclid(len);
        self.active_project = Some(self.open_projects[next as usize].clone());
    }

    /// Drop any open/active repo that isn't in `known_repos` — #1326:
    /// "tolerate a persisted repo that no longer exists in config (drop
    /// it)". If the active project was dropped, the first remaining open
    /// project (if any) becomes active.
    pub fn retain_known(&mut self, known_repos: &[String]) {
        self.open_projects.retain(|r| known_repos.contains(r));
        if let Some(active) = &self.active_project {
            if !self.open_projects.iter().any(|r| r == active) {
                self.active_project = self.open_projects.first().cloned();
            }
        }
    }

    /// Build the initial workspace from the full set of repos known to the
    /// board — every known repo starts open, and the active project
    /// defaults to the first one in a deterministic (sorted) order.
    pub fn derive_from_repos(known_repos: &[String]) -> Self {
        let mut open_projects: Vec<String> = known_repos.to_vec();
        open_projects.sort();
        open_projects.dedup();
        let active_project = open_projects.first().cloned();
        Self {
            open_projects,
            active_project,
        }
    }

    // ─── Persistence ─────────────────────────────────────────────────────────

    /// Path to the persisted workspace file (`~/.coord/workspace.json`), or
    /// `None` when `HOME` is unset — matches `TuiSettings::path()`.
    pub fn path() -> Option<PathBuf> {
        let home = std::env::var_os("HOME").map(PathBuf::from)?;
        Some(home.join(".coord").join("workspace.json"))
    }

    /// Load from a specific path. Returns the default (empty) workspace
    /// when the file doesn't exist or fails to parse — malformed JSON
    /// never panics, it just falls back as if nothing were persisted yet.
    pub fn load_from_path(path: &std::path::Path) -> Self {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Self::default();
        };
        serde_json::from_str::<Self>(&text).unwrap_or_default()
    }

    /// Load from `~/.coord/workspace.json`. Returns the default workspace
    /// when `HOME` is unset, the file is absent, or it fails to parse.
    pub fn load() -> Self {
        match Self::path() {
            Some(path) => Self::load_from_path(&path),
            None => Self::default(),
        }
    }

    /// Persist to a specific path, creating parent directories as needed.
    pub fn save_to_path(&self, path: &std::path::Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("create workspace dir: {e}"))?;
        }
        let text =
            serde_json::to_string_pretty(self).map_err(|e| format!("serialize workspace: {e}"))?;
        std::fs::write(path, text).map_err(|e| format!("write workspace: {e}"))?;
        Ok(())
    }

    /// Persist to `~/.coord/workspace.json`. A no-op (`Ok`) when `HOME` is
    /// unset — the TUI stays functional without a home directory, it just
    /// won't remember the workspace across restarts.
    pub fn save(&self) -> Result<(), String> {
        match Self::path() {
            Some(path) => self.save_to_path(&path),
            None => Ok(()),
        }
    }
}

// ─── BoardData repo union ────────────────────────────────────────────────────

/// The set of repo local-names referenced anywhere in `data` — machines'
/// configured repos, assignments, and open issues. Sorted for determinism.
///
/// This is the same union `CoordApp::issues_by_repo` computes (`app/mod.rs`)
/// for the Board sidebar's repo sections, extracted here so the workspace's
/// initial `open_projects` derivation stays byte-for-byte in sync with it
/// instead of drifting via a second hand-written copy.
pub(crate) fn known_repos(data: &BoardData) -> Vec<String> {
    let mut set: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for m in &data.machines {
        for r in &m.repos {
            set.insert(r.clone());
        }
    }
    for a in &data.assignments {
        set.insert(a.repo.clone());
    }
    for oi in &data.open_issues {
        set.insert(oi.repo_name.clone());
    }
    set.into_iter().collect()
}

/// Pure reconciliation step shared by [`CoordApp::sync_workspace_repos`]:
/// drops any open/active repo not in `known`, then — if that leaves
/// nothing open (including "nothing was ever persisted") — derives a
/// fresh workspace from `known`. Returns `true` when `workspace` was
/// actually mutated, so the caller only persists on a real change.
///
/// Split out as a free function (rather than inlined into the `CoordApp`
/// method) so it's unit-testable without touching disk or building a full
/// `CoordApp`.
fn reconcile_workspace(workspace: &mut Workspace, known: &[String]) -> bool {
    let before = workspace.clone();
    workspace.retain_known(known);
    if workspace.open_projects().is_empty() {
        *workspace = Workspace::derive_from_repos(known);
    }
    *workspace != before
}

// ─── CoordApp integration ────────────────────────────────────────────────────

impl CoordApp {
    /// The currently active project (repo local-name), if any.
    pub fn active_project(&self) -> Option<&str> {
        self.workspace.active_project()
    }

    /// All currently open projects.
    pub fn open_projects(&self) -> &[String] {
        self.workspace.open_projects()
    }

    /// Open `repo` in the workspace and persist the change.
    pub fn open_project(&mut self, repo: &str) {
        self.workspace.open_project(repo);
        self.persist_workspace();
    }

    /// Close `repo` in the workspace and persist the change.
    pub fn close_project(&mut self, repo: &str) {
        self.workspace.close_project(repo);
        self.persist_workspace();
    }

    /// Make `repo` the active project and persist the change. Returns
    /// `false` when `repo` isn't open (nothing changes, nothing is saved).
    pub fn set_active(&mut self, repo: &str) -> bool {
        let changed = self.workspace.set_active(repo);
        if changed {
            self.persist_workspace();
        }
        changed
    }

    /// Move the active project by `delta` and persist the change.
    pub fn cycle_active(&mut self, delta: i32) {
        self.workspace.cycle_active(delta);
        self.persist_workspace();
    }

    /// #1326: reconcile `self.workspace` against the repos currently known
    /// to the board. Called after every successful data refresh
    /// (`apply_pending_data` in `app/mod.rs`), right after
    /// `rebuild_board_sidebar()`:
    ///
    /// - Drops any open/active repo no longer present anywhere in `data`
    ///   (config change, e.g. a repo removed from `coordinator.yml`).
    /// - If that leaves nothing open — including the very first run, where
    ///   nothing was persisted yet — derives a fresh workspace from the
    ///   full repo set, so the app always boots with a sane default
    ///   `active_project` once real board data has loaded.
    ///
    /// A no-op while `data` hasn't produced any repos yet (startup's very
    /// first, still-empty tick) — that would otherwise wipe out whatever
    /// was just loaded from disk before the real data arrives.
    pub(crate) fn sync_workspace_repos(&mut self) {
        let known = known_repos(&self.data);
        if known.is_empty() {
            return;
        }
        if reconcile_workspace(&mut self.workspace, &known) {
            self.persist_workspace();
        }
    }

    fn persist_workspace(&self) {
        if let Err(e) = self.workspace.save() {
            eprintln!("coord-tui: failed to persist workspace: {e}");
        }
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn repos(names: &[&str]) -> Vec<String> {
        names.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn derive_from_repos_opens_all_and_activates_first_sorted() {
        let ws = Workspace::derive_from_repos(&repos(&["zeta", "alpha", "mid"]));
        assert_eq!(ws.open_projects(), &["alpha", "mid", "zeta"]);
        assert_eq!(ws.active_project(), Some("alpha"));
    }

    #[test]
    fn derive_from_repos_dedups() {
        let ws = Workspace::derive_from_repos(&repos(&["a", "b", "a"]));
        assert_eq!(ws.open_projects(), &["a", "b"]);
    }

    #[test]
    fn derive_from_repos_empty_is_empty() {
        let ws = Workspace::derive_from_repos(&[]);
        assert!(ws.open_projects().is_empty());
        assert_eq!(ws.active_project(), None);
    }

    #[test]
    fn open_project_dedups_and_activates_first() {
        let mut ws = Workspace::default();
        ws.open_project("a");
        ws.open_project("a");
        ws.open_project("b");
        assert_eq!(ws.open_projects(), &["a", "b"]);
        // First-ever open becomes active; second open doesn't steal it.
        assert_eq!(ws.active_project(), Some("a"));
    }

    #[test]
    fn close_project_activates_neighbor_when_active_closed() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b", "c"]));
        ws.set_active("b");
        ws.close_project("b");
        assert_eq!(ws.open_projects(), &["a", "c"]);
        // idx of "b" was 1; min(1, len-1=1) -> "c".
        assert_eq!(ws.active_project(), Some("c"));
    }

    #[test]
    fn close_project_activates_new_last_when_closing_last() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b", "c"]));
        ws.set_active("c");
        ws.close_project("c");
        assert_eq!(ws.open_projects(), &["a", "b"]);
        assert_eq!(ws.active_project(), Some("b"));
    }

    #[test]
    fn close_project_none_left_clears_active() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a"]));
        ws.close_project("a");
        assert!(ws.open_projects().is_empty());
        assert_eq!(ws.active_project(), None);
    }

    #[test]
    fn close_project_not_active_leaves_active_alone() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b", "c"]));
        ws.set_active("a");
        ws.close_project("c");
        assert_eq!(ws.active_project(), Some("a"));
    }

    #[test]
    fn close_project_unknown_repo_is_noop() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b"]));
        ws.close_project("nope");
        assert_eq!(ws.open_projects(), &["a", "b"]);
    }

    #[test]
    fn set_active_rejects_unopened_repo() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b"]));
        assert!(!ws.set_active("c"));
        assert_eq!(ws.active_project(), Some("a"));
    }

    #[test]
    fn set_active_accepts_open_repo() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b"]));
        assert!(ws.set_active("b"));
        assert_eq!(ws.active_project(), Some("b"));
    }

    #[test]
    fn cycle_active_wraps_forward_and_backward() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b", "c"]));
        assert_eq!(ws.active_project(), Some("a"));
        ws.cycle_active(1);
        assert_eq!(ws.active_project(), Some("b"));
        ws.cycle_active(1);
        assert_eq!(ws.active_project(), Some("c"));
        ws.cycle_active(1);
        assert_eq!(ws.active_project(), Some("a"), "should wrap forward");
        ws.cycle_active(-1);
        assert_eq!(ws.active_project(), Some("c"), "should wrap backward");
    }

    #[test]
    fn cycle_active_noop_when_nothing_open() {
        let mut ws = Workspace::default();
        ws.cycle_active(1);
        assert_eq!(ws.active_project(), None);
    }

    #[test]
    fn retain_known_drops_stale_repo_and_active_moves_on() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b", "c"]));
        ws.set_active("b");
        // "b" no longer exists in config.
        ws.retain_known(&repos(&["a", "c"]));
        assert_eq!(ws.open_projects(), &["a", "c"]);
        assert_eq!(ws.active_project(), Some("a"));
    }

    #[test]
    fn retain_known_keeps_active_when_still_known() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b", "c"]));
        ws.set_active("c");
        ws.retain_known(&repos(&["a", "c"]));
        assert_eq!(ws.active_project(), Some("c"));
    }

    #[test]
    fn retain_known_all_dropped_clears_active() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b"]));
        ws.retain_known(&repos(&["z"]));
        assert!(ws.open_projects().is_empty());
        assert_eq!(ws.active_project(), None);
    }

    // ── Persistence round-trip ────────────────────────────────────────────────

    #[test]
    fn save_load_round_trip() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!(
            "coord_workspace_test_roundtrip_{}_{}.json",
            std::process::id(),
            "a"
        ));
        let mut ws = Workspace::derive_from_repos(&repos(&["repo-a", "repo-b"]));
        ws.set_active("repo-b");
        ws.save_to_path(&path).unwrap();
        let loaded = Workspace::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert_eq!(loaded, ws);
    }

    #[test]
    fn load_from_path_missing_file_returns_default() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!(
            "coord_workspace_test_missing_{}.json",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&path);
        let loaded = Workspace::load_from_path(&path);
        assert_eq!(loaded, Workspace::default());
    }

    #[test]
    fn load_from_path_malformed_json_returns_default() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!(
            "coord_workspace_test_malformed_{}.json",
            std::process::id()
        ));
        std::fs::write(&path, b"this is not valid json at all").unwrap();
        let loaded = Workspace::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert_eq!(loaded, Workspace::default());
    }

    // ── known_repos union ─────────────────────────────────────────────────────

    fn machine_with_repos(names: &[&str]) -> super::super::types::Machine {
        super::super::types::Machine {
            name: "m1".to_string(),
            host: String::new(),
            reachable: true,
            active_count: 0,
            repos: repos(names),
            version: None,
            worktree_bytes: 0,
        }
    }

    fn open_issue_for(repo: &str) -> super::super::types::OpenIssue {
        super::super::types::OpenIssue {
            repo_name: repo.to_string(),
            number: 1,
            title: "issue".to_string(),
            body: String::new(),
            labels: Vec::new(),
            state: "open".to_string(),
            milestone_number: None,
            milestone_title: None,
            body_truncated: false,
            body_len: None,
            // #1941: fields added to the generated wire DTO; not exercised by this test.
            synced_at: None,
        }
    }

    #[test]
    fn known_repos_unions_machines_assignments_and_open_issues() {
        use super::super::fixtures::make_assignment_typed;
        let data = BoardData {
            machines: vec![machine_with_repos(&["m-repo"])],
            assignments: vec![make_assignment_typed("done", 1, "a-repo", Some("work"))],
            open_issues: vec![open_issue_for("oi-repo")],
            ..BoardData::default()
        };
        assert_eq!(known_repos(&data), repos(&["a-repo", "m-repo", "oi-repo"]));
    }

    #[test]
    fn known_repos_dedups_across_sources() {
        use super::super::fixtures::make_assignment_typed;
        let data = BoardData {
            machines: vec![machine_with_repos(&["shared"])],
            assignments: vec![make_assignment_typed("done", 1, "shared", Some("work"))],
            ..BoardData::default()
        };
        assert_eq!(known_repos(&data), repos(&["shared"]));
    }

    #[test]
    fn known_repos_empty_board_is_empty() {
        assert!(known_repos(&BoardData::default()).is_empty());
    }

    // ── reconcile_workspace (sync_workspace_repos' pure core) ─────────────────

    #[test]
    fn reconcile_derives_fresh_when_nothing_open_and_reports_changed() {
        let mut ws = Workspace::default();
        let changed = reconcile_workspace(&mut ws, &repos(&["b", "a"]));
        assert!(changed, "deriving from empty must report a change");
        assert_eq!(ws.open_projects(), &["a", "b"]);
        assert_eq!(ws.active_project(), Some("a"));
    }

    #[test]
    fn reconcile_drops_stale_repo_and_reports_changed() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b", "c"]));
        ws.set_active("b");
        let changed = reconcile_workspace(&mut ws, &repos(&["a", "c"]));
        assert!(changed, "dropping a stale repo must report a change");
        assert_eq!(ws.open_projects(), &["a", "c"]);
        assert_eq!(ws.active_project(), Some("a"));
    }

    #[test]
    fn reconcile_all_stale_rederives_from_known() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b"]));
        // Every previously-open repo vanished from config; the known set
        // now only has repos never seen before.
        let changed = reconcile_workspace(&mut ws, &repos(&["c", "d"]));
        assert!(changed);
        assert_eq!(ws.open_projects(), &["c", "d"]);
        assert_eq!(ws.active_project(), Some("c"));
    }

    #[test]
    fn reconcile_noop_when_nothing_changed() {
        let mut ws = Workspace::derive_from_repos(&repos(&["a", "b"]));
        let changed = reconcile_workspace(&mut ws, &repos(&["a", "b"]));
        assert!(!changed, "reconciling against an already-consistent set must be a no-op");
    }
}
