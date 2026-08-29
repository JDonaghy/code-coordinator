// Sealed acceptance slice for **issue #2285** — coord-tui: per-tab detail
// sub-state (sub-tab, scroll, expanded stage, terminal) — milestone ms-65
// (tracking issue #2289, "coord-tui: per-panel document tabs (preview/pin)").
//
// Authored independently from `tests/acceptance/ms-65/contract.md` (Gate A),
// with **zero** worker/implementation context: no work branch, PR or commit
// for #2285 (or any other ms-65 issue) was read. Every assertion below is
// derived from contract §5 ("Per-tab sub-state (#2285)") and the issue body's
// own acceptance criteria, which §5 restates. §5 declares itself mock-less
// ("No new mock — this issue is invisible to a static screen grid"), so the
// only mock this slice leans on is `mocks/board-baseline-no-tabs.screen`,
// whose `PIPELINE STAGES` heading is the Board detail pane's default
// (`Board` sub-tab) content and therefore this slice's marker for "this tab
// is on its default sub-tab".
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-65 mock declares (contract §0).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2285 may run it
// (`coord acceptance run --issue 2285`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §5 only, on the **Board** panel. §2 (#2282 the Board strip, the
// label budget, the open/preview/pin semantics), §4 (#2283 close/navigate —
// this slice does click a tab's `×`, but only as the *precondition* of §5's
// "closing a tab discards its sub-state" clause, never as its subject), §3
// (#2284 Pipeline's own set), §6 (#2286 persistence), §8 (#2287
// discoverability) and §9 (#2288 split) are other issues' slices and are
// untouched.
//
// ── Harness facts this slice had to design around ─────────────────────────
//
// 1. **The sub-tab bar renders no brackets, so "which sub-tab is active" is
//    asserted from CONTENT, not from a `[Issue]` substring.** Contract §5's
//    testable clauses are written as `driver.screen_contains("[Issue]")` /
//    `[Board]`, and `mocks/board-baseline-no-tabs.screen` draws the row as
//    `[Board]   Issue    Chat    Terminal`. The shipped Board sub-tab bar
//    (a `quadraui::TabBar`, rasterised by `quadraui/src/tui/tab_bar.rs`)
//    marks its active tab with **colour + modifiers only** — the row renders
//    as ` Board  Issue  Board Chat  Terminal ` with no bracket anywhere, and
//    the third tab is labelled `Board Chat`, not `Chat`. Measured on a real
//    120×40 frame at authoring time, not assumed.
//    TODO(test-author): the contract asserts a sub-tab-bar bracket convention
//    that the shipped sub-tab bar does not have (§2c even calls it a
//    convention that "pre-dates this milestone"). Rather than force #2285's
//    worker to restyle a bar #2285 does not own — which is what a literal
//    `screen_contains("[Issue]")` assertion would do — every clause below
//    asserts the *rendered content of the active sub-tab* instead:
//      * `Issue` sub-tab active  ⇔  that tab's issue-body lines are on screen
//      * `Board` sub-tab active  ⇔  `PIPELINE STAGES` is on screen and no
//        issue-body line is
//      * `Terminal` sub-tab active ⇔ the terminal pane's placeholder is on
//        screen
//    This is strictly stronger than the bracket check (it proves the pane
//    switched, not just the bar), and it is what a per-tab sub-state
//    regression actually shows up as. Flagged for the coordinator in
//    `manifest.yml`.
//
// 2. **PTY liveness itself is not observable through `TuiDriver`.**
//    Contract §5's last bullet and Note 6 ask that a background tab's
//    Terminal session keep running. Sessions are spawned and polled by
//    `drive_detail_terminals`, which the app calls from `tick()`; the driver
//    has no tick pump (`quadraui::tui::testing` exposes `render`, `dispatch`
//    and input helpers, no `tick`), so no session is ever spawned in this
//    suite and "is it still alive" has no screen-level answer here — exactly
//    what contract Note 6 warns ("a background PTY session is not something a
//    static screen grid can show staying alive").
//    TODO(test-author): the contract does not specify an observable proxy for
//    session liveness. `terminal_sub_tab_selection_follows_its_own_tab` below
//    therefore asserts the half that *is* black-box observable and that a
//    shared-sub-state implementation gets wrong: the Terminal sub-tab
//    selection belongs to the tab that opened it, is not inflicted on a
//    freshly-opened tab, and is still there on return. The "keeps running"
//    half is asserted by nobody in this suite; it needs either a tick hook on
//    the driver or an in-crate unit test.
//
// 3. **Scroll is driven with the wheel, not `j`/`k`.** Bare `j`/`k` reach the
//    detail pane only once focus has been moved off the sidebar (`Ctrl-W`),
//    which is a second, unrelated surface; `UiEvent::Scroll` over the main
//    panel is the direct path and is what the app's own scroll arm reads.
//    One notch = one line on the Board detail pane, measured.
//
// 4. **Fixture bodies are line-numbered on purpose.** `AAA-`/`BBB-`/`CCC-`
//    line markers make both *which* issue's body is rendered and *where that
//    body is scrolled to* readable off the grid: at scroll 0 line `-01` is
//    visible, at scroll 5 it is not and `-03` is the first body line. No
//    other screen text can be mistaken for either fact.
//
// 5. **The expanded/focused stage is not covered.** The issue body's design
//    section also moves "the focused/expanded-stage selection" onto the
//    document, but contract §5 — the sealed statement of this issue's
//    black-box surface — never mentions it, and stage expansion lives on the
//    **Pipeline** Overview sub-tab, which cannot be driven from this external
//    test crate at all (the fixture-wipe blocker `pipeline_tabs_2284.rs`
//    documents in its header note 1, still unfixed at authoring time).
//    TODO(test-author): contract §5 doesn't specify per-tab expanded-stage
//    behaviour; no test is written for it. Flagged in `manifest.yml`.

