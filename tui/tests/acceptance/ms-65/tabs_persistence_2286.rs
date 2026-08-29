// Sealed acceptance slice for **issue #2286** — coord-tui: persist each
// panel's tab set across a restart (`~/.coord/tabs.json`), pruning documents
// whose issue no longer exists — milestone ms-65 (tracking issue #2289,
// "coord-tui: per-panel document tabs (preview/pin)").
//
// Authored independently from `tests/acceptance/ms-65/contract.md` (Gate A),
// with **zero** worker/implementation context: no work branch, PR or commit
// for #2286 (or any other ms-65 issue) was read. Every assertion below is
// derived from contract §6 ("Persistence — `~/.coord/tabs.json`") — including
// the JSON shape §6 pins verbatim, which Note 5 states is exactly "what
// `coord acceptance author` will write fixtures against" — plus the issue
// body's own acceptance criteria, which §6 restates.
//
// §6 declares itself mock-less ("No visual mock (this is a file-format
// contract, not a screen)"); the mocks this slice leans on are only the ones
// its *rendered* halves need — `mocks/board-baseline-no-tabs.screen` (the
// zero-tab state a missing/malformed file must produce) and
// `mocks/board-pinned-3-tabs.screen` (what three restored Board tabs must
// look like, §2c's `[<label> ×]` active bracket included).
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-65 mock declares (contract §0), and reads/writes the
// real `~/.coord/tabs.json` through a sandboxed `HOME` (harness note 1).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2286 may run it
// (`coord acceptance run --issue 2286`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §6 only. §2 (#2282 the Board strip, the label budget, the active
// bracket, the open/preview/pin semantics) and §3 (#2284 Pipeline's own set)
// are *preconditions* of everything here — a tab set has to exist before it
// can be persisted, and it has to render before a restored one can be read
// off the grid — but they are those issues' slices
// (`board_tabs_2282.rs`, `pipeline_tabs_2284.rs`) and no clause of theirs is
// re-asserted here as this slice's subject. §4 (#2283 close/navigate/
// overflow), §5 (#2285 per-tab sub-state), §8 (#2287 discoverability) and §9
// (#2288 split) are untouched.
//
// ── Harness facts this slice had to design around ─────────────────────────
//
// 1. **`HOME` is the only seam onto `~/.coord/`, and it is process-global.**
//    Every `~/.coord/…` path in this app resolves through
//    `std::env::var_os("HOME")` (`TuiSettings::path`, `Workspace::path`,
//    `app::data::coord_dir` — the three precedents §6 names). There is no
//    `COORD_HOME`-style override, so a test that must not clobber the
//    developer's real `~/.coord/tabs.json` has to swap `HOME` for a temp
//    directory for the duration of the test. `HomeSandbox` below does that,
//    serialised behind a slice-global mutex so two of these tests never
//    overlap, and restores the previous value (and deletes the temp tree) on
//    drop — including on panic. The window is still process-global, i.e.
//    concurrent tests from OTHER slices observe the sandboxed `HOME` while it
//    is held; flagged for the coordinator in `manifest.yml`, since the only
//    real fix is an injectable coord-dir seam.
//
// 2. **The suite's only app-construction seam is #2281's
//    `make_app_with_board_json`.** `CoordApp::new()` is public but builds the
//    real, daemon-backed app with an EMPTY board, against which every
//    persisted document would be pruned on load (§6: "drop any document whose
//    issue is absent from the board") — so it cannot witness a restore at all.
//    Consequently the restore half of §6 is observable in a sealed acceptance
//    suite only if the load runs on the fixture path. Flagged for the
//    coordinator in `manifest.yml` (finding 14): this is a real constraint on
//    #2286's implementation, not a decision this slice is entitled to make
//    quietly.
//
// 3. **When the file is written is not pinned by §6** — only that a restart
//    restores. `saved_tabs_json()` therefore reads the file after the tab
//    edits, and if it is not there yet sends a `q` (the status bar's own quit
//    key) and reads again, so an implementation that flushes on tab mutation
//    and one that flushes on quit both satisfy these tests. Only a run that
//    produces no file at all fails them.
//
// 4. **Preview state is read as contract §1's `∘ ` marker, never as style.**
//    Tab counts are `×` counts in the strip row and the active tab is §2c's
//    `[<label> ×]` bracket, exactly as §2e instructs — no styled-cell read is
//    performed anywhere in this slice.
//
// 5. **Entering the Pipeline panel is a known live harness blocker** (already
//    filed by the #2284 slice as manifest finding 7): the panel switch kicks
//    `maybe_kick_pipeline_loader` → `refresh` → `start_data_load`, whose
//    fixture short-circuit is `#[cfg(test)]`-only and therefore inert in this
//    external test crate, so the seeded board can be replaced wholesale by
//    whatever the machine's real `~/.coord/coord.db` holds. Under this slice's
//    sandboxed `HOME` that database does not exist, which changes the failure
//    mode but not its existence. The one Pipeline-side test below labels every
//    step of that switch `HARNESS PRECONDITION (NOT a #2286 defect …)` so the
//    two failure modes stay distinguishable from a JSON report alone.

