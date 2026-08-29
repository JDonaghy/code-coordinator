// Sealed acceptance slice for **issue #2284** — coord-tui: the Pipeline panel
// owns its own independent document tab set, preserved across panel switches —
// milestone ms-65 (tracking issue #2289, "coord-tui: per-panel document tabs
// (preview/pin)").
//
// Authored independently from `tests/acceptance/ms-65/contract.md` (Gate A)
// and its mocks, with **zero** worker/implementation context: no work branch,
// PR or commit for #2284 (or any other ms-65 issue) was read. Every assertion
// below is derived from contract §3 (§3a/§3b) and the mock that section
// indexes (`mocks/pipeline-tabs-independent.screen`), plus its declared
// partner `mocks/board-pinned-3-tabs.screen` (§3b calls the two "the
// independence pair").
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-65 mock declares (contract §0).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2284 may run it
// (`coord acceptance run --issue 2284`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §3 only. §2 (#2282 Board strip: where it renders, the 20/22-column
// label budget, the active bracket, the open/preview/pin semantics) is a
// *precondition* of everything here — every scenario has to open tabs before
// it can prove they are scoped — but it is #2282's slice
// (`board_tabs_2282.rs`) that asserts it, and this file never re-asserts a §2
// clause as its own subject. The one §2-shaped fact restated here is §3a's own
// testable contrast ("on Board it is the second (toolbar row precedes it)"),
// which §3 states because the asymmetry between the two panels is exactly what
// #2284 must get right. §4 (#2283 close/navigate/overflow), §5 (#2285 per-tab
// sub-state), §6 (#2286 persistence — including its per-scope `"board"` /
// `"pipeline"` JSON keys), §8 (#2287 discoverability) and §9 (#2288 split) are
// other issues' slices and are untouched.
//
// ── Harness facts this slice had to design around ─────────────────────────
//
// 1. **A LIVE HARNESS BLOCKER, not a #2284 defect: entering the Pipeline panel
//    wipes the seeded fixture in this (external) test crate.**
//    `sync_active_view_from_shell` → `maybe_kick_pipeline_loader` → `refresh`
//    → `start_data_load` (`tui/src/app/data.rs`). That function short-circuits
//    to `BoardData::default()` — so `apply_pending_data`'s #620 degraded-tick
//    guard preserves the seeded board — but **only under `#[cfg(test)]`**, and
//    `cfg(test)` is FALSE for `tui/tests/acceptance.rs`, which is a separate
//    integration-test crate linking `coord_tui` as a normal dependency. So in
//    this suite the loader reads the developer's real `~/.coord/coord.db`, and
//    the very next dispatched event (`dispatch_handle` drains the receiver
//    before handling anything) wholesale-replaces `self.data` — the seeded
//    #101–#203 board vanishes and the Pipeline tree empties. Measured, not
//    assumed: after one activity-bar click onto Pipeline plus one further
//    click, the Board sidebar renders this machine's real `api (1)` repo.
//    Consequence: every Pipeline-side setup step below currently dies at a
//    *harness precondition*, and each one says so in its panic message. The
//    one-line fix is in the app, not in this suite —
//    `#[cfg(any(test, feature = "test-support"))]` on that short-circuit — and
//    it is flagged for the coordinator in `tests/acceptance/ms-65/manifest.yml`.
//
// 2. **Double-click / click folding.** `tui/Cargo.toml` pins a quadraui rev at
//    which #592 has landed, so `TuiDriver` has real `double_click()`.
//    `set_double_click_folding(false)` is set in the fixture so two `click()`s
//    at one cell are two *single* clicks, never a wall-clock race against the
//    400 ms `DoubleClickDetector` window; `dbl_click_*` sends the
//    `MouseDown` + `DoubleClick` pair a real double click delivers.
//
// 3. **Preview styling.** Per contract §1 the preview tab's plain-text marker
//    is a leading `"∘ "` (U+2218); `screen()` styling is never asserted. Tab
//    *counts* are `×` counts in the strip row, exactly as §2e instructs.
//
// 4. **Column geometry.** Contract §0 pins columns 3–37 as sidebar content and
//    38–119 as main-panel content, every glyph in play being one column wide,
//    so a char index into a `screen()` row IS its cell column. That is how
//    `sidebar_hit` targets a sidebar row without matching the same `#<N>` in
//    the main panel, and how §3a's "first content row of `main_content_bounds`"
//    is measured without hard-coding a row number.
//
// 5. **Fixture ranges.** The Board sidebar lists every open issue; the
//    Pipeline lists only issues carrying a tracked label. So the fixture gives
//    #101–#103 an untracked label and #201–#203 the tracked `coord` one —
//    which is what makes the two panels' issue sets disjoint in the mocks' own
//    100s-vs-200s ranges (§3b: "a side-by-side read of the two mocks is itself
//    the independence proof"). #201–#203 do also appear in the *Board*
//    sidebar (it does not filter by label); nothing below ever clicks them
//    there.

