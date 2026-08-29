// Sealed acceptance slice for **issue #2282** — Board panel document tab strip
// (preview / pin) — milestone ms-65 (tracking issue #2289, "coord-tui:
// per-panel document tabs (preview/pin)").
//
// Authored independently from `tests/acceptance/ms-65/contract.md` (Gate A)
// and its mocks, with **zero** worker/implementation context: no work branch,
// PR or commit for #2282 was read. Every assertion below is derived from
// contract §2 (§2a–§2f) and the three mocks that section indexes
// (`mocks/board-baseline-no-tabs.screen`, `mocks/board-preview-tab.screen`,
// `mocks/board-pinned-3-tabs.screen`) alone.
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-65 mock declares
// (`driver_with_shell(app, CoordApp::shell_config(), 120, 40)`, contract §0).
// The one exception is `activating_a_tab_scrolls_its_sidebar_row_into_view`,
// which §2f's own testable clause explicitly requires to run on a shorter
// viewport ("the viewport short enough that not all 5 fit").
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2282 may run it
// (`coord acceptance run --issue 2282`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §2 only. §3 (#2284 Pipeline's own set), §4 (#2283 close/navigate/
// overflow), §5 (#2285 per-tab sub-state), §6 (#2286 persistence), §8 (#2287
// discoverability) and §9 (#2288 split) are other issues' slices and are
// deliberately untouched here — including §8a's `"click=preview …"` status-bar
// hint, which `mocks/board-preview-tab.screen` shows on the same frame as this
// slice's subject but which the contract's own mock index attributes to #2287.
//
// ── Harness facts this slice had to design around ─────────────────────────
//
// 1. **Double-click.** `TuiDriver` at the pinned quadraui rev
//    (`d70da7dff7ef9f1eb578ee48aca69297e548348e`) has no `double_click()`
//    helper — that is milestone pre-requisite quadraui#592, which has NOT
//    landed. Sending two `click()`s would race the runner's 400 ms
//    `DOUBLE_CLICK_MS` wall clock, the documented flake source #592 exists to
//    remove. So `dbl_click()` below models a double click the way the real TUI
//    runner actually delivers one: `quadraui::tui::events::DoubleClickDetector`
//    *replaces* the second `MouseDown` with `UiEvent::DoubleClick`, so a
//    physical double-click reaches `AppLogic` as `MouseDown` **then**
//    `DoubleClick` at the same position. That pair is dispatched directly,
//    deterministically, with no clock involved. When #592 lands, `dbl_click()`
//    becomes a one-line delegation and nothing else in this file changes.
//
// 2. **Preview styling.** `screen()` is symbols-only (quadraui#593, also not
//    landed), so `Modifier::ITALIC` is unassertable. Contract §1 pins the
//    plain-text stand-in: a preview tab's label carries a leading `"∘ "`
//    (U+2218). Every preview/pin assertion below keys off that marker and off
//    tab *counts*, exactly as §2e instructs — never off colour or style.
//
// 3. **Sidebar row text.** The mocks draw sidebar rows as
//    `"#102 Auth token refresh bug"`. The shipped Board sidebar renders
//    `"      #102  Auth token refresh b"` — **two** spaces after the number,
//    and the title truncated to the 35-column sidebar. The `ROW_*` constants
//    below are therefore taken from a real 120×40 render, not from the mocks.
//    The double space also makes them unambiguous once tabs exist: a tab label
//    is `"#<N> <title>"` with a *single* space (§2b), and the detail pane
//    renders the number and the title on separate lines, so `find(ROW_102)`
//    can only ever hit the sidebar row it is meant to click.
//
// ── Two mock-vs-shipped-app discrepancies, resolved in favour of reality ───
// A test-author may not amend a contract, so where §2 restates a *shipped*
// baseline fact that the running app contradicts, this slice asserts the
// weaker claim both agree on rather than pinning a string that would be red
// against a correct #2282 implementation. Both are flagged for the coordinator
// in `tests/acceptance/ms-65/manifest.yml`.
//
//   (a) §2a says `driver.screen_contains("[Board]")` is true — the mocks draw
//       the sub-tab bar as `[Board]   Issue    Chat    Terminal`. The shipped
//       bar renders `Board  Issue  Board Chat  Terminal`: quadraui's
//       `draw_tab_bar` marks the active tab with colour, not brackets, and
//       coord-tui's labels are `" Board "` / `" Issue "` / `" Board Chat "` /
//       `" Terminal "` (`app/render.rs`). §2a itself calls the sub-tab bar
//       "unchanged by this milestone", so the bracket is a mock artifact.
//       `subtab_row_index()` locates the bar by its labels instead.
//       (§2c's `[<label> ×]` bracket on the *doc*-tab strip is different: the
//       contract calls it "a new, milestone-local convention", i.e. genuinely
//       #2282's job, so it IS asserted literally.)
//
//   (b) §2a says that with zero tabs "the row immediately below the toolbar
//       row **is** the `Board / Issue / Chat / Terminal` row". It is not, in
//       either the shipped app (row 1 is the `[ ↻ Sync (S) ]` button row) or
//       in §2a's own reference mock (row 1 is blank). What the prose and both
//       mocks do agree on is the ordering — toolbar above, strip in the
//       middle, sub-tab bar below — and that is what is asserted here.