mod tabs_persistence_2286 {
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Mutex, MutexGuard};

    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::AppLogic;
    use serde_json::Value;

    // ═══════════════════════════════════════════════════════════════════════
    // Fixture
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §7's fixture in the two disjoint ranges §3b's independence
    /// pair is drawn in — #101–#105 for the Board, #201–#203 for the Pipeline
    /// — because §6's own pinned JSON example persists both scopes at once
    /// (`"board"` holding #101–#103, `"pipeline"` holding #201–#202) and the
    /// issue's first acceptance criterion is "three Board tabs plus two
    /// Pipeline tabs are restored to both panels".
    ///
    /// Titles are the mocks' own. The Pipeline sidebar lists only issues
    /// carrying a tracked label, so #201–#203 get `coord` and #101–#105 an
    /// untracked one (the Board sidebar does not filter by label and lists
    /// all eight).
    ///
    /// **#199 is deliberately NOT in this set** — it is the "issue has been
    /// closed and dropped from the board" document §6's pruning clauses need.
    const BOARD_JSON: &str = r#"{
      "issues": [
        {"repo_name": "claude-coordinator", "number": 101, "title": "Fix login race timeout", "state": "open", "labels": ["board-only"]},
        {"repo_name": "claude-coordinator", "number": 102, "title": "Auth token refresh bug", "state": "open", "labels": ["board-only"]},
        {"repo_name": "claude-coordinator", "number": 103, "title": "Race condition in poller", "state": "open", "labels": ["board-only"]},
        {"repo_name": "claude-coordinator", "number": 104, "title": "Flaky CI on macOS runners", "state": "open", "labels": ["board-only"]},
        {"repo_name": "claude-coordinator", "number": 105, "title": "Memory leak in watch loop", "state": "open", "labels": ["board-only"]},
        {"repo_name": "claude-coordinator", "number": 201, "title": "Add retry backoff to fetch", "state": "open", "labels": ["coord"]},
        {"repo_name": "claude-coordinator", "number": 202, "title": "Migrate settings to TOML v2", "state": "open", "labels": ["coord"]},
        {"repo_name": "claude-coordinator", "number": 203, "title": "Dark theme contrast pass", "state": "open", "labels": ["coord"]}
      ],
      "board_meta": {
        "pipeline_repos": "{\"claude-coordinator\": \"JDonaghy/claude-coordinator\"}",
        "pipeline_tracked_labels": "[\"coord\"]",
        "pipeline_default_gates": "[\"review\", \"merge\"]"
      }
    }"#;

    /// The repo local-name every persisted document key in this slice carries
    /// (§6's pinned shape: `{"repo": "claude-coordinator", "issue": 101}`).
    const REPO: &str = "claude-coordinator";

    /// Board sidebar-row click targets. The mocks draw these as
    /// `"#102 Auth token refresh bug"`; the shipped 35-column sidebar renders
    /// two spaces after the number and truncates the title, so these are taken
    /// from a real 120×40 render.
    const ROW_101: &str = "#101  Fix login race";
    const ROW_102: &str = "#102  Auth token refresh";
    const ROW_103: &str = "#103  Race condition in";
    const ROW_104: &str = "#104  Flaky CI on macOS";

    /// Activity-bar icons, contract §0: `B` = Board (row 0), `▶` = Pipeline
    /// (row 2), both painted in columns 0–1.
    const PIPELINE_ICON: char = '▶';

    /// Contract §0: sidebar content is columns 3–37, main-panel content is
    /// columns 38–119.
    const SIDEBAR_COLS: std::ops::Range<usize> = 3..38;
    const MAIN_START_COL: usize = 38;

    // ═══════════════════════════════════════════════════════════════════════
    // `HOME` sandbox (harness note 1)
    // ═══════════════════════════════════════════════════════════════════════

    /// Serialises every test in this slice: `HOME` is process-global, so two
    /// sandboxes must never be live at once.
    static HOME_LOCK: Mutex<()> = Mutex::new(());
    /// Makes each sandbox directory unique even within one process.
    static SANDBOX_SEQ: AtomicUsize = AtomicUsize::new(0);

    /// A temporary `HOME` with an empty `.coord/` inside it. Restores the
    /// previous `HOME` and deletes the tree on drop — including on panic, so
    /// a failing test never leaks the swap into the rest of the suite.
    struct HomeSandbox {
        home: PathBuf,
        previous: Option<std::ffi::OsString>,
        // Declared last so it is released last (fields drop in declaration
        // order): `HOME` is restored before the next test may take the lock.
        _guard: MutexGuard<'static, ()>,
    }

    impl HomeSandbox {
        fn new(tag: &str) -> Self {
            let guard = HOME_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            let seq = SANDBOX_SEQ.fetch_add(1, Ordering::SeqCst);
            let home = std::env::temp_dir().join(format!(
                "coord-ms65-2286-{}-{}-{}",
                std::process::id(),
                seq,
                tag
            ));
            let _ = std::fs::remove_dir_all(&home);
            std::fs::create_dir_all(home.join(".coord")).unwrap_or_else(|e| {
                panic!("harness: could not create the sandbox HOME at {home:?}: {e}")
            });
            let previous = std::env::var_os("HOME");
            // SAFETY: set_var is `unsafe` in recent stdlib. `HOME_LOCK` is
            // held for the lifetime of this guard (released last, per the
            // field-order comment above), so no other thread observes or
            // mutates `HOME` concurrently with this write.
            unsafe {
                std::env::set_var("HOME", &home);
            }
            Self {
                home,
                previous,
                _guard: guard,
            }
        }

        /// `~/.coord/tabs.json` — the exact path §6 names.
        fn tabs_path(&self) -> PathBuf {
            self.home.join(".coord").join("tabs.json")
        }

        fn write_tabs_json(&self, contents: &str) {
            let path = self.tabs_path();
            std::fs::write(&path, contents)
                .unwrap_or_else(|e| panic!("harness: could not seed {path:?}: {e}"));
        }

        fn read_tabs_json(&self) -> Option<String> {
            std::fs::read_to_string(self.tabs_path()).ok()
        }
    }

    impl Drop for HomeSandbox {
        fn drop(&mut self) {
            // SAFETY: set_var/remove_var are `unsafe` in recent stdlib.
            // `_guard` (holding `HOME_LOCK`) is still live at this point —
            // it is declared last on the struct and so drops after this
            // body runs — so no other thread observes or mutates `HOME`
            // concurrently with this write.
            unsafe {
                match &self.previous {
                    Some(prev) => std::env::set_var("HOME", prev),
                    None => std::env::remove_var("HOME"),
                }
            }
            let _ = std::fs::remove_dir_all(&self.home);
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §6's pinned file shape — writers and readers
    //
    // §6 pins the shape verbatim and Note 5 states it is "what `coord
    // acceptance author` will write fixtures against", so the field names
    // (`tabs` / `active` / `preview`, and a document key's `repo` / `issue`)
    // are asserted as written. A deviation is, in the contract's own words,
    // "an amendment, not a silent implementation choice".
    // ═══════════════════════════════════════════════════════════════════════

    /// One document key, §6's shape: `{"repo": …, "issue": …}`.
    fn key(issue: u32) -> String {
        format!("{{\"repo\": \"{REPO}\", \"issue\": {issue}}}")
    }

    /// One scope's object: ordered `tabs`, `active`, `preview`.
    fn scope(tabs: &[u32], active: Option<u32>, preview: Option<u32>) -> String {
        let tabs = tabs.iter().map(|n| key(*n)).collect::<Vec<_>>().join(", ");
        let one = |n: Option<u32>| n.map(key).unwrap_or_else(|| "null".to_string());
        format!(
            "{{\"tabs\": [{tabs}], \"active\": {}, \"preview\": {}}}",
            one(active),
            one(preview)
        )
    }

    /// A whole `tabs.json`, §6's top-level shape (lowercase `PanelScope`
    /// names; "a scope absent from the file starts with no tabs", so an
    /// omitted scope is simply not written).
    fn tabs_json(board: Option<String>, pipeline: Option<String>) -> String {
        let mut parts = Vec::new();
        if let Some(b) = board {
            parts.push(format!("\"board\": {b}"));
        }
        if let Some(p) = pipeline {
            parts.push(format!("\"pipeline\": {p}"));
        }
        format!("{{{}}}", parts.join(", "))
    }

    /// Parse the saved file, with §6's shape quoted in the failure message.
    fn parse_saved(raw: &str) -> Value {
        serde_json::from_str::<Value>(raw).unwrap_or_else(|e| {
            panic!(
                "contract §6: `~/.coord/tabs.json` must be JSON of the pinned shape \
                 (`{{\"board\": {{\"tabs\": [{{\"repo\": …, \"issue\": …}}], \"active\": …, \
                 \"preview\": …}}, \"pipeline\": {{…}}}}`) — it does not parse as JSON at \
                 all: {e}\n--- file ---\n{raw}"
            )
        })
    }

    /// A scope's object out of a parsed file.
    fn saved_scope<'a>(saved: &'a Value, scope_name: &str, raw: &str) -> &'a Value {
        saved.get(scope_name).unwrap_or_else(|| {
            panic!(
                "contract §6: `tabs.json`'s top-level keys are the lowercase `PanelScope` \
                 names — `{scope_name}` is missing.\n--- file ---\n{raw}"
            )
        })
    }

    /// A scope's ordered issue numbers (§6: "`tabs` is the **ordered** list of
    /// open document keys (order = strip order)").
    fn saved_tab_issues(saved: &Value, scope_name: &str, raw: &str) -> Vec<u64> {
        let scope = saved_scope(saved, scope_name, raw);
        let tabs = scope.get("tabs").and_then(Value::as_array).unwrap_or_else(|| {
            panic!(
                "contract §6: scope `{scope_name}` must carry a `tabs` ARRAY of document \
                 keys, in strip order.\n--- file ---\n{raw}"
            )
        });
        tabs.iter()
            .map(|entry| {
                entry
                    .get("issue")
                    .and_then(Value::as_u64)
                    .unwrap_or_else(|| {
                        panic!(
                            "contract §6: every entry of `{scope_name}.tabs` is a document key \
                             `{{\"repo\": …, \"issue\": <number>}}` — {entry} has no numeric \
                             `issue`.\n--- file ---\n{raw}"
                        )
                    })
            })
            .collect()
    }

    /// A scope's `active` (or `preview`) issue number — `None` for JSON
    /// `null`, which §6 pins as "no tabs" / "no preview slot".
    fn saved_key_field(saved: &Value, scope_name: &str, field: &str, raw: &str) -> Option<u64> {
        let scope = saved_scope(saved, scope_name, raw);
        let value = scope.get(field).unwrap_or_else(|| {
            panic!(
                "contract §6: scope `{scope_name}` must carry a `{field}` field — a document \
                 key, or `null`.\n--- file ---\n{raw}"
            )
        });
        if value.is_null() {
            return None;
        }
        Some(
            value
                .get("issue")
                .and_then(Value::as_u64)
                .unwrap_or_else(|| {
                    panic!(
                        "contract §6: `{scope_name}.{field}` is either `null` or a document key \
                         `{{\"repo\": …, \"issue\": <number>}}` — it is {value}.\n\
                         --- file ---\n{raw}"
                    )
                }),
        )
    }

    // ═══════════════════════════════════════════════════════════════════════
    // Grid + driving helpers
    //
    // Everything in this block is a *precondition* harness, not a #2286
    // clause: each panic message says so, so a failure here reads as a
    // fixture/baseline finding rather than as broken persistence.
    // ═══════════════════════════════════════════════════════════════════════

    /// Screen rows, 0-indexed, as the grid the mocks are written in.
    fn rows<A: AppLogic>(driver: &TuiDriver<A>) -> Vec<String> {
        driver.screen().lines().map(str::to_string).collect()
    }

    /// A row's main-panel slice (contract §0: columns 38–119).
    fn main_slice(row: &str) -> String {
        row.chars().skip(MAIN_START_COL).collect()
    }

    /// The document tab strip: the first row carrying the §2d close glyph `×`
    /// (U+00D7) in its main-panel columns. `None` when no tab is open (§2a:
    /// the strip then "renders nothing and reserves no row").
    ///
    /// Unambiguous: Board's `[P]urge` toolbar button uses `✕` (U+2715), a
    /// different code point.
    fn strip<A: AppLogic>(driver: &TuiDriver<A>) -> Option<String> {
        rows(driver)
            .into_iter()
            .find(|r| main_slice(r).contains('×'))
            .map(|r| main_slice(&r))
    }

    /// The strip row's text, or a diagnosis naming the clause that expected
    /// it. `what` names the scenario so a JSON report alone is legible.
    fn strip_text<A: AppLogic>(driver: &TuiDriver<A>, what: &str) -> String {
        strip(driver).unwrap_or_else(|| {
            panic!(
                "contract §6: {what} — no document tab strip is rendered at all (no \
                 main-panel row carries the §2d `×` close glyph), i.e. nothing was \
                 restored.\n--- screen ---\n{}",
                driver.screen()
            )
        })
    }

    /// How many tabs the strip shows, counted the way contract §2e mandates:
    /// `×` occurrences in the strip row, never colour or style.
    fn tab_count(strip_text: &str) -> usize {
        strip_text.matches('×').count()
    }

    /// The labels of the strip's bracketed (i.e. §2c-active) tabs. A correct
    /// strip has exactly one.
    fn bracketed(strip_text: &str) -> Vec<String> {
        let mut out = Vec::new();
        let mut rest = strip_text;
        while let Some(open) = rest.find('[') {
            let after = &rest[open + 1..];
            match after.find(']') {
                Some(close) => {
                    out.push(after[..close].to_string());
                    rest = &after[close + 1..];
                }
                None => break,
            }
        }
        out
    }

    /// The single active tab's label (§2c: `"[<label> ×]"`).
    fn active_label(strip_text: &str, what: &str, screen: &str) -> String {
        let all = bracketed(strip_text);
        assert_eq!(
            all.len(),
            1,
            "contract §2c (a PRECONDITION of §6, owned by #2282): exactly one tab is active \
             and it is the bracketed one — {what} produced {} bracketed segments \
             ({all:?}). Strip row was {strip_text:?}.\n--- screen ---\n{screen}",
            all.len(),
        );
        all.into_iter().next().unwrap()
    }

    /// Click point for the first occurrence of `needle` inside the **sidebar**
    /// columns (§0: 3–37), so a `#<N>` tag rendered in the main panel (a tab
    /// label, a detail-pane header) can never be mistaken for the sidebar row
    /// that opens it.
    fn sidebar_hit<A: AppLogic>(driver: &TuiDriver<A>, needle: &str) -> Option<(f32, f32)> {
        for (y, row) in rows(driver).into_iter().enumerate() {
            let chars: Vec<char> = row.chars().collect();
            let end = SIDEBAR_COLS.end.min(chars.len());
            if end <= SIDEBAR_COLS.start {
                continue;
            }
            let band: String = chars[SIDEBAR_COLS.start..end].iter().collect();
            if let Some(off) = band.find(needle) {
                let col = SIDEBAR_COLS.start + band[..off].chars().count();
                return Some((col as f32 + 0.5, y as f32 + 0.5));
            }
        }
        None
    }

    /// A driver on the contract's pinned 120×40 grid (§0), seeded with the
    /// fixture above. `HOME` must already be sandboxed by the caller.
    fn driver_120x40() -> TuiDriver<impl AppLogic> {
        let app = make_app_with_board_json(BOARD_JSON);
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        driver.set_double_click_folding(false);
        driver.render();
        driver
    }

    /// A restart: seed `~/.coord/tabs.json`, then build a fresh app exactly
    /// the way every other slice in this suite builds one.
    fn restart_with(sandbox: &HomeSandbox, file: &str) -> TuiDriver<impl AppLogic> {
        sandbox.write_tabs_json(file);
        driver_120x40()
    }

    /// Board panel with the fixture's sidebar issue rows revealed (the seeded
    /// repo's `No milestone` group is collapsed by default, #857).
    fn board_driver() -> TuiDriver<impl AppLogic> {
        let mut driver = driver_120x40();
        if sidebar_hit(&driver, ROW_101).is_none() {
            let before = driver.screen();
            let (x, y) = sidebar_hit(&driver, "No milestone").unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2286 clause): the Board sidebar must render the \
                     seeded repo's collapsed \"No milestone\" group header for the fixture — \
                     not found.\n--- screen ---\n{before}"
                )
            });
            driver.click(x, y);
            driver.render();
        }
        assert!(
            sidebar_hit(&driver, ROW_101).is_some(),
            "ms-65 baseline (NOT a #2286 clause): the Board sidebar must render a row for \
             the fixture's issue #101.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver
    }

    /// Single-click a Board sidebar row (§2e rule 1: opens/replaces the one
    /// preview tab).
    fn click_row<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let (x, y) = sidebar_hit(driver, row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2286 clause): Board sidebar row {row:?} must be on \
                 screen to click.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.render();
    }

    /// Double-click a Board sidebar row: opens-or-activates its document tab,
    /// then promotes it to pinned (§2e rule 3).
    fn dbl_click_row<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let (x, y) = sidebar_hit(driver, row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2286 clause): Board sidebar row {row:?} must be on \
                 screen to double-click.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.double_click(x, y);
        driver.render();
    }

    /// Switch panels the way an operator does: click the panel's activity-bar
    /// icon, which §0 pins to columns 0–1.
    fn switch_panel<A: AppLogic>(driver: &mut TuiDriver<A>, icon: char) {
        let target = rows(driver).into_iter().enumerate().find_map(|(y, row)| {
            let chars: Vec<char> = row.chars().collect();
            chars
                .iter()
                .take(SIDEBAR_COLS.start.min(chars.len()))
                .position(|c| *c == icon)
                .map(|x| (x as f32 + 0.5, y as f32 + 0.5))
        });
        let (x, y) = target.unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2286 clause): contract §0 pins the activity bar to \
                 columns 0–1, with `{icon}` as a panel icon — no such icon \
                 there.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.render();
    }

    /// The `tabs.json` the app has written, parsed. Reads after the tab edits
    /// and — if nothing is there yet — after a `q` quit, so an implementation
    /// that flushes on mutation and one that flushes on exit both pass
    /// (harness note 3: §6 pins the restart contract, not the save trigger).
    fn saved_tabs_json<A: AppLogic>(driver: &mut TuiDriver<A>, sandbox: &HomeSandbox) -> String {
        if let Some(raw) = sandbox.read_tabs_json() {
            return raw;
        }
        driver.type_char('q');
        driver.render();
        sandbox.read_tabs_json().unwrap_or_else(|| {
            panic!(
                "contract §6 / issue #2286: each panel's tab set is persisted to \
                 `~/.coord/tabs.json` so it survives a restart — no such file exists after \
                 opening tabs (checked after the tab edits and again after a `q` quit, since \
                 §6 does not pin WHEN the file is written). Expected at {:?}.\n\
                 --- screen ---\n{}",
                sandbox.tabs_path(),
                driver.screen()
            )
        })
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §6 — what gets written
    // ═══════════════════════════════════════════════════════════════════════

    /// §6 bullet 1: "Writing 3 Board tabs […] produces the exact shape above"
    /// — `tabs` in strip order, `active` the active document, `preview` null
    /// when every open tab is pinned.
    ///
    /// The three tabs are opened with three double-clicks, the sequence §2e
    /// traces to `mocks/board-pinned-3-tabs.screen`'s end state (each row not
    /// yet open at the moment it is clicked, so no preview slot is ever
    /// occupied and none is replaced).
    #[test]
    fn three_board_tabs_are_saved_to_tabs_json_in_strip_order_with_the_active_tab() {
        let sandbox = HomeSandbox::new("save-board");
        let mut driver = board_driver();
        dbl_click_row(&mut driver, ROW_101);
        dbl_click_row(&mut driver, ROW_102);
        dbl_click_row(&mut driver, ROW_103);

        let text = strip_text(&driver, "three double-clicked Board rows must open three tabs");
        assert_eq!(
            tab_count(&text),
            3,
            "contract §2e (a PRECONDITION of §6, owned by #2282): three double-clicks on \
             #101/#102/#103 must leave three pinned Board tabs open — §6's first testable \
             clause is about persisting exactly that set. Strip row was {text:?}.\n\
             --- screen ---\n{}",
            driver.screen(),
        );

        let raw = saved_tabs_json(&mut driver, &sandbox);
        let saved = parse_saved(&raw);
        assert_eq!(
            saved_tab_issues(&saved, "board", &raw),
            vec![101, 102, 103],
            "contract §6: `board.tabs` is the ORDERED list of open document keys (order = \
             strip order) — #101, #102 then #103, the order they were opened in.\n\
             --- file ---\n{raw}",
        );
        assert_eq!(
            saved_key_field(&saved, "board", "active", &raw),
            Some(103),
            "contract §6: `active` is the scope's active document — §2e rule 3 activates \
             what it promotes, so the last double-clicked tab (#103) is active, exactly as \
             §6's own example (`\"active\": {{\"repo\": …, \"issue\": 103}}`) shows.\n\
             --- file ---\n{raw}",
        );
        assert_eq!(
            saved_key_field(&saved, "board", "preview", &raw),
            None,
            "contract §6: `preview` is `null` when there is no preview slot, i.e. when \
             every open tab is pinned — all three of these were promoted by a double click \
             (§2e rule 3).\n--- file ---\n{raw}",
        );
    }

    /// §6: "`preview` is […] the key of the one preview tab — which, per §2e,
    /// must also appear in `tabs`."
    ///
    /// A single click opens a preview tab (§2e rule 1), so the saved scope
    /// must record it as the preview slot rather than as a plain pinned tab —
    /// the file-level half of "a preview tab is restored as a preview tab,
    /// not promoted".
    #[test]
    fn a_preview_tab_is_saved_as_the_scopes_preview_slot() {
        let sandbox = HomeSandbox::new("save-preview");
        let mut driver = board_driver();
        dbl_click_row(&mut driver, ROW_101);
        click_row(&mut driver, ROW_102);

        let text = strip_text(&driver, "one pinned tab plus one single-clicked preview tab");
        assert_eq!(
            tab_count(&text),
            2,
            "contract §2e (a PRECONDITION of §6, owned by #2282): a double click on #101 \
             followed by a single click on #102 leaves two tabs — #101 pinned, #102 the one \
             preview. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let raw = saved_tabs_json(&mut driver, &sandbox);
        let saved = parse_saved(&raw);
        assert_eq!(
            saved_tab_issues(&saved, "board", &raw),
            vec![101, 102],
            "contract §6: the preview tab is an open tab like any other and \"must also \
             appear in `tabs`\", in strip order.\n--- file ---\n{raw}",
        );
        assert_eq!(
            saved_key_field(&saved, "board", "preview", &raw),
            Some(102),
            "contract §6: `preview` records WHICH open tab is the preview slot — #102 was \
             opened with a single click (§2e rule 1) and is therefore the preview. Saving \
             `null` here loses the distinction and is what \"restored as a preview tab, not \
             promoted\" (issue #2286) forbids.\n--- file ---\n{raw}",
        );
        assert_eq!(
            saved_key_field(&saved, "board", "active", &raw),
            Some(102),
            "contract §6: `active` is the active document — §2e rule 1 activates the \
             preview tab it opens.\n--- file ---\n{raw}",
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §6 — what gets restored
    // ═══════════════════════════════════════════════════════════════════════

    /// §6 bullet 1 + issue #2286 AC 1: three Board tabs are restored after a
    /// restart, "with each panel's active tab preserved". This is the Board
    /// half; `pipeline_tabs_are_restored_to_the_pipeline_panel_with_its_own_active_tab`
    /// is the other.
    ///
    /// The seeded file is §6's own pinned example, verbatim in shape.
    #[test]
    fn board_tabs_are_restored_from_tabs_json_after_a_restart() {
        let sandbox = HomeSandbox::new("restore-board");
        let driver = restart_with(
            &sandbox,
            &tabs_json(
                Some(scope(&[101, 102, 103], Some(103), None)),
                Some(scope(&[201, 202], Some(202), None)),
            ),
        );

        let text = strip_text(
            &driver,
            "a restart with three Board documents in `~/.coord/tabs.json` must restore three \
             Board tabs",
        );
        assert_eq!(
            tab_count(&text),
            3,
            "contract §6 / issue #2286 AC 1: the three documents persisted under `board` are \
             restored as three tabs (`×` count, §2e's own counting rule). Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for issue in ["#101", "#102", "#103"] {
            assert!(
                text.contains(issue),
                "contract §6: every persisted `board.tabs` entry is restored — {issue} is \
                 missing from the strip. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
        assert!(
            !text.contains("#201") && !text.contains("#202"),
            "contract §6 + §3b: the scopes are persisted and restored SEPARATELY — the \
             `pipeline` scope's documents must not appear in the Board's strip. Strip row \
             was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let active = active_label(&text, "a restored Board strip", &driver.screen());
        assert!(
            active.contains("#103"),
            "contract §6 / issue #2286 AC 1 (\"with each panel's active tab preserved\"): the \
             persisted `board.active` (#103) must be the active tab after the restart — §2c \
             renders the active tab bracketed, and the bracketed one is {active:?}.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §6 + issue #2286 AC 2: "A preview tab is restored as a preview tab, not
    /// promoted." Read through contract §1's plain-text `∘ ` marker, which is
    /// the pinned way to tell preview from pinned in a symbols-only grid.
    #[test]
    fn a_restored_preview_tab_is_still_a_preview_tab() {
        let sandbox = HomeSandbox::new("restore-preview");
        let driver = restart_with(
            &sandbox,
            &tabs_json(Some(scope(&[101, 102], Some(102), Some(102))), None),
        );

        let text = strip_text(
            &driver,
            "a restart with two Board documents (one of them the preview) must restore two \
             Board tabs",
        );
        assert_eq!(
            tab_count(&text),
            2,
            "contract §6: both persisted documents are restored — the preview tab is an open \
             tab like any other. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            text.contains("∘ #102"),
            "contract §6 + §1 / issue #2286 AC 2: the persisted `preview` document (#102) is \
             restored AS the preview tab, which §1 pins as carrying the plain-text `∘ ` \
             marker immediately before its label. The strip has no `∘ #102`, i.e. the \
             preview was promoted to a pinned tab on load. Strip row was {text:?}.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains("∘ #101"),
            "contract §6 + §2e rule 4: there is at most ONE preview tab per group, and the \
             persisted one is #102 — #101 was pinned and must be restored pinned (no `∘ ` \
             marker). Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §6 bullet 1 + issue #2286 AC 1, the other half: "two Pipeline tabs are
    /// restored […] with each panel's active tab preserved", from the same
    /// file that restored the Board's three. §3b's independence applies to the
    /// restored sets exactly as it does to live ones.
    ///
    /// See this slice's harness note 5 for the live blocker on entering the
    /// Pipeline panel from an external test crate (already filed by the #2284
    /// slice as manifest finding 7); every step of the switch is labelled a
    /// harness precondition so the two failure modes stay distinguishable.
    #[test]
    fn pipeline_tabs_are_restored_to_the_pipeline_panel_with_its_own_active_tab() {
        let sandbox = HomeSandbox::new("restore-pipeline");
        let mut driver = restart_with(
            &sandbox,
            &tabs_json(
                Some(scope(&[101, 102, 103], Some(103), None)),
                Some(scope(&[201, 202], Some(202), Some(202))),
            ),
        );

        switch_panel(&mut driver, PIPELINE_ICON);
        assert!(
            driver.screen_contains("Overview"),
            "HARNESS PRECONDITION (NOT a #2286 defect — see this slice's harness note 5 and \
             manifest finding 7): clicking the `▶` activity-bar icon must land on the \
             Pipeline panel, whose sub-tab bar starts with `Overview`.\n--- screen ---\n{}",
            driver.screen(),
        );

        let text = strip_text(
            &driver,
            "a restart with two Pipeline documents in `~/.coord/tabs.json` must restore two \
             Pipeline tabs",
        );
        assert_eq!(
            tab_count(&text),
            2,
            "contract §6 / issue #2286 AC 1: the two documents persisted under `pipeline` are \
             restored to the PIPELINE panel. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for issue in ["#201", "#202"] {
            assert!(
                text.contains(issue),
                "contract §6: every persisted `pipeline.tabs` entry is restored — {issue} is \
                 missing from the Pipeline strip. Strip row was {text:?}.\n\
                 --- screen ---\n{}",
                driver.screen(),
            );
        }
        assert!(
            !text.contains("#101") && !text.contains("#102") && !text.contains("#103"),
            "contract §6 + §3b: each scope is persisted under its own key and restored to its \
             own panel — the `board` scope's documents must never appear in the Pipeline's \
             strip. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let active = active_label(&text, "a restored Pipeline strip", &driver.screen());
        assert!(
            active.contains("#202"),
            "contract §6 / issue #2286 AC 1 (\"with each panel's active tab preserved\"): the \
             persisted `pipeline.active` (#202) must be the Pipeline's active tab after the \
             restart, independently of the Board's. The bracketed tab is {active:?}.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §6 — pruning documents whose issue is gone
    // ═══════════════════════════════════════════════════════════════════════

    /// §6 bullet 2 + issue #2286 AC 3: "A tab whose `issue` number no longer
    /// exists in the loaded board is dropped on load, never rendered, never
    /// round-tripped back into a re-saved file."
    ///
    /// Both halves are asserted: #199 (absent from the fixture board) is not
    /// rendered after the restart, and it is not written back when the file is
    /// next saved. The persisted `active` is a surviving document here, so
    /// this test isolates pruning from the active-was-pruned rule below.
    #[test]
    fn a_document_whose_issue_is_absent_from_the_board_is_pruned_on_load() {
        let sandbox = HomeSandbox::new("prune-dead");
        let mut driver = restart_with(
            &sandbox,
            &tabs_json(Some(scope(&[101, 199, 102], Some(101), None)), None),
        );

        let text = strip_text(
            &driver,
            "a restart with two live documents and one dead one must still restore the two \
             live tabs",
        );
        assert!(
            !text.contains("#199"),
            "contract §6 / issue #2286 AC 3: a persisted document whose issue is absent from \
             the loaded board (#199 is not in the fixture) is dropped on load and NEVER \
             RENDERED — \"rather than rendering a dead tab\". Strip row was {text:?}.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            tab_count(&text),
            2,
            "contract §6: pruning drops ONLY the dead document — #101 and #102 both exist in \
             the loaded board and must both be restored. Strip row was {text:?}.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        for issue in ["#101", "#102"] {
            assert!(
                text.contains(issue),
                "contract §6: the surviving documents are restored in their persisted order — \
                 {issue} is missing. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }

        // …and it is never round-tripped back. Opening a further tab is the
        // cheapest way to make the app re-save whatever it now holds.
        if sidebar_hit(&driver, ROW_104).is_none() {
            if let Some((x, y)) = sidebar_hit(&driver, "No milestone") {
                driver.click(x, y);
                driver.render();
            }
        }
        dbl_click_row(&mut driver, ROW_104);
        let raw = saved_tabs_json(&mut driver, &sandbox);
        let saved = parse_saved(&raw);
        let issues = saved_tab_issues(&saved, "board", &raw);
        assert!(
            !issues.contains(&199),
            "contract §6 / issue #2286 AC 3: a pruned document is \"never round-tripped back \
             into a re-saved file\" — #199 was dropped on load but reappears in the file the \
             app wrote afterwards ({issues:?}).\n--- file ---\n{raw}",
        );
        assert!(
            issues.contains(&101) && issues.contains(&102) && issues.contains(&104),
            "contract §6: the re-saved file holds what is actually open — the two restored \
             documents plus the newly opened #104 ({issues:?}). (If #101/#102 are missing \
             here, the restore never happened at all — see \
             `board_tabs_are_restored_from_tabs_json_after_a_restart`.)\n--- file ---\n{raw}",
        );
    }

    /// §6 bullet 3, first branch: "If the active document was pruned, a
    /// surviving neighbour in `tabs` (order preserved) becomes active."
    ///
    /// TODO(test-author): §6 says "a surviving neighbour" without pinning
    /// WHICH one (§4 pins a left-neighbour rule for `Ctrl-W`, but that is
    /// #2283's close gesture, not this load-time prune, and the contract does
    /// not extend it here). This test therefore asserts what the contract does
    /// pin — that exactly one tab is active and that it is one of the
    /// survivors — and deliberately does not pin left vs. right. Flagged in
    /// `manifest.yml`.
    #[test]
    fn pruning_the_active_document_activates_a_surviving_neighbour() {
        let sandbox = HomeSandbox::new("prune-active");
        let driver = restart_with(
            &sandbox,
            &tabs_json(Some(scope(&[101, 199, 102], Some(199), None)), None),
        );

        let text = strip_text(
            &driver,
            "a restart whose persisted ACTIVE document is dead must still restore the two \
             live tabs",
        );
        assert!(
            !text.contains("#199"),
            "contract §6: the dead document is pruned even when it is the persisted active \
             one. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            tab_count(&text),
            2,
            "contract §6: pruning the active document does not take its neighbours with it — \
             #101 and #102 both survive. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let active = active_label(&text, "a restored strip whose active document was pruned", &driver.screen());
        assert!(
            active.contains("#101") || active.contains("#102"),
            "contract §6 / issue #2286: \"if the active document was pruned, activate a \
             surviving neighbour\" — exactly one of the surviving tabs (#101 or #102) must be \
             the bracketed active one (§2c). It is {active:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §6 bullet 3, second branch + issue #2286: "if none survive, start with
    /// no tabs" / "if `tabs` is now empty, `active` and `preview` are both
    /// `null`" — the zero-tab state of §2a and
    /// `mocks/board-baseline-no-tabs.screen`.
    ///
    /// RATCHET. Green today (nothing is ever restored, so nothing renders) and
    /// must STAY green once restore lands: an implementation that renders dead
    /// tabs, or that keeps a pruned document as the active one and paints its
    /// label, breaks exactly this test.
    #[test]
    fn a_scope_whose_documents_are_all_pruned_starts_with_no_tabs() {
        let sandbox = HomeSandbox::new("prune-all");
        let driver = restart_with(
            &sandbox,
            &tabs_json(Some(scope(&[198, 199], Some(199), Some(198))), None),
        );

        assert!(
            !driver.screen_contains("×"),
            "contract §6 / issue #2286: every persisted document (#198, #199) is absent from \
             the loaded board, so none survives the load and the scope \"starts with no \
             tabs\" — §2a's zero-tab state renders no strip and therefore no `×` close glyph \
             anywhere.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains("∘"),
            "contract §6 + §1: a pruned preview document leaves no preview tab, so the `∘` \
             preview marker must not render anywhere.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains("#198") && !driver.screen_contains("#199"),
            "contract §6: a document whose issue is absent from the board is \"never \
             rendered\" — neither #198 nor #199 may appear anywhere on the frame.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §6 — a missing / empty / malformed file
    // ═══════════════════════════════════════════════════════════════════════

    /// §6 bullet 4 + issue #2286 AC 4: "A missing file, an empty file, or a
    /// file that fails to parse as the shape above all produce the **same**
    /// result: every scope starts with no tabs — never a panic, never a
    /// partial/best-effort parse."
    ///
    /// The three cases are driven in one test because the contract's claim is
    /// precisely that they are indistinguishable; a per-case test would assert
    /// three things the contract states as one. The "never a panic" half is
    /// asserted by the test completing at all — a panic inside `CoordApp`
    /// construction or the first render fails it.
    ///
    /// CONTROL. Green today (nothing is ever loaded, so nothing can go wrong)
    /// and must STAY green once loading lands: an implementation that
    /// `unwrap()`s the parse, or that best-effort restores the well-formed
    /// prefix of a truncated file, breaks exactly this test.
    #[test]
    fn a_missing_empty_or_malformed_tabs_json_starts_with_no_tabs() {
        // (a) missing — the sandbox's `.coord/` is created empty.
        {
            let _sandbox = HomeSandbox::new("bad-missing");
            let driver = driver_120x40();
            assert!(
                !driver.screen_contains("×") && !driver.screen_contains("∘"),
                "contract §6: with NO `~/.coord/tabs.json` at all, every scope starts with no \
                 tabs — §2a's zero-tab state renders no strip, so neither the `×` close glyph \
                 nor the `∘` preview marker may appear.\n--- screen ---\n{}",
                driver.screen(),
            );
        }

        // (b) empty file.
        {
            let sandbox = HomeSandbox::new("bad-empty");
            let driver = restart_with(&sandbox, "");
            assert!(
                !driver.screen_contains("×") && !driver.screen_contains("∘"),
                "contract §6: an EMPTY `~/.coord/tabs.json` produces the same result as a \
                 missing one — every scope starts with no tabs, and the app does not \
                 panic.\n--- screen ---\n{}",
                driver.screen(),
            );
        }

        // (c) malformed — truncated mid-array, so a best-effort parser could
        // plausibly recover `#101` from it. It must not.
        {
            let sandbox = HomeSandbox::new("bad-malformed");
            let driver = restart_with(
                &sandbox,
                "{\"board\": {\"tabs\": [{\"repo\": \"claude-coordinator\", \"issue\": 101},",
            );
            assert!(
                !driver.screen_contains("×") && !driver.screen_contains("∘"),
                "contract §6 / issue #2286 AC 4: a MALFORMED `~/.coord/tabs.json` starts \
                 clean — \"never a panic, never a partial/best-effort parse\". This file is \
                 truncated mid-array after a well-formed #101 entry; restoring that entry is \
                 exactly the partial parse the contract forbids.\n--- screen ---\n{}",
                driver.screen(),
            );
            // Scoped to the main panel (§0: columns 38–119) rather than the
            // whole frame: the Board SIDEBAR legitimately lists `#101` as an
            // issue row whenever its group is expanded, which has nothing to
            // do with whether a document was restored.
            let main: String = rows(&driver).iter().map(|r| main_slice(r)).collect();
            assert!(
                !main.contains("#101"),
                "contract §6: no document from a malformed file may be restored, not even the \
                 well-formed prefix of one — `#101` (the file's one complete entry) is \
                 rendered in the main panel.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    // TODO(test-author): contract §6's last bullet pins that per-tab sub-state
    // is persisted "only where cheap": the sub-tab selection ("Board" /
    // "Issue" / …, as a string) yes, scroll offsets explicitly optional. But
    // §6's pinned JSON shape carries no field for it — a document key is
    // `{"repo": …, "issue": …}` and nothing more — so there is no pinned place
    // to look for it in the file and no pinned rule for what a restored
    // sub-tab does when the field is absent. Asserting it would mean inventing
    // a field name, so no test below covers sub-tab persistence. Flagged in
    // `manifest.yml`.
}