mod pipeline_tabs_2284 {
    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::AppLogic;

    /// Contract §7's fixture, in the two disjoint number ranges §3b's
    /// independence pair is drawn in: #101–#103 for the Board (the three tabs
    /// `mocks/board-pinned-3-tabs.screen` shows) and #201–#203 for the
    /// Pipeline (the set `mocks/pipeline-tabs-independent.screen` draws two
    /// tabs from). Titles are the mocks' own.
    ///
    /// Wire shape is `make_app_with_board_json`'s (the daemon's `/board`
    /// payload) — §7 leaves the outer schema to #2281, which chose this one.
    /// `pipeline_tracked_labels` is pinned explicitly rather than left to its
    /// `["coord"]` default so the split between the two ranges is stated in
    /// the fixture instead of inherited.
    const BOARD_JSON: &str = r#"{
      "issues": [
        {"repo_name": "claude-coordinator", "number": 101, "title": "Fix login race timeout", "state": "open", "labels": ["board-only"]},
        {"repo_name": "claude-coordinator", "number": 102, "title": "Auth token refresh bug", "state": "open", "labels": ["board-only"]},
        {"repo_name": "claude-coordinator", "number": 103, "title": "Race condition in poller", "state": "open", "labels": ["board-only"]},
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

    /// Board sidebar-row click targets. The mocks draw these rows as
    /// `"#102 Auth token refresh bug"`; the shipped 35-column sidebar renders
    /// `"      #102  Auth token refresh b"` — **two** spaces after the number,
    /// title truncated. Taken from a real 120×40 render for that reason.
    const ROW_101: &str = "#101  Fix login race";
    const ROW_102: &str = "#102  Auth token refresh";
    const ROW_103: &str = "#103  Race condition in";

    /// Activity-bar icons, contract §0: `B` = Board (row 0), `▶` = Pipeline
    /// (row 2), both painted in columns 0–1.
    const BOARD_ICON: char = 'B';
    const PIPELINE_ICON: char = '▶';

    /// Contract §0: sidebar content is columns 3–37, main-panel content is
    /// columns 38–119.
    const SIDEBAR_COLS: std::ops::Range<usize> = 3..38;
    const MAIN_START_COL: usize = 38;

    // ═══════════════════════════════════════════════════════════════════════
    // Fixture + grid helpers
    //
    // Everything in this block is a *precondition* harness, not a #2284
    // clause: each panic message says so, so a failure here reads as a
    // fixture/baseline finding (see harness note 1) rather than as a missing
    // per-scope tab set.
    // ═══════════════════════════════════════════════════════════════════════

    /// Board panel with the §7 fixture seeded and its sidebar issue rows
    /// revealed, on the contract's pinned 120×40 grid (§0).
    ///
    /// `SidebarView::Board` is the app's default active view, so no
    /// activity-bar click is needed here. The seeded repo's `No milestone`
    /// group is collapsed by default (#857), so it is expanded.
    fn board_driver() -> TuiDriver<impl AppLogic> {
        let app = make_app_with_board_json(BOARD_JSON);
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        driver.set_double_click_folding(false);
        driver.render();

        if !driver.screen_contains(ROW_101) {
            let before = driver.screen();
            let (x, y) = driver.find("No milestone (6)").unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2284 clause): the Board sidebar must render the \
                     seeded repo's collapsed \"No milestone (6)\" group header for contract \
                     §7's fixture — not found.\n--- screen ---\n{before}"
                )
            });
            driver.click(x, y);
            driver.render();
        }
        assert!(
            driver.screen_contains(ROW_101),
            "ms-65 baseline (NOT a #2284 clause): the Board sidebar must render a row for \
             contract §7's issue #101.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver
    }

    /// Screen rows, 0-indexed, as the grid the mocks are written in.
    fn rows<A: AppLogic>(driver: &TuiDriver<A>) -> Vec<String> {
        driver.screen().lines().map(str::to_string).collect()
    }

    /// A row's main-panel slice (contract §0: columns 38–119).
    fn main_slice(row: &str) -> String {
        row.chars().skip(MAIN_START_COL).collect()
    }

    /// The document tab strip: the first row carrying the §2d close glyph `×`
    /// (U+00D7, `quadraui::tui::tab_bar::TAB_CLOSE_CHAR`) in its **main-panel**
    /// columns.
    ///
    /// Unambiguous: neither shipped panel paints `×` anywhere else — Board's
    /// `[P]urge` toolbar button uses `✕` (U+2715), a different code point —
    /// and neither does any ms-65 mock outside a tab strip.
    ///
    /// `None` when no strip is rendered — the zero-tab state §3a requires of
    /// the Pipeline panel before any Pipeline tab is opened.
    fn strip<A: AppLogic>(driver: &TuiDriver<A>) -> Option<(usize, String)> {
        rows(driver)
            .into_iter()
            .enumerate()
            .find(|(_, r)| main_slice(r).contains('×'))
    }

    /// The strip row's `(index, text)`, or a diagnosis naming the clause that
    /// expected it. `panel` names the panel under test so the message is
    /// legible from a JSON test report alone.
    fn strip_row<A: AppLogic>(driver: &TuiDriver<A>, panel: &str) -> (usize, String) {
        strip(driver).unwrap_or_else(|| {
            panic!(
                "contract §3a/§2d: with ≥1 document tab open the {panel} panel must render a \
                 tab-strip row in which every open tab carries the trailing `×` close glyph — \
                 no row's main-panel columns contain `×`.\n--- screen ---\n{}",
                driver.screen()
            )
        })
    }

    /// The strip row's text.
    fn strip_text<A: AppLogic>(driver: &TuiDriver<A>, panel: &str) -> String {
        strip_row(driver, panel).1
    }

    /// How many tabs the strip shows, counted the way contract §2e mandates:
    /// `×` occurrences in the strip row, never colour or style.
    fn tab_count<A: AppLogic>(driver: &TuiDriver<A>, panel: &str) -> usize {
        strip_text(driver, panel).matches('×').count()
    }

    /// Click point for the first occurrence of `needle` inside the **sidebar**
    /// columns (§0: 3–37) of any row — so a `#<N>` tag rendered in the main
    /// panel (a tab label, a detail-pane header) can never be mistaken for the
    /// sidebar row that opens it.
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

    /// Click point for the first occurrence of `needle` inside the strip row.
    fn strip_hit<A: AppLogic>(driver: &TuiDriver<A>, panel: &str, needle: &str) -> (f32, f32) {
        let (y, text) = strip_row(driver, panel);
        let chars: Vec<char> = text.chars().collect();
        let hay: String = chars.iter().collect();
        let off = hay.find(needle).unwrap_or_else(|| {
            panic!(
                "contract §2b (a PRECONDITION of §3, owned by #2282): the {panel} tab strip \
                 must label each open tab with its `{needle}` issue tag so one tab out of \
                 several can be activated. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        let col = hay[..off].chars().count();
        (col as f32 + 0.5, y as f32 + 0.5)
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
                "ms-65 baseline (NOT a #2284 clause): contract §0 pins the activity bar to \
                 columns 0–1, with `{icon}` as a panel icon — no such icon there.\n\
                 --- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.render();
    }

    /// Enter the Pipeline panel and make its issue rows clickable.
    ///
    /// The Pipeline sidebar groups tracked issues under state → repo →
    /// milestone nodes, the innermost of which starts collapsed, so the
    /// `#201`–`#203` rows need one expand click before they exist. Both steps
    /// are harness preconditions: see harness note 1 for the live blocker that
    /// currently makes them fail.
    fn goto_pipeline<A: AppLogic>(driver: &mut TuiDriver<A>) {
        switch_panel(driver, PIPELINE_ICON);
        if sidebar_hit(driver, "#201").is_none() {
            if let Some((x, y)) = sidebar_hit(driver, "No milestone") {
                driver.click(x, y);
                driver.render();
            }
        }
        assert!(
            sidebar_hit(driver, "#201").is_some(),
            "HARNESS PRECONDITION (NOT a #2284 defect — see this slice's header note 1): the \
             Pipeline sidebar must list contract §7's tracked issues #201–#203. Entering the \
             Pipeline panel kicks `maybe_kick_pipeline_loader` → `refresh` → `start_data_load`, \
             whose fixture short-circuit is `#[cfg(test)]`-only and therefore inert in this \
             external `--test acceptance` crate, so the next dispatched event replaces the \
             seeded board with whatever the local machine's real `~/.coord/coord.db` holds. \
             The fix is `#[cfg(any(test, feature = \"test-support\"))]` in \
             `tui/src/app/data.rs`, not anything in this file.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Double-click a Board sidebar row: opens-or-activates its tab, then
    /// promotes it to pinned (§2e rule 3).
    fn dbl_click_board_row<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let before = driver.screen();
        let (x, y) = driver.find(row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2284 clause): Board sidebar row {row:?} must be on \
                 screen to double-click.\n--- screen ---\n{before}"
            )
        });
        driver.click(x, y);
        driver.double_click(x, y);
        driver.render();
    }

    /// Single-click a Board sidebar row: opens-or-replaces the preview tab
    /// (§2e rule 1).
    fn click_board_row<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let before = driver.screen();
        let (x, y) = driver.find(row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2284 clause): Board sidebar row {row:?} must be on \
                 screen to click.\n--- screen ---\n{before}"
            )
        });
        driver.click(x, y);
        driver.render();
    }

    /// Double-click the Pipeline sidebar row carrying `tag` (e.g. `"#201"`).
    fn dbl_click_pipeline_row<A: AppLogic>(driver: &mut TuiDriver<A>, tag: &str) {
        let (x, y) = sidebar_hit(driver, tag).unwrap_or_else(|| {
            panic!(
                "HARNESS PRECONDITION (NOT a #2284 defect — header note 1): Pipeline sidebar \
                 row {tag:?} must be on screen to double-click.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.double_click(x, y);
        driver.render();
    }

    /// Single-click the Pipeline sidebar row carrying `tag`.
    fn click_pipeline_row<A: AppLogic>(driver: &mut TuiDriver<A>, tag: &str) {
        let (x, y) = sidebar_hit(driver, tag).unwrap_or_else(|| {
            panic!(
                "HARNESS PRECONDITION (NOT a #2284 defect — header note 1): Pipeline sidebar \
                 row {tag:?} must be on screen to click.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.render();
    }

    /// The Board's headline state: `#101`, `#102`, `#103` open as three pinned
    /// tabs with `#103` active — `mocks/board-pinned-3-tabs.screen`, the left
    /// half of §3b's independence pair. Three separate double clicks, each
    /// landing while no preview tab is open, is the sequence §2e traces to it.
    fn board_with_three_pinned_tabs() -> TuiDriver<impl AppLogic> {
        let mut driver = board_driver();
        for row in [ROW_101, ROW_102, ROW_103] {
            dbl_click_board_row(&mut driver, row);
        }
        assert_eq!(
            tab_count(&driver, "Board"),
            3,
            "contract §2e (a PRECONDITION of §3, owned by #2282): three double clicks on \
             #101/#102/#103 must leave three pinned Board tabs open — \
             `mocks/board-pinned-3-tabs.screen`. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver, "Board"),
            driver.screen(),
        );
        driver
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §3a — where the Pipeline strip renders (and where it does not)
    // ═══════════════════════════════════════════════════════════════════════

    /// §3a, zero-tab case: "Pipeline shows its baseline (no doc-tab strip row,
    /// matching §3a for the zero-tab case)".
    ///
    /// CONTROL. This is green before #2284 exists (no panel paints a strip
    /// with nothing open) and must STAY green after it lands: an
    /// implementation that reserves the strip row unconditionally, or that
    /// renders the Board's tabs on Pipeline, breaks exactly this test and
    /// nothing else.
    #[test]
    fn pipeline_with_zero_doc_tabs_renders_no_tab_strip() {
        let mut driver = board_driver();
        switch_panel(&mut driver, PIPELINE_ICON);

        assert!(
            strip(&driver).is_none(),
            "contract §3a/§2a: with zero document tabs open in the Pipeline scope, the \
             Pipeline panel must render no tab strip at all — no `×` (U+00D7) close glyph \
             anywhere in its main-panel columns.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains("∘"),
            "contract §3a/§1: with zero document tabs open there is no preview tab in the \
             Pipeline scope, so the `∘` (U+2218) preview marker must not render \
             anywhere.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §3a: "Pipeline has **no** toolbar row. So on Pipeline, the doc-tab strip
    /// is the first row of the main panel, immediately followed by the existing
    /// `Overview / Issue / Log / Summary / Terminal` sub-tab bar" — and its
    /// stated contrast, "on Board it is the second (toolbar row precedes it)".
    /// `mocks/pipeline-tabs-independent.screen` draws exactly that: strip on
    /// the main panel's first row, sub-tab bar directly beneath.
    #[test]
    fn pipeline_doc_tab_strip_is_the_first_main_panel_row_with_no_toolbar_above_it() {
        let mut driver = board_with_three_pinned_tabs();

        // Board half of §3a's contrast: the toolbar occupies the main panel's
        // first content row, so the strip cannot be it.
        let board_rows = rows(&driver);
        let board_first_content = board_rows
            .iter()
            .position(|r| !main_slice(r).trim().is_empty())
            .expect("the Board main panel must paint something");
        let (board_strip, _) = strip_row(&driver, "Board");
        assert!(
            board_strip > board_first_content,
            "contract §3a: on the Board the doc-tab strip is the SECOND main-panel content \
             row — `panel_toolbar()` precedes it; got first content row {board_first_content}, \
             strip row {board_strip}.\n--- screen ---\n{}",
            driver.screen(),
        );

        goto_pipeline(&mut driver);
        dbl_click_pipeline_row(&mut driver, "#201");

        let pipe_rows = rows(&driver);
        let first_content = pipe_rows
            .iter()
            .position(|r| !main_slice(r).trim().is_empty())
            .expect("the Pipeline main panel must paint something");
        let (pipe_strip, strip_text) = strip_row(&driver, "Pipeline");
        assert_eq!(
            pipe_strip,
            first_content,
            "contract §3a: `panel_toolbar()` returns None on Pipeline, so with ≥1 doc tab \
             open the doc-tab strip must be the VERY FIRST content row of the main panel — \
             no toolbar row, and no blank row, above it. First content row was \
             {first_content}, strip row {pipe_strip}. Strip row text: \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let subtab = pipe_rows
            .iter()
            .position(|r| {
                let m = main_slice(r);
                m.contains("Overview") && m.contains("Log") && m.contains("Summary")
            })
            .unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2284 clause): the Pipeline panel's \
                     `Overview / Issue / Log / Summary / Terminal` sub-tab bar always \
                     renders.\n--- screen ---\n{}",
                    driver.screen()
                )
            });
        assert_eq!(
            subtab,
            pipe_strip + 1,
            "contract §3a: the Pipeline doc-tab strip is `immediately followed by` the \
             `Overview / Issue / Log / Summary / Terminal` sub-tab bar — \
             `mocks/pipeline-tabs-independent.screen` draws the strip on row 0 and that bar \
             on row 1. Strip row {pipe_strip}, sub-tab row {subtab}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §3b — independence: one tab set per PanelScope
    // ═══════════════════════════════════════════════════════════════════════

    /// §3b, first testable bullet: "Board panel active, 3 Board tabs open →
    /// switch to Pipeline (no Pipeline tabs yet) → Pipeline shows its baseline
    /// (no doc-tab strip row) — Board's 3 tabs are not visible and not lost."
    ///
    /// Both halves matter: a single global tab set would show `#101 #102 #103`
    /// on Pipeline (visible when it must not be), and a per-panel set that is
    /// rebuilt rather than preserved would lose them (gone when they must
    /// come back).
    #[test]
    fn switching_to_pipeline_hides_the_boards_tabs_without_losing_them() {
        let mut driver = board_with_three_pinned_tabs();
        switch_panel(&mut driver, PIPELINE_ICON);

        assert!(
            strip(&driver).is_none(),
            "contract §3b: switching to Pipeline while it has no tabs of its own must show \
             Pipeline's own (empty) tab set — its baseline, with no doc-tab strip row — not \
             the Board's three tabs.\n--- screen ---\n{}",
            driver.screen(),
        );
        for tag in ["#101", "#102", "#103"] {
            assert!(
                !driver.screen_contains(tag),
                "contract §3b: the Board's tab set belongs to the Board scope and must not \
                 render on the Pipeline panel — {tag} is on screen.\n--- screen ---\n{}",
                driver.screen(),
            );
        }

        switch_panel(&mut driver, BOARD_ICON);
        assert_eq!(
            tab_count(&driver, "Board"),
            3,
            "contract §3b: switching panels `never merges, reorders or drops the outgoing` \
             scope's set — the Board's three tabs must be back, intact, on return. Strip row \
             was {:?}.\n--- screen ---\n{}",
            strip_text(&driver, "Board"),
            driver.screen(),
        );
    }

    /// #2284's first acceptance criterion, and the Pipeline half of §3b's
    /// independence pair: "Opening two Pipeline issues as tabs, with three
    /// already open on the Board, shows exactly two tabs on Pipeline" —
    /// `mocks/pipeline-tabs-independent.screen` (`#201`, `#202`).
    #[test]
    fn pipeline_opens_its_own_two_tabs_while_the_board_keeps_three() {
        let mut driver = board_with_three_pinned_tabs();
        goto_pipeline(&mut driver);
        dbl_click_pipeline_row(&mut driver, "#201");
        dbl_click_pipeline_row(&mut driver, "#202");

        let strip_text = strip_text(&driver, "Pipeline");
        assert_eq!(
            strip_text.matches('×').count(),
            2,
            "contract §3b + #2284 AC 1: opening two Pipeline issues as tabs, with three \
             already open on the Board, shows EXACTLY two tabs on Pipeline — the Board's \
             three do not merge into this scope's strip. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for tag in ["#201", "#202"] {
            assert!(
                strip_text.contains(tag),
                "contract §3b: the Pipeline strip holds the two Pipeline documents just \
                 opened — {tag} is missing. Strip row was \
                 {strip_text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
        for tag in ["#101", "#102", "#103"] {
            assert!(
                !strip_text.contains(tag),
                "contract §3b: Board and Pipeline tab sets `never merge` — the Board \
                 document {tag} must not appear in the Pipeline strip. Strip row was \
                 {strip_text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    /// §3b, second testable bullet — the milestone's headline round trip:
    /// "Open 2 Pipeline tabs → switch back to Board → strip shows exactly
    /// `#101 #102 #103` again, same order, same active tab".
    ///
    // TODO(test-author): §3b also says "same scroll position as before the
    // switch". With three tabs on an 82-column strip nothing is scrolled, and
    // the contract pins no observable rendering for a *restored* strip scroll
    // offset beyond §4's `‹`/`›` affordances (which are #2283's slice). Not
    // asserted here rather than invented.
    #[test]
    fn board_pipeline_board_round_trip_restores_the_boards_tabs_order_and_active_tab() {
        let mut driver = board_with_three_pinned_tabs();
        let before = strip_text(&driver, "Board");

        goto_pipeline(&mut driver);
        dbl_click_pipeline_row(&mut driver, "#201");
        dbl_click_pipeline_row(&mut driver, "#202");
        switch_panel(&mut driver, BOARD_ICON);

        let after = strip_text(&driver, "Board");
        assert_eq!(
            after.matches('×').count(),
            3,
            "contract §3b (the milestone's headline requirement): after Board → Pipeline → \
             Board, the Board strip shows exactly its own three tabs again. Was {before:?}, \
             now {after:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let order: Vec<&str> = ["#101", "#102", "#103"]
            .into_iter()
            .filter(|tag| after.contains(tag))
            .collect();
        assert_eq!(
            order,
            vec!["#101", "#102", "#103"],
            "contract §3b: the restored Board set keeps the SAME ORDER — switching panels \
             `never merges, reorders or drops` a scope's set. Strip row was \
             {after:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            after.contains("[#103"),
            "contract §3b + §2c: the restored Board set keeps the SAME ACTIVE TAB — #103 was \
             active before the switch, so it is the bracketed tab after it. Strip row was \
             {after:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for tag in ["#201", "#202"] {
            assert!(
                !after.contains(tag),
                "contract §3b: the Pipeline documents stay in the Pipeline scope — {tag} must \
                 not have followed the panel switch into the Board's strip. Strip row was \
                 {after:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    /// #2284's third acceptance criterion: "Each scope has its own preview
    /// slot: a single click in Pipeline does not disturb the Board's preview
    /// tab." Contract §3b pins the same fact ("preview slots … never merge").
    ///
    /// The Board is left in the two-tab state §2e calls out explicitly —
    /// `#101` pinned plus `#102` as the one preview (`∘ ` marker, §1) — so a
    /// shared preview slot is visible two ways after the Pipeline click:
    /// either the Board's preview is replaced by the Pipeline document, or the
    /// marker moves off `#102`.
    #[test]
    fn each_scope_owns_its_own_preview_slot() {
        let mut driver = board_driver();
        dbl_click_board_row(&mut driver, ROW_101); // #101 → pinned
        click_board_row(&mut driver, ROW_102); // #102 → the Board's preview
        let board_before = strip_text(&driver, "Board");
        assert!(
            board_before.contains("∘ #102") && board_before.matches('×').count() == 2,
            "contract §2e (a PRECONDITION of §3b, owned by #2282): a pinned #101 plus a \
             single-clicked #102 is a two-tab Board strip whose preview is #102. Strip row \
             was {board_before:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        goto_pipeline(&mut driver);
        click_pipeline_row(&mut driver, "#201");

        let pipe = strip_text(&driver, "Pipeline");
        assert_eq!(
            pipe.matches('∘').count(),
            1,
            "contract §3b + #2284 AC 3: the Pipeline scope has its OWN preview slot, so a \
             single click there opens one preview tab of its own. Strip row was \
             {pipe:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            pipe.contains("∘ #201") && pipe.matches('×').count() == 1,
            "contract §3b + §2e rule 1: the Pipeline scope had no tabs, so the single click \
             appends #201 as its one preview tab. Strip row was \
             {pipe:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        switch_panel(&mut driver, BOARD_ICON);
        let board_after = strip_text(&driver, "Board");
        assert_eq!(
            board_after.matches('×').count(),
            2,
            "contract §3b + #2284 AC 3: a single click in Pipeline must not disturb the \
             Board's tab set — it still holds pinned #101 and preview #102. Was \
             {board_before:?}, now {board_after:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            board_after.contains("∘ #102"),
            "contract §3b + #2284 AC 3: each scope has its own preview slot, so the Board's \
             preview is STILL #102 after a single click in Pipeline — it was neither replaced \
             by the Pipeline document nor promoted/demoted. Strip row was \
             {board_after:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !board_after.contains("#201"),
            "contract §3b: the Pipeline preview document must not appear in the Board's \
             strip. Strip row was {board_after:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// #2284's fourth acceptance criterion: "Activating a Pipeline tab reveals
    /// its row in the Pipeline sidebar, not the Board's" — i.e. §2f's
    /// reveal-on-activate "applies per scope, against that panel's own
    /// sidebar". `▸` (U+25B8) is contract §2f's pinned sidebar-selection
    /// marker.
    ///
    /// Both halves are asserted: the Pipeline sidebar follows the activated
    /// Pipeline tab, and the Board's own sidebar selection (its active tab's
    /// row, #103) is untouched by that activation.
    ///
    // TODO(test-author): §2f's other half — that reveal also SCROLLS the row
    // into view — is not asserted for the Pipeline scope. The contract states
    // the scroll requirement only for the Board (§2f, with the 5-issue fixture
    // and a short viewport), and pins no Pipeline-sidebar geometry to measure
    // it against; §3b's own testable bullets are set-, order- and
    // selection-shaped. Selection is asserted here; scrolling is left to
    // #2282's slice, which owns §2f.
    #[test]
    fn activating_a_pipeline_tab_reveals_its_row_in_the_pipeline_sidebar_not_the_boards() {
        let mut driver = board_with_three_pinned_tabs();
        assert!(
            driver.screen_contains("▸ #103"),
            "contract §2f (a PRECONDITION of #2284 AC 4, owned by #2282): activating the \
             Board tab for #103 selects its Board sidebar row, marked `▸` \
             (U+25B8).\n--- screen ---\n{}",
            driver.screen(),
        );

        goto_pipeline(&mut driver);
        dbl_click_pipeline_row(&mut driver, "#201");
        dbl_click_pipeline_row(&mut driver, "#202");

        // Activate the OTHER Pipeline tab from the strip — the activation path
        // §2f names first ("click"), driven inside the Pipeline scope.
        let (x, y) = strip_hit(&driver, "Pipeline", "#201");
        driver.click(x, y);
        driver.render();

        assert!(
            driver.screen_contains("▸ #201"),
            "contract §3b + #2284 AC 4: reveal-on-activate applies PER SCOPE — activating the \
             Pipeline tab for #201 must select #201's row in the PIPELINE sidebar, marked `▸` \
             (U+25B8), the way `mocks/pipeline-tabs-independent.screen` marks its own active \
             document's row.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            sidebar_hit(&driver, "▸ #201").is_some(),
            "contract §3b + #2284 AC 4: …and that selection marker belongs to the Pipeline \
             SIDEBAR (columns 3–37, §0), not to anything the main panel paints.\n\
             --- screen ---\n{}",
            driver.screen(),
        );

        switch_panel(&mut driver, BOARD_ICON);
        assert!(
            driver.screen_contains("▸ #103"),
            "contract §3b + #2284 AC 4: activating a tab in the Pipeline scope reveals its \
             row in the Pipeline sidebar, `not the Board's` — the Board's own selection is \
             still its active tab's row, #103.\n--- screen ---\n{}",
            driver.screen(),
        );
    }
}