mod board_tabs_2282 {
    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::{NamedKey, Point, UiEvent};

    /// The fixture issue set contract §7 pins for every ms-65 mock: five
    /// `claude-coordinator` issues, #101–#105, with the exact titles the
    /// mocks' truncated tab labels were rendered from.
    ///
    /// Wire shape is `make_app_with_board_json`'s (`BoardPayload`, i.e. the
    /// daemon's `/board` payload) — §7 leaves the outer schema to #2281, which
    /// chose this one. The `coord` label is what puts these issues in the
    /// tracked set; `board_meta` mirrors #2281's own seam test.
    const BOARD_JSON: &str = r#"{
      "issues": [
        {"repo_name": "claude-coordinator", "number": 101, "title": "Fix login race timeout", "state": "open", "labels": ["coord"]},
        {"repo_name": "claude-coordinator", "number": 102, "title": "Auth token refresh bug", "state": "open", "labels": ["coord"]},
        {"repo_name": "claude-coordinator", "number": 103, "title": "Race condition in poller", "state": "open", "labels": ["coord"]},
        {"repo_name": "claude-coordinator", "number": 104, "title": "Flaky CI on macOS runners", "state": "open", "labels": ["coord"]},
        {"repo_name": "claude-coordinator", "number": 105, "title": "Memory leak in watch loop", "state": "open", "labels": ["coord"]}
      ],
      "board_meta": {
        "pipeline_repos": "{\"claude-coordinator\": \"JDonaghy/claude-coordinator\"}",
        "pipeline_default_gates": "[\"review\", \"merge\"]"
      }
    }"#;

    /// Sidebar-row click targets — see harness note 3 for why these are not
    /// the mocks' `"#102 Auth token refresh bug"` form.
    const ROW_101: &str = "#101  Fix login race";
    const ROW_102: &str = "#102  Auth token refresh";
    const ROW_103: &str = "#103  Race condition in";
    const ROW_105: &str = "#105  Memory leak in watch";

    // ═══════════════════════════════════════════════════════════════════════
    // Fixture + screen helpers
    // ═══════════════════════════════════════════════════════════════════════

    /// Board panel with the §7 fixture seeded and its sidebar issue rows
    /// revealed, on a `width`×`height` grid.
    ///
    /// `SidebarView::Board` is the app's default active view, so no
    /// activity-bar click is needed. The seeded repo's `No milestone` group
    /// is collapsed by default (#857), so it is expanded here — baseline
    /// behaviour this milestone does not change, which is why a failure
    /// inside this helper means the ms-65 *baseline* regressed, not that
    /// #2282 is missing.
    fn board_driver_sized(width: u16, height: u16) -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_app_with_board_json(BOARD_JSON);
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), width, height);
        driver.render();

        if !driver.screen_contains(ROW_101) {
            let before = driver.screen();
            let (x, y) = driver.find("No milestone (5)").unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2282 clause): the Board sidebar must render \
                     the seeded repo's collapsed \"No milestone (5)\" group header for \
                     contract §7's five-issue fixture — not found.\n--- screen ---\n{before}"
                )
            });
            driver.click(x, y);
            driver.render();
        }
        assert!(
            driver.screen_contains(ROW_101),
            "ms-65 baseline (NOT a #2282 clause): the Board sidebar must render a row \
             for contract §7's issue #101.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver
    }

    /// The contract's pinned 120×40 grid (§0).
    fn board_driver() -> TuiDriver<impl quadraui::AppLogic> {
        board_driver_sized(120, 40)
    }

    /// Screen rows, 0-indexed, as the grid the mocks are written in.
    fn rows<A: quadraui::AppLogic>(driver: &TuiDriver<A>) -> Vec<String> {
        driver.screen().lines().map(str::to_string).collect()
    }

    /// Index of the row carrying the Board panel toolbar (`panel_toolbar()`,
    /// contract §2a — explicitly unchanged by this milestone).
    fn toolbar_row_index<A: quadraui::AppLogic>(driver: &TuiDriver<A>) -> usize {
        rows(driver)
            .iter()
            .position(|r| r.contains("[A]dd"))
            .unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2282 clause): contract §2a pins the Board panel \
                     toolbar row (`[ [A]dd ] …`) as unchanged by this milestone — not \
                     found.\n--- screen ---\n{}",
                    driver.screen()
                )
            })
    }

    /// Index of the `Board / Issue / Board Chat / Terminal` sub-tab bar row
    /// (contract §2a — pre-dates this milestone, always renders). Located by
    /// its labels rather than by `"[Board]"`; see discrepancy (a) in the file
    /// header.
    fn subtab_row_index<A: quadraui::AppLogic>(driver: &TuiDriver<A>) -> usize {
        rows(driver)
            .iter()
            .position(|r| r.contains(" Board ") && r.contains(" Issue ") && r.contains(" Terminal"))
            .unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2282 clause): contract §2a pins the \
                     `Board / Issue / Chat / Terminal` sub-tab bar as always rendered on \
                     the Board panel — no row carries all of \" Board \", \" Issue \" and \
                     \" Terminal\".\n--- screen ---\n{}",
                    driver.screen()
                )
            })
    }

    /// The document tab strip: the first row carrying the §2d close glyph
    /// `×` (U+00D7 MULTIPLICATION SIGN, `quadraui::tui::tab_bar::TAB_CLOSE_CHAR`).
    ///
    /// Unambiguous: the shipped Board screen contains no `×` anywhere — its
    /// `[P]urge` toolbar button uses `✕` (U+2715 HEAVY MULTIPLICATION X), a
    /// different code point — and neither does
    /// `mocks/board-baseline-no-tabs.screen`.
    ///
    /// `None` when no strip is rendered (the zero-tab state, §2a).
    fn strip<A: quadraui::AppLogic>(driver: &TuiDriver<A>) -> Option<(usize, String)> {
        rows(driver)
            .into_iter()
            .enumerate()
            .find(|(_, r)| r.contains('×'))
    }

    /// The strip row's text, or a diagnosis naming the clause that expected it.
    fn strip_text<A: quadraui::AppLogic>(driver: &TuiDriver<A>) -> String {
        strip(driver).map(|(_, text)| text).unwrap_or_else(|| {
            panic!(
                "contract §2a/§2d: with ≥1 document tab open the Board panel must render \
                 a tab-strip row, and every open tab must carry the trailing `×` close \
                 glyph (§2d, `quadraui::tui::tab_bar::TAB_CLOSE_CHAR`, U+00D7) — no row \
                 on screen contains `×`.\n--- screen ---\n{}",
                driver.screen()
            )
        })
    }

    /// How many tabs the strip shows, counted the way contract §2e mandates:
    /// `×` occurrences in the strip row, never colour or style.
    fn tab_count<A: quadraui::AppLogic>(driver: &TuiDriver<A>) -> usize {
        strip_text(driver).matches('×').count()
    }

    /// Single-click the sidebar row whose text starts with `row`.
    fn click_row<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let before = driver.screen();
        let (x, y) = driver.find(row).unwrap_or_else(|| {
            panic!("sidebar row {row:?} must be on screen to click\n--- screen ---\n{before}")
        });
        driver.click(x, y);
        driver.render();
    }

    /// Double-click the sidebar row whose text starts with `row`.
    ///
    /// Faithful to what the real runner delivers: the first press arrives as
    /// `MouseDown`, and `DoubleClickDetector::process` *replaces* the second
    /// `MouseDown` with `UiEvent::DoubleClick` at the same position. See
    /// harness note 1 in this file's header for why this is not two `click()`s.
    fn dbl_click<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let before = driver.screen();
        let (x, y) = driver.find(row).unwrap_or_else(|| {
            panic!("sidebar row {row:?} must be on screen to double-click\n--- screen ---\n{before}")
        });
        driver.click(x, y);
        driver.dispatch(UiEvent::DoubleClick {
            widget: None,
            position: Point::new(x, y),
        });
        driver.render();
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §2a — where the strip renders (and where it does not)
    // ═══════════════════════════════════════════════════════════════════════

    /// §2a: "When zero tabs are open, the strip renders nothing and reserves
    /// no row" — `mocks/board-baseline-no-tabs.screen`, which carries neither
    /// the §2d close glyph nor the §1 preview marker anywhere on the grid.
    #[test]
    fn zero_doc_tabs_render_no_tab_strip() {
        let driver = board_driver();
        assert!(
            !driver.screen_contains("×"),
            "contract §2a: with zero document tabs open the Board panel must render no \
             tab strip at all — `mocks/board-baseline-no-tabs.screen` carries no `×` \
             (U+00D7) close glyph anywhere.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains("∘"),
            "contract §2a/§1: with zero document tabs open there is no preview tab, so \
             the `∘` (U+2218) preview marker must not render \
             anywhere.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2a: with ≥1 doc tab open the strip is "a new row inserted between the
    /// panel toolbar and the existing sub-tab bar", containing at least one
    /// `"#<N>"` substring.
    ///
    /// Asserts the ordering relation the prose and both mocks agree on, not
    /// §2a's literal "immediately below" row-index claim — see discrepancy (b)
    /// in the file header.
    #[test]
    fn doc_tab_strip_renders_between_the_toolbar_and_the_subtab_bar() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_102);

        let toolbar = toolbar_row_index(&driver);
        let subtab = subtab_row_index(&driver);
        let (strip_row, strip_text) = strip(&driver).unwrap_or_else(|| {
            panic!(
                "contract §2a/§2e: single-clicking sidebar row {ROW_102:?} must open a \
                 preview document tab, so a tab-strip row carrying the §2d `×` close \
                 glyph must render.\n--- screen ---\n{}",
                driver.screen()
            )
        });

        assert!(
            toolbar < strip_row && strip_row < subtab,
            "contract §2a: the document tab strip must render between the Board panel \
             toolbar row and the `Board / Issue / Chat / Terminal` sub-tab bar; got \
             toolbar row {toolbar}, strip row {strip_row}, sub-tab row \
             {subtab}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip_text.contains("#102"),
            "contract §2a: with ≥1 doc tab open the strip row must contain at least one \
             `#<N>` substring; strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2a: the `Board / Issue / Chat / Terminal` sub-tab bar renders "on the
    /// Board panel regardless of tab state" — before any tab is opened and
    /// after one is.
    ///
    /// Located by labels rather than by §2a's literal `"[Board]"`; see
    /// discrepancy (a) in the file header.
    #[test]
    fn subtab_bar_renders_regardless_of_doc_tab_state() {
        let mut driver = board_driver();

        let before = subtab_row_index(&driver);
        assert!(
            driver.screen_contains(" Issue "),
            "contract §2a: the sub-tab bar must render its `Issue` sub-tab with zero doc \
             tabs open.\n--- screen ---\n{}",
            driver.screen(),
        );

        click_row(&mut driver, ROW_102);

        // Without this the "after" half would be vacuously green today: with
        // no strip implemented, the second check would just re-observe the
        // zero-tab frame. §2e rule 1 says the click must have opened one.
        let _ = strip_text(&driver);

        let after = subtab_row_index(&driver);
        assert!(
            driver.screen_contains(" Issue "),
            "contract §2a: the sub-tab bar must still render its `Issue` sub-tab once a \
             document tab is open — opening a tab must not replace or hide \
             it.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            after >= before,
            "contract §2a: the doc-tab strip is inserted ABOVE the sub-tab bar, so \
             opening the first tab may push the sub-tab bar down but must never move it \
             up; it went from row {before} to row {after}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §2b — tab label format (20 columns, 22 with the §1 preview marker)
    // ═══════════════════════════════════════════════════════════════════════

    /// §2b + §1: a preview tab's label is `"∘ #<N> <title>"` truncated at 22
    /// total columns with a trailing `…`. Exact string lifted from
    /// `mocks/board-preview-tab.screen`.
    #[test]
    fn preview_tab_label_is_the_issue_number_and_title_truncated_to_22_columns() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_102);

        let strip_text = strip_text(&driver);
        assert!(
            strip_text.contains("∘ #102 Auth token ref… ×"),
            "contract §2b/§1: a preview tab's label is `#<N> <title>` truncated to 20 \
             columns with a trailing `…`, prefixed by the `∘ ` preview marker (22 columns \
             total), followed by the §2d close glyph — `mocks/board-preview-tab.screen` \
             renders exactly `∘ #102 Auth token ref… ×` for issue #102 \"Auth token \
             refresh bug\". Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2b: pinned tab labels are `"#<N> <title>"` truncated to 20 columns,
    /// with no `∘ ` marker. All three exact strings lifted from
    /// `mocks/board-pinned-3-tabs.screen`.
    #[test]
    fn pinned_tab_labels_are_truncated_to_20_columns() {
        let mut driver = board_driver();
        dbl_click(&mut driver, ROW_101);
        dbl_click(&mut driver, ROW_102);
        dbl_click(&mut driver, ROW_103);

        let strip_text = strip_text(&driver);
        for label in [
            "#101 Fix login race… ×",
            "#102 Auth token ref… ×",
            "#103 Race condition… ×",
        ] {
            assert!(
                strip_text.contains(label),
                "contract §2b: a pinned tab's label is `#<N> <title>` truncated to 20 \
                 columns (inclusive of the `#<N> ` prefix) with a trailing `…`, followed \
                 by the §2d close glyph — `mocks/board-pinned-3-tabs.screen` renders \
                 {label:?}. Strip row was {strip_text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §2c — active tab marker
    // ═══════════════════════════════════════════════════════════════════════

    /// §2c: the active tab is wrapped in `[` `]` (`"[<label> ×]"`); inactive
    /// tabs are not. Pinned against `mocks/board-pinned-3-tabs.screen`, whose
    /// `#103` is active and whose `#101`/`#102` are not.
    #[test]
    fn active_tab_is_bracketed_and_inactive_tabs_are_not() {
        let mut driver = board_driver();
        dbl_click(&mut driver, ROW_101);
        dbl_click(&mut driver, ROW_102);
        dbl_click(&mut driver, ROW_103);

        let strip_text = strip_text(&driver);
        assert!(
            strip_text.contains("[#103 Race condition… ×]"),
            "contract §2c: the active tab is wrapped in `[` `]` as `[<label> ×]`; after \
             three double-clicks ending on #103, `mocks/board-pinned-3-tabs.screen` \
             renders `[#103 Race condition… ×]`. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for inactive in ["[#101", "[#102"] {
            assert!(
                !strip_text.contains(inactive),
                "contract §2c: only the active tab is bracketed — an inactive tab renders \
                 as `<label> ×` with no `[`, but the strip row contains {inactive:?}. \
                 Strip row was {strip_text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §2d — close glyph
    // ═══════════════════════════════════════════════════════════════════════

    /// §2d: "Every open, closable tab renders a trailing `× `". Three open
    /// tabs → exactly three `×` in the strip row.
    #[test]
    fn every_open_tab_renders_the_close_glyph() {
        let mut driver = board_driver();
        dbl_click(&mut driver, ROW_101);
        dbl_click(&mut driver, ROW_102);
        dbl_click(&mut driver, ROW_103);

        assert_eq!(
            tab_count(&driver),
            3,
            "contract §2d: every open, closable tab renders a trailing `×` \
             (`quadraui::tui::tab_bar::TAB_CLOSE_CHAR`), so a strip with three open tabs \
             carries exactly three. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §2e — open semantics (single click / second click / double click)
    // ═══════════════════════════════════════════════════════════════════════

    /// §2e rule 1 (append branch): single-clicking a sidebar row that is not
    /// already open, with no preview tab present, appends **one** preview tab
    /// and activates it.
    #[test]
    fn single_click_opens_one_preview_tab() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_102);

        let strip_text = strip_text(&driver);
        assert_eq!(
            strip_text.matches('×').count(),
            1,
            "contract §2e rule 1: a single click on a not-yet-open sidebar row appends \
             exactly ONE preview tab. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            strip_text.matches("#102").count(),
            1,
            "contract §2e rule 1: the one tab opened must be #102, the row that was \
             clicked. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip_text.contains('∘'),
            "contract §2e rule 1 + §1: a tab opened by a SINGLE click is a preview tab, so \
             its label carries the `∘ ` marker. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2e rule 1 (replace branch) + rule 4: with a preview tab already open,
    /// single-clicking a *different*, not-yet-open row replaces the preview in
    /// place. The tab count stays 1 — "at most one preview tab per tab group,
    /// ever".
    #[test]
    fn second_single_click_replaces_the_preview_tab_in_place() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_102);
        assert_eq!(
            tab_count(&driver),
            1,
            "contract §2e rule 1 precondition: the first single click must leave exactly \
             one tab open.\n--- screen ---\n{}",
            driver.screen(),
        );

        click_row(&mut driver, ROW_103);
        let strip_text = strip_text(&driver);
        assert_eq!(
            strip_text.matches('×').count(),
            1,
            "contract §2e rule 1/rule 4: single-clicking a second, not-yet-open row while \
             a preview tab exists REPLACES that preview in place — it does not append. \
             The strip must still hold exactly one tab. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip_text.contains("#103"),
            "contract §2e rule 1: the replacement preview tab is #103, the row just \
             clicked. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !strip_text.contains("#102"),
            "contract §2e rule 1: #102's preview tab was replaced in place, so it must no \
             longer appear in the strip. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2e rule 2: single-clicking a row whose tab is *already open* (pinned,
    /// here) activates that existing tab — no new tab, no replace.
    #[test]
    fn single_click_on_an_already_open_row_activates_it_without_adding_a_tab() {
        let mut driver = board_driver();
        dbl_click(&mut driver, ROW_101); // #101 pinned, active
        click_row(&mut driver, ROW_102); // #102 preview, active
        assert_eq!(
            tab_count(&driver),
            2,
            "contract §2e precondition: a pinned #101 plus a preview #102 is a two-tab \
             strip.\n--- screen ---\n{}",
            driver.screen(),
        );

        click_row(&mut driver, ROW_101);
        let strip_text = strip_text(&driver);
        assert_eq!(
            strip_text.matches('×').count(),
            2,
            "contract §2e rule 2: single-clicking a row that is ALREADY open activates its \
             existing tab — no new tab, and no replace of the open preview. The count must \
             stay 2. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip_text.contains("[#101"),
            "contract §2e rule 2 + §2c: after single-clicking the already-open #101, that \
             tab is the active one, so it is the bracketed tab. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip_text.contains("#102"),
            "contract §2e rule 2: activating #101 must not close or replace #102's preview \
             tab. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2e rule 3: a double click opens-or-activates, **then** promotes the tab
    /// to pinned — it "drops `is_preview`, drops the `∘ ` marker".
    #[test]
    fn double_click_pins_the_tab_and_drops_the_preview_marker() {
        let mut driver = board_driver();
        dbl_click(&mut driver, ROW_102);

        let strip_text = strip_text(&driver);
        assert_eq!(
            strip_text.matches('×').count(),
            1,
            "contract §2e rule 3: double-clicking a not-yet-open row opens exactly one tab \
             (open-or-activate, then promote) — not two. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !strip_text.contains('∘'),
            "contract §2e rule 3 + §1: promoting a tab to pinned drops the `∘ ` preview \
             marker. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip_text.contains("[#102 Auth token ref… ×]"),
            "contract §2e rule 3 + §2b/§2c: the pinned tab keeps its 20-column label and, \
             being active, is bracketed. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2e, the "distinct 2-tab intermediate state" the contract calls out
    /// explicitly: double-click #101 (→ pinned), then single-click #102
    /// (→ preview, since no preview slot exists any more) gives **two** tabs.
    ///
    /// This is the bullet the contract warns must not be confused with the
    /// three-double-click sequence below.
    #[test]
    fn a_pinned_tab_plus_a_single_click_yields_two_tabs_one_of_them_preview() {
        let mut driver = board_driver();
        dbl_click(&mut driver, ROW_101);
        click_row(&mut driver, ROW_102);

        let strip_text = strip_text(&driver);
        assert_eq!(
            strip_text.matches('×').count(),
            2,
            "contract §2e: double-click #101 (pinned) then single-click #102 leaves TWO \
             tabs — rule 1's replace branch does not fire, because the only open tab is \
             pinned and no preview slot exists. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            strip_text.matches('∘').count(),
            1,
            "contract §2e rule 4 + §1: exactly one of the two tabs is the preview (#102) — \
             `at most one preview tab per tab group, ever`, and #101 was promoted out of \
             preview by its double click. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip_text.contains("∘ #102"),
            "contract §2e: the preview tab is #102, the single-clicked row — not #101, \
             which was pinned. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2e, the headline end state: three double clicks, each landing while no
    /// preview tab is open, give three pinned tabs with `#103` active and no
    /// `∘ ` marker anywhere — `mocks/board-pinned-3-tabs.screen` exactly.
    #[test]
    fn three_double_clicks_yield_three_pinned_tabs_with_the_last_active() {
        let mut driver = board_driver();
        dbl_click(&mut driver, ROW_101);
        dbl_click(&mut driver, ROW_102);
        dbl_click(&mut driver, ROW_103);

        let strip_text = strip_text(&driver);
        assert_eq!(
            strip_text.matches('×').count(),
            3,
            "contract §2e: three double-clicks on #101/#102/#103, each landing while no \
             preview tab is open, append three tabs — \
             `mocks/board-pinned-3-tabs.screen`. Strip row was \
             {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !strip_text.contains('∘'),
            "contract §2e: all three tabs are pinned, so NONE carries the §1 `∘ ` preview \
             marker. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip_text.contains("[#103"),
            "contract §2e + §2c: the last double-clicked tab (#103) is active, so it is \
             the bracketed one. Strip row was {strip_text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §2f — reveal-on-activate
    // ═══════════════════════════════════════════════════════════════════════

    /// §2f, half one: "Activating a tab (by any path — click, `Ctrl-Tab`,
    /// promote) selects the matching sidebar row". `▸` (U+25B8) is the
    /// contract's pinned sidebar-selection marker — distinct from `▶` (the
    /// Pipeline activity-bar icon) and from §2c's `[...]` active-tab bracket.
    #[test]
    fn activating_a_tab_selects_its_sidebar_row() {
        let mut driver = board_driver();
        dbl_click(&mut driver, ROW_101);
        dbl_click(&mut driver, ROW_102);
        dbl_click(&mut driver, ROW_103);

        assert!(
            driver.screen_contains("▸ #103"),
            "contract §2f: activating a document tab must SELECT the matching sidebar row, \
             marked with `▸` (U+25B8) — `mocks/board-pinned-3-tabs.screen` renders \
             `▸ #103` for the active #103 tab.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §2f, half two — the half a selection marker alone does not prove:
    /// activation also **scrolls the row into view**.
    ///
    /// §2f's testable clause requires "the viewport short enough that not all 5
    /// fit", so this test deliberately runs on a 120×11 grid rather than the
    /// 120×40 the mocks declare — at 40 rows all five fixture issues fit
    /// trivially and the clause is unobservable. 11 rows was measured against
    /// the shipped app: it renders sidebar rows for #101–#103 only.
    ///
    /// The setup has to reach #105, whose row starts below the fold, without
    /// depending on scrolling the sidebar — which is exactly the capability
    /// under test, and which the shipped app does not have (milestone
    /// pre-requisite quadraui#595, `SidebarSystem::reveal`, is not landed at
    /// the pinned rev: neither wheel `Scroll` events nor arrow-key selection
    /// move the Board list). The filter box is used instead: typing `105`
    /// narrows the tree to that one issue, its tab is opened there, and the
    /// filter is then cleared so the list is back to its unscrolled state
    /// before the tab is re-activated.
    ///
    /// Every step before the final two asserts is a *harness precondition* and
    /// says so in its panic message — a failure there is a fixture/geometry
    /// finding, not a #2282 defect.
    #[test]
    fn activating_a_tab_scrolls_its_sidebar_row_into_view() {
        let mut driver = board_driver_sized(120, 11);

        assert!(
            !driver.screen_contains(ROW_105),
            "harness precondition for contract §2f (NOT a #2282 defect): on the 120×11 \
             grid, #105's sidebar row must start BELOW the fold, or `activating its tab \
             scrolls it into view` proves nothing.\n--- screen ---\n{}",
            driver.screen(),
        );

        // Tab 1: #101, whose row is visible from the start.
        dbl_click(&mut driver, ROW_101);

        // Tab 2: #105, reached via the filter box rather than by scrolling.
        let before = driver.screen();
        let (fx, fy) = driver.find("Filter issues…").unwrap_or_else(|| {
            panic!(
                "harness precondition for contract §2f (NOT a #2282 defect): the Board \
                 sidebar's `⌕ Filter issues…` box must be on screen so #105 can be \
                 reached without scrolling.\n--- screen ---\n{before}"
            )
        });
        driver.click(fx, fy);
        driver.render();
        for c in "105".chars() {
            driver.type_char(c);
        }
        driver.render();
        assert!(
            driver.screen_contains(ROW_105),
            "harness precondition for contract §2f (NOT a #2282 defect): typing `105` into \
             the Board sidebar filter must narrow the tree to issue \
             #105.\n--- screen ---\n{}",
            driver.screen(),
        );
        dbl_click(&mut driver, ROW_105);

        // Clear the filter — clicking the row moved focus to the tree, so the
        // filter box has to be re-focused first.
        let before = driver.screen();
        let (fx, fy) = driver.find("[105").unwrap_or_else(|| {
            panic!(
                "harness precondition for contract §2f (NOT a #2282 defect): the filter box \
                 must still show the typed `105` so it can be re-focused and \
                 cleared.\n--- screen ---\n{before}"
            )
        });
        driver.click(fx + 2.0, fy);
        driver.render();
        for _ in 0..6 {
            driver.press_named(NamedKey::Backspace);
        }
        driver.render();
        assert!(
            driver.screen_contains(ROW_101) && !driver.screen_contains(ROW_105),
            "harness precondition for contract §2f (NOT a #2282 defect): clearing the \
             filter must restore the full five-issue tree, unscrolled — #101's row \
             visible, #105's below the fold.\n--- screen ---\n{}",
            driver.screen(),
        );

        // Both tabs are open and the sidebar is back at the top. Activating
        // #105's tab from the strip is the event under test.
        let before = driver.screen();
        let (tx, ty) = driver.find("#105 Memory leak in…").unwrap_or_else(|| {
            panic!(
                "contract §2b: #105's pinned tab must render in the strip as \
                 `#105 Memory leak in…` (20-column truncation) so it can be \
                 activated.\n--- screen ---\n{before}"
            )
        });
        driver.click(tx, ty);
        driver.render();

        assert!(
            driver.screen_contains("▸ #105"),
            "contract §2f: activating a document tab must select the matching sidebar row, \
             marked `▸` (U+25B8).\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains(ROW_101),
            "contract §2f: activating a tab must SCROLL the matching sidebar row into \
             view, not merely select it. With the list unscrolled and #105's row below \
             the fold, revealing #105 must move the scroll position, which pushes #101 \
             off-screen — `mocks/board-pinned-3-tabs.screen` depicts exactly that shape \
             (`⋮ 1 more above`, #101 gone). This is the live latent bug milestone \
             pre-requisite quadraui#595 (`SidebarSystem::reveal`) exists to fix: \
             `set_selected_path` alone does not scroll into \
             view.\n--- screen ---\n{}",
            driver.screen(),
        );
    }
}