mod detail_substate_2285 {
    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::event::{Point, ScrollDelta, UiEvent};
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::AppLogic;

    // ═══════════════════════════════════════════════════════════════════════
    // Fixture
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §7's five Board issues, verbatim titles, with line-numbered
    /// bodies on the three this slice opens (harness note 4). 40 lines is
    /// comfortably more than the ~34 body rows the 120×40 grid shows, so the
    /// pane is genuinely scrollable.
    fn board_json() -> String {
        let body = |tag: &str| {
            (1..=40)
                .map(|i| format!("{tag}-line-{i:02}"))
                .collect::<Vec<_>>()
                .join("\\n")
        };
        format!(
            r#"{{
      "issues": [
        {{"repo_name": "claude-coordinator", "number": 101, "title": "Fix login race timeout", "state": "open", "labels": ["board-only"], "body": "{a}"}},
        {{"repo_name": "claude-coordinator", "number": 102, "title": "Auth token refresh bug", "state": "open", "labels": ["board-only"], "body": "{b}"}},
        {{"repo_name": "claude-coordinator", "number": 103, "title": "Race condition in poller", "state": "open", "labels": ["board-only"], "body": "{c}"}},
        {{"repo_name": "claude-coordinator", "number": 104, "title": "Flaky CI on macOS runners", "state": "open", "labels": ["board-only"]}},
        {{"repo_name": "claude-coordinator", "number": 105, "title": "Memory leak in watch loop", "state": "open", "labels": ["board-only"]}}
      ],
      "board_meta": {{
        "pipeline_repos": "{{\"claude-coordinator\": \"JDonaghy/claude-coordinator\"}}",
        "pipeline_tracked_labels": "[\"coord\"]",
        "pipeline_default_gates": "[\"review\", \"merge\"]"
      }}
    }}"#,
            a = body("AAA"),
            b = body("BBB"),
            c = body("CCC"),
        )
    }

    /// Board sidebar-row click targets — the shipped 35-column sidebar puts
    /// **two** spaces after the number and truncates the title. Taken from a
    /// real 120×40 render.
    const ROW_102: &str = "#102  Auth token refresh";
    const ROW_103: &str = "#103  Race condition in";

    /// Doc-tab-strip labels (§2b's 20-column budget, as #2282 ships them).
    const TAB_102: &str = "#102 Auth";
    const TAB_103: &str = "#103 Race";

    /// Board detail sub-tab labels, as the shipped bar renders them
    /// (harness note 1 — `Board Chat`, not `Chat`).
    const SUBTAB_BOARD: &str = " Board ";
    const SUBTAB_ISSUE: &str = " Issue ";
    const SUBTAB_TERMINAL: &str = " Terminal ";

    /// Contract §0: sidebar content is columns 3–37, main-panel content is
    /// columns 38–119.
    const SIDEBAR_COLS: std::ops::Range<usize> = 3..38;
    const MAIN_START_COL: usize = 38;

    /// The Board (default) sub-tab's own content — the heading
    /// `mocks/board-baseline-no-tabs.screen` draws in the detail pane for the
    /// selected issue. Its presence, together with the absence of any
    /// `<TAG>-line-` body marker, is this slice's "this tab is on the default
    /// `Board` sub-tab" reading (harness note 1).
    const BOARD_SUBTAB_MARKER: &str = "PIPELINE STAGES";

    /// The Terminal sub-tab's own content. No PTY is ever spawned in this
    /// suite (harness note 2), so the pane sits on its pre-spawn placeholder —
    /// which is exactly the "the Terminal sub-tab is the active one" reading
    /// this slice needs.
    const TERMINAL_SUBTAB_MARKER: &str = "Starting shell session…";

    // ═══════════════════════════════════════════════════════════════════════
    // Grid + driving helpers
    //
    // Everything in this block is a *precondition* harness, not a #2285
    // clause: each panic message says so, so a failure here reads as a
    // fixture/baseline finding rather than as shared sub-state.
    // ═══════════════════════════════════════════════════════════════════════

    /// Screen rows, 0-indexed, as the grid the mocks are written in.
    fn rows<A: AppLogic>(driver: &TuiDriver<A>) -> Vec<String> {
        driver.screen().lines().map(str::to_string).collect()
    }

    /// A row's main-panel slice (contract §0: columns 38–119).
    fn main_slice(row: &str) -> String {
        row.chars().skip(MAIN_START_COL).collect()
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

    /// Click point for the first occurrence of `needle` inside the
    /// **main-panel** columns of any row.
    fn main_hit<A: AppLogic>(driver: &TuiDriver<A>, needle: &str) -> Option<(f32, f32)> {
        for (y, row) in rows(driver).into_iter().enumerate() {
            let band = main_slice(&row);
            if let Some(off) = band.find(needle) {
                let col = MAIN_START_COL + band[..off].chars().count();
                return Some((col as f32 + 0.5, y as f32 + 0.5));
            }
        }
        None
    }

    /// The document tab strip: the first row carrying the §2d close glyph `×`
    /// (U+00D7) in its main-panel columns. `None` when no tab is open (§2a:
    /// the strip then "renders nothing and reserves no row").
    ///
    /// Unambiguous: Board's `[P]urge` toolbar button uses `✕` (U+2715), a
    /// different code point.
    fn strip<A: AppLogic>(driver: &TuiDriver<A>) -> Option<(usize, String)> {
        rows(driver)
            .into_iter()
            .enumerate()
            .find(|(_, r)| main_slice(r).contains('×'))
            .map(|(y, r)| (y, main_slice(&r)))
    }

    /// Board panel with the §7 fixture seeded and its sidebar issue rows
    /// revealed, on the contract's pinned 120×40 grid (§0).
    fn board_driver() -> TuiDriver<impl AppLogic> {
        let app = make_app_with_board_json(&board_json());
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        driver.set_double_click_folding(false);
        driver.render();

        if sidebar_hit(&driver, ROW_102).is_none() {
            let before = driver.screen();
            let (x, y) = sidebar_hit(&driver, "No milestone").unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2285 clause): the Board sidebar must render the \
                     seeded repo's collapsed \"No milestone\" group header for contract §7's \
                     fixture — not found.\n--- screen ---\n{before}"
                )
            });
            driver.click(x, y);
            driver.render();
        }
        assert!(
            sidebar_hit(&driver, ROW_102).is_some(),
            "ms-65 baseline (NOT a #2285 clause): the Board sidebar must render a row for \
             contract §7's issue #102.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver
    }

    /// Double-click a Board sidebar row: opens-or-activates its document tab,
    /// then promotes it to pinned (§2e rule 3). Pinned tabs are used
    /// throughout so no scenario below is perturbed by the single preview
    /// slot's replace-in-place rule.
    fn open_pinned_tab<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let (x, y) = sidebar_hit(driver, row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2285 clause): Board sidebar row {row:?} must be on \
                 screen to double-click.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.double_click(x, y);
        driver.render();
    }

    /// Activate an already-open document tab by clicking its label in the
    /// strip (§2e rule 2's "activate its existing tab", via the strip rather
    /// than the sidebar so no sidebar-selection side effect is involved).
    fn activate_tab<A: AppLogic>(driver: &mut TuiDriver<A>, tab_label: &str) {
        let (x, y) = main_hit(driver, tab_label).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2285 clause): document tab {tab_label:?} must be \
                 rendered in the strip to be activated (§2b labels each tab \
                 `#<N> <title>`).\n--- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.render();
    }

    /// Click a Board detail sub-tab (`Board` / `Issue` / `Board Chat` /
    /// `Terminal`) in the sub-tab bar.
    ///
    /// The sub-tab bar is located by content — the one main-panel row that
    /// carries all four labels — so it keeps working when the doc-tab strip
    /// pushes it down a row (§2a) or back up again (§4's empty state).
    fn click_subtab<A: AppLogic>(driver: &mut TuiDriver<A>, label: &str) {
        let bar = rows(driver).into_iter().enumerate().find_map(|(y, row)| {
            let band = main_slice(&row);
            let all = [SUBTAB_BOARD, SUBTAB_ISSUE, SUBTAB_TERMINAL]
                .iter()
                .all(|l| band.contains(l));
            all.then_some((y, band))
        });
        let (y, band) = bar.unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2285 clause): contract §2a pins that the \
                 `Board / Issue / Chat / Terminal` sub-tab bar always renders on the Board \
                 panel — no main-panel row carries all of {SUBTAB_BOARD:?}, {SUBTAB_ISSUE:?} \
                 and {SUBTAB_TERMINAL:?}.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        let off = band.find(label).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2285 clause): the Board sub-tab bar must offer a \
                 {label:?} sub-tab. Bar row was {band:?}.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        // +1: `label` carries the TabItem's own leading pad space; clicking
        // the first glyph of the label proper stays inside the tab's hit box.
        let col = MAIN_START_COL + band[..off].chars().count() + 1;
        driver.click(col as f32 + 0.5, y as f32 + 0.5);
        driver.render();
    }

    /// One wheel notch down over the main panel = one line of detail scroll.
    fn scroll_main_down<A: AppLogic>(driver: &mut TuiDriver<A>, notches: usize) {
        for _ in 0..notches {
            driver.dispatch(UiEvent::Scroll {
                widget: None,
                delta: ScrollDelta::new(0.0, -1.0),
                position: Point::new(80.0, 20.0),
            });
        }
        driver.render();
    }

    /// Is `tag`'s issue body (harness note 4's line markers) on screen at all?
    fn body_visible<A: AppLogic>(driver: &TuiDriver<A>, tag: &str) -> bool {
        driver.screen_contains(&format!("{tag}-line-"))
    }

    /// Board panel with two pinned tabs — `#102` then `#103`, so `#103` is
    /// the active one (§2e rule 3 activates what it promotes).
    fn two_pinned_tabs() -> TuiDriver<impl AppLogic> {
        let mut driver = board_driver();
        open_pinned_tab(&mut driver, ROW_102);
        open_pinned_tab(&mut driver, ROW_103);
        let strip_text = strip(&driver)
            .map(|(_, t)| t)
            .unwrap_or_else(|| String::from("<no strip row>"));
        assert_eq!(
            strip_text.matches('×').count(),
            2,
            "contract §2e (a PRECONDITION of §5, owned by #2282): two double clicks on \
             #102/#103 must leave two pinned Board tabs open — §5's whole subject is what \
             each of two open tabs remembers. Strip row was {strip_text:?}.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        driver
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §5 — the sub-tab selection is per-tab
    // ═══════════════════════════════════════════════════════════════════════

    /// §5 bullet 1, first half: "Open tab A on `#102`, switch its sub-tab
    /// to `Issue`. Open tab B on `#103` (**still on Board's default `Board`
    /// sub-tab**)."
    ///
    /// A newly-opened tab starts from the defaults the issue body pins
    /// ("Defaults on open match today's defaults (`Board` / `Overview`,
    /// scroll 0)") — it must not inherit the sub-tab the previously-active
    /// tab happened to be on.
    #[test]
    fn a_newly_opened_tab_starts_on_the_default_board_sub_tab() {
        let mut driver = board_driver();
        open_pinned_tab(&mut driver, ROW_102);
        click_subtab(&mut driver, SUBTAB_ISSUE);
        assert!(
            body_visible(&driver, "BBB"),
            "PRECONDITION: clicking the `Issue` sub-tab must render issue #102's body \
             (the fixture seeds it as `BBB-line-01…40`), otherwise nothing below can \
             distinguish one tab's sub-tab from another's.\n--- screen ---\n{}",
            driver.screen(),
        );

        open_pinned_tab(&mut driver, ROW_103);

        assert!(
            !body_visible(&driver, "CCC"),
            "contract §5 bullet 1: the detail pane's sub-tab is per-document — a tab \
             opened while another tab sits on `Issue` must start on the DEFAULT `Board` \
             sub-tab (issue #2285: \"Defaults on open match today's defaults (Board / \
             Overview, scroll 0)\"), not inherit `Issue`. Tab #103 opened straight into \
             its issue body (`CCC-line-…` is on screen), which is what a single global \
             `board_detail_tab` shared by every tab looks like.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains(BOARD_SUBTAB_MARKER),
            "contract §5 bullet 1: a freshly-opened tab renders the default `Board` \
             sub-tab, whose content is the detail summary \
             `mocks/board-baseline-no-tabs.screen` draws ({BOARD_SUBTAB_MARKER:?}) — it is \
             absent.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §5 bullet 1, second half: "Switching back to tab A must show `Issue`
    /// still active for A […] and switching to B must show `[Board]` active
    /// for B, not `[Issue]`."
    ///
    /// Tab B is driven onto a *different* sub-tab explicitly, so the
    /// assertion cannot pass by accident on an implementation that simply
    /// leaves one global selection alone.
    #[test]
    fn switching_back_to_a_tab_restores_that_tabs_own_sub_tab() {
        let mut driver = two_pinned_tabs();

        // A = #102 → Issue.
        activate_tab(&mut driver, TAB_102);
        click_subtab(&mut driver, SUBTAB_ISSUE);
        assert!(
            body_visible(&driver, "BBB"),
            "PRECONDITION: tab #102 must be on its `Issue` sub-tab, showing \
             `BBB-line-…`.\n--- screen ---\n{}",
            driver.screen(),
        );

        // B = #103 → explicitly the Board sub-tab.
        activate_tab(&mut driver, TAB_103);
        click_subtab(&mut driver, SUBTAB_BOARD);
        assert!(
            !body_visible(&driver, "CCC") && driver.screen_contains(BOARD_SUBTAB_MARKER),
            "PRECONDITION: tab #103 must be on its `Board` sub-tab, showing \
             {BOARD_SUBTAB_MARKER:?} and no issue body.\n--- screen ---\n{}",
            driver.screen(),
        );

        // Back to A: it kept `Issue`.
        activate_tab(&mut driver, TAB_102);
        assert!(
            body_visible(&driver, "BBB"),
            "contract §5 bullet 1: each open tab keeps its OWN sub-tab. Tab #102 was left \
             on `Issue`; tab #103 was then switched to `Board`; activating #102 again must \
             show #102's issue body (`BBB-line-…`) once more. It does not — the sub-tab \
             selection is shared between tabs, which is exactly the single global \
             `board_detail_tab` this issue removes.\n--- screen ---\n{}",
            driver.screen(),
        );

        // And B still has its own.
        activate_tab(&mut driver, TAB_103);
        assert!(
            !body_visible(&driver, "CCC"),
            "contract §5 bullet 1: switching back to tab #103 must show ITS sub-tab \
             (`Board`), not tab #102's `Issue` — #103's issue body is on \
             screen.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §5 — the scroll position is per-tab
    // ═══════════════════════════════════════════════════════════════════════

    /// §5 bullet 2: "Scroll position within a tab's sub-tab content is
    /// likewise per-tab: scrolling tab A's Issue body must not move tab B's
    /// (independently-scrolled) content." Issue #2285: "each open tab keeps
    /// its own sub-tab, scroll position and expanded stage."
    ///
    /// Both halves are asserted: B's body is at its own top while A is
    /// scrolled, and A is still where it was left after the round trip.
    #[test]
    fn scrolling_one_tabs_issue_body_leaves_the_other_tabs_position_alone() {
        let mut driver = two_pinned_tabs();

        // A = #102, Issue sub-tab, scrolled 5 lines down.
        activate_tab(&mut driver, TAB_102);
        click_subtab(&mut driver, SUBTAB_ISSUE);
        assert!(
            driver.screen_contains("BBB-line-01"),
            "PRECONDITION: tab #102's issue body must start at scroll 0, with \
             `BBB-line-01` visible.\n--- screen ---\n{}",
            driver.screen(),
        );
        scroll_main_down(&mut driver, 5);
        assert!(
            !driver.screen_contains("BBB-line-01") && driver.screen_contains("BBB-line-03"),
            "PRECONDITION: five wheel notches over the Board detail pane must scroll tab \
             #102's issue body down five lines — `BBB-line-01` scrolled off, `BBB-line-03` \
             now the first body line. Scrolling itself is pre-existing behaviour, not a \
             #2285 clause.\n--- screen ---\n{}",
            driver.screen(),
        );

        // B = #103, its own Issue body, its own (untouched) scroll.
        activate_tab(&mut driver, TAB_103);
        click_subtab(&mut driver, SUBTAB_ISSUE);
        assert!(
            driver.screen_contains("CCC-line-01"),
            "contract §5 bullet 2: scroll position is per-document — scrolling tab #102's \
             Issue body five lines down must NOT move tab #103's, which has never been \
             scrolled and must render from its first line (`CCC-line-01`).\n\
             --- screen ---\n{}",
            driver.screen(),
        );

        // A is still where it was left.
        activate_tab(&mut driver, TAB_102);
        assert!(
            driver.screen_contains("BBB-line-03") && !driver.screen_contains("BBB-line-01"),
            "contract §5 bullet 2 / issue #2285 (\"each open tab keeps its own […] scroll \
             position\"): tab #102's Issue body was scrolled five lines down before tab \
             #103 was activated; activating #102 again must restore that position \
             (`BBB-line-03` first, `BBB-line-01` still scrolled off). It reset instead, \
             which is what a single global scroll field shared by every tab looks \
             like.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §5 — closing a tab discards its sub-state
    // ═══════════════════════════════════════════════════════════════════════

    /// §5 bullet 3: "Closing a tab discards its sub-state; re-opening the same
    /// issue number starts from `Board` / scroll 0 again — it does not
    /// remember the old sub-state."
    ///
    /// The close itself is #2283's clause (§4) and is used here only as this
    /// scenario's precondition — the subject is what the *re-opened* tab
    /// remembers.
    #[test]
    fn closing_a_tab_discards_its_sub_state_and_reopening_starts_from_the_defaults() {
        let mut driver = board_driver();
        open_pinned_tab(&mut driver, ROW_102);
        click_subtab(&mut driver, SUBTAB_ISSUE);
        scroll_main_down(&mut driver, 5);
        assert!(
            driver.screen_contains("BBB-line-03") && !driver.screen_contains("BBB-line-01"),
            "PRECONDITION: tab #102 must be on its `Issue` sub-tab, scrolled five lines \
             down, before it is closed.\n--- screen ---\n{}",
            driver.screen(),
        );

        // Close it via its `×` (§2d's close glyph, §4's close gesture).
        let (x, y) = main_hit(&driver, "×").unwrap_or_else(|| {
            panic!(
                "PRECONDITION (§2d/§4, owned by #2282/#2283): the open tab must render a \
                 trailing `×` close glyph to click.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        driver.click(x, y);
        driver.render();
        assert!(
            strip(&driver).is_none(),
            "PRECONDITION (§4, owned by #2283): closing the last open tab must leave the \
             zero-tab baseline with no strip row.\n--- screen ---\n{}",
            driver.screen(),
        );

        // Re-open the same issue: defaults, not the discarded sub-state.
        open_pinned_tab(&mut driver, ROW_102);
        assert!(
            !body_visible(&driver, "BBB"),
            "contract §5 bullet 3: closing a tab DISCARDS its sub-state — re-opening the \
             same issue number must start from the default `Board` sub-tab, not resume the \
             `Issue` sub-tab the closed tab was on. #102's issue body is on screen \
             immediately after re-opening.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains(BOARD_SUBTAB_MARKER),
            "contract §5 bullet 3: a re-opened tab renders the default `Board` sub-tab, \
             whose content is {BOARD_SUBTAB_MARKER:?} — it is absent.\n--- screen ---\n{}",
            driver.screen(),
        );

        // …and scroll 0, not the discarded offset of 5.
        click_subtab(&mut driver, SUBTAB_ISSUE);
        assert!(
            driver.screen_contains("BBB-line-01"),
            "contract §5 bullet 3: the re-opened tab starts from scroll 0 — its Issue body \
             must render from `BBB-line-01`, not resume the five-line offset the closed \
             tab had.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §5 — the Terminal sub-tab belongs to its own tab
    // ═══════════════════════════════════════════════════════════════════════

    /// §5 bullet 4 / issue #2285's third acceptance criterion: "A terminal
    /// opened for issue A stays attached to tab A and keeps running while
    /// tab B is active."
    ///
    /// Scope, per harness note 2: session *liveness* has no screen-level
    /// observable in this suite (the driver has no tick pump, so no PTY is
    /// ever spawned). What is observable — and what a shared-sub-state
    /// implementation gets wrong — is that the Terminal sub-tab belongs to
    /// the tab that opened it: a newly-opened tab must not land in someone
    /// else's terminal, and the opening tab must still be in its own on
    /// return.
    ///
    /// TODO(test-author): contract §5 pins "keeps running" but names no
    /// observable for it (its own Note 6 concedes a screen grid cannot show
    /// it). The "still running" half is NOT asserted here.
    #[test]
    fn terminal_sub_tab_selection_follows_its_own_tab() {
        let mut driver = board_driver();
        open_pinned_tab(&mut driver, ROW_102);
        click_subtab(&mut driver, SUBTAB_TERMINAL);
        assert!(
            driver.screen_contains(TERMINAL_SUBTAB_MARKER),
            "PRECONDITION: clicking the `Terminal` sub-tab must show tab #102's terminal \
             pane ({TERMINAL_SUBTAB_MARKER:?} — no PTY is spawned in this suite, see this \
             slice's harness note 2).\n--- screen ---\n{}",
            driver.screen(),
        );

        // A second tab opens on its own defaults — not into #102's terminal.
        open_pinned_tab(&mut driver, ROW_103);
        assert!(
            !driver.screen_contains(TERMINAL_SUBTAB_MARKER),
            "contract §5 bullet 4: a terminal opened for issue #102 stays attached to \
             #102's TAB. Opening a tab for #103 dropped straight into a terminal pane, so \
             the Terminal sub-tab is shared between tabs rather than owned by the one that \
             opened it — a tab for a different issue must start on its default `Board` \
             sub-tab.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains(BOARD_SUBTAB_MARKER),
            "contract §5 bullet 4: the newly-opened tab #103 renders its default `Board` \
             sub-tab ({BOARD_SUBTAB_MARKER:?}) — it is absent.\n--- screen ---\n{}",
            driver.screen(),
        );

        // Returning to #102 lands back in ITS terminal.
        activate_tab(&mut driver, TAB_102);
        assert!(
            driver.screen_contains(TERMINAL_SUBTAB_MARKER),
            "contract §5 bullet 4: tab #102's terminal follows tab #102 — activating it \
             again must show its terminal pane, which is only torn down on tab CLOSE, \
             never on tab switch.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // Regression bar — "behaviour-preserving for a single open tab"
    // ═══════════════════════════════════════════════════════════════════════

    /// Issue #2285's fourth acceptance criterion: "With exactly one tab open,
    /// sub-tab and scroll behave exactly as they do today" — the design
    /// section's own regression bar ("This slice is behaviour-preserving for
    /// a single open tab"), and contract §2a's "the strictest form of
    /// #2285's behaviour-preserving bar" restated at one tab instead of zero.
    ///
    /// RATCHET. Green today (there is one global sub-state and one tab to own
    /// it) and must STAY green: moving the sub-state onto the document must
    /// not change what a single-tab session does. Deliberately asserts only
    /// what the contract pins — which sub-tab's content renders, and that the
    /// wheel scrolls it — and not the incidental sub-tab-switch scroll reset,
    /// which no clause fixes either way.
    #[test]
    fn a_single_open_tab_keeps_todays_sub_tab_and_scroll_behaviour() {
        let mut driver = board_driver();
        open_pinned_tab(&mut driver, ROW_102);

        assert!(
            !body_visible(&driver, "BBB") && driver.screen_contains(BOARD_SUBTAB_MARKER),
            "regression bar (#2285: \"With exactly one tab open, sub-tab and scroll behave \
             exactly as they do today\"): a freshly-opened tab shows the default `Board` \
             sub-tab ({BOARD_SUBTAB_MARKER:?}), not the issue body.\n--- screen ---\n{}",
            driver.screen(),
        );

        click_subtab(&mut driver, SUBTAB_ISSUE);
        assert!(
            driver.screen_contains("BBB-line-01"),
            "regression bar: with one tab open, clicking `Issue` shows that issue's body \
             from its first line.\n--- screen ---\n{}",
            driver.screen(),
        );

        scroll_main_down(&mut driver, 5);
        assert!(
            driver.screen_contains("BBB-line-03") && !driver.screen_contains("BBB-line-01"),
            "regression bar: with one tab open, the wheel still scrolls the Issue body — \
             five notches move it five lines.\n--- screen ---\n{}",
            driver.screen(),
        );

        click_subtab(&mut driver, SUBTAB_BOARD);
        assert!(
            !body_visible(&driver, "BBB") && driver.screen_contains(BOARD_SUBTAB_MARKER),
            "regression bar: with one tab open, clicking `Board` returns to the detail \
             summary and the issue body is no longer rendered.\n--- screen ---\n{}",
            driver.screen(),
        );
    }
}
