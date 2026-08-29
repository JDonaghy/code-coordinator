// Sealed acceptance slice for **issue #2287** — coord-tui document tabs:
// discoverability (status-bar hints, `?` help entries, right-click tab menu) —
// milestone ms-65 (tracking issue #2289, "coord-tui: per-panel document tabs
// (preview/pin)").
//
// Authored independently from `tests/acceptance/ms-65/contract.md` (Gate A)
// and its mocks, with **zero** worker/implementation context: no work branch,
// PR or commit for #2287 (or any other ms-65 issue) was read. Every assertion
// below is derived from contract §8 and the two mocks that section indexes
// (`mocks/board-tab-context-menu.screen`, `mocks/board-tabs-help-overlay.screen`),
// plus §8a's own two reference mocks (`mocks/board-preview-tab.screen` for the
// hint-present case, `mocks/board-baseline-no-tabs.screen` for the absent one).
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-65 mock declares (contract §0).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2287 may run it
// (`coord acceptance run --issue 2287`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §8 only. §2 (#2282 Board strip), §3 (#2284 Pipeline's own set),
// §4 (#2283 close/navigate/overflow), §5 (#2285 per-tab sub-state) and §6
// (#2286 persistence) are other issues' slices. §2 and §4 are *preconditions*
// of everything here — every scenario below has to open tabs before it can
// read a hint or right-click one, and §8c's menu items close tabs — but this
// file never re-asserts one of their clauses as its own subject: every helper
// assertion that leans on them says so in its panic message. §9 (#2288 split)
// is deliberately untouched; see the TODO at the bottom for the one §8b clause
// that overlaps it.
//
// ── Harness facts this slice had to design around ─────────────────────────
//
// 1. **`TuiDriver` at the pinned quadraui rev** (`tui/Cargo.toml`:
//    `d6ae247c007721284dc895d4fbf42b5f4a5ba47f`) has real `right_click()`,
//    `double_click()` and `set_double_click_folding()`. Right-clicks are
//    therefore delivered through the driver rather than by hand-rolling a
//    `UiEvent::MouseDown { button: Right, .. }` (which the older ms-38 slices
//    had to do). Folding is pinned OFF so two clicks at the same cell are two
//    *single* clicks, never a wall-clock race against the 400 ms
//    `DoubleClickDetector` window.
//
// 2. **Targeting one tab out of several.** Every tab paints the same §2d `×`
//    close glyph, so `driver.find("×")` can only ever hit the leftmost one.
//    quadraui's `TuiDriver::tab_close_center(&WidgetId, idx)` would be the
//    ergonomic answer but needs the `WidgetId` the *implementation* gives the
//    doc-tab bar — an implementation detail a sealed, independently authored
//    slice must not guess at. `label_pos()` below resolves a target purely
//    from the rendered grid: locate the tab's `#<N>` tag in the strip row and
//    click there. Per contract §0 every glyph in play (`× ∘ ‹ › … ▸ │`) is one
//    column wide, so a char index into a `screen()` row IS its cell column.
//
// 3. **Menu-item activation is not pinned by the contract.** §8c pins the four
//    labels and what each one *does* to the tab set, but not the gesture that
//    invokes an open menu item. A left click on the item's own label is the
//    only gesture every context menu in this app already answers to (ms-38 §4
//    pins right-click-to-open for the Plans menus and never names a second
//    activation path), so that is what `click_menu_item()` sends. If #2287
//    ships keyboard-only activation, that is a contract gap, not a test defect.
//
// 4. **`"Close"` is a prefix of `"Close others"` and `"Close all"`**, so a bare
//    `screen_contains("Close")` would pass on a menu that only offers the two
//    longer items. `menu_item_row()` therefore rejects a match whose label is
//    immediately followed by another item's continuation (`" others"` /
//    `" all"`). It deliberately does NOT require the row to end after the
//    label: §8c pins no accelerator column for this menu, and ms-38's menus do
//    render one (`Refresh … r`), so an item row carrying an accelerator must
//    still count.

mod tabs_discoverability_2287 {
    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::{AppLogic, NamedKey};

    /// The fixture issue set contract §7 pins for every ms-65 mock: five
    /// `claude-coordinator` issues, #101–#105, with the exact titles the mocks'
    /// tab labels were rendered from.
    ///
    /// Wire shape is `make_app_with_board_json`'s (the daemon's `/board`
    /// payload) — §7 leaves the outer schema to #2281, which chose this one.
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

    /// Sidebar-row click targets. The mocks draw these rows as
    /// `"#102 Auth token refresh bug"`; the shipped 35-column sidebar renders
    /// `"  #102  Auth token refresh b"` — **two** spaces after the number.
    /// These prefixes are taken from a real 120×40 render for that reason, and
    /// the double space also keeps them unambiguous once tabs exist (a tab
    /// label is `#<N> <title>` with a *single* space, §2b).
    const ROW_101: &str = "#101  Fix login race";
    const ROW_102: &str = "#102  Auth token refresh";
    const ROW_103: &str = "#103  Race condition in";

    /// §8a's pinned hint string, verbatim from the contract ("Pinned, exact
    /// substring, taken verbatim from the issue body", two spaces between
    /// segments) and from `mocks/board-preview-tab.screen`'s status-bar row.
    const TAB_HINTS: &str = "click=preview  dbl-click=pin  ctrl-w=close  ctrl-tab=next";

    // ═══════════════════════════════════════════════════════════════════════
    // Fixture + grid helpers
    //
    // Everything in this block is a *precondition* harness, not a #2287
    // clause: each panic message says so, so a failure here reads as a
    // fixture/§2/§4 finding rather than as a missing discoverability
    // behaviour.
    // ═══════════════════════════════════════════════════════════════════════

    /// Board panel with the §7 fixture seeded and its sidebar issue rows
    /// revealed, on the contract's pinned 120×40 grid (§0).
    ///
    /// `SidebarView::Board` is the app's default active view, so no
    /// activity-bar click is needed. The seeded repo's `No milestone` group is
    /// collapsed by default (#857), so it is expanded here.
    fn board_driver() -> TuiDriver<impl AppLogic> {
        let app = make_app_with_board_json(BOARD_JSON);
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        // Two clicks at the same cell must be two SINGLE clicks, never a
        // wall-clock-dependent fold into a `DoubleClick` (harness note 1).
        driver.set_double_click_folding(false);
        driver.render();

        if !driver.screen_contains(ROW_101) {
            let before = driver.screen();
            let (x, y) = driver.find("No milestone (5)").unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2287 clause): the Board sidebar must render \
                     the seeded repo's collapsed \"No milestone (5)\" group header for \
                     contract §7's five-issue fixture — not found.\n--- screen ---\n{before}"
                )
            });
            driver.click(x, y);
            driver.render();
        }
        assert!(
            driver.screen_contains(ROW_101),
            "ms-65 baseline (NOT a #2287 clause): the Board sidebar must render a row for \
             contract §7's issue #101.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver
    }

    /// Screen rows, 0-indexed, as the grid the mocks are written in.
    fn rows<A: AppLogic>(driver: &TuiDriver<A>) -> Vec<String> {
        driver.screen().lines().map(str::to_string).collect()
    }

    /// The global status bar: the last row of the 120×40 grid (contract §0,
    /// "Row 39 (last) is the global status bar").
    fn status_bar<A: AppLogic>(driver: &TuiDriver<A>) -> String {
        rows(driver).pop().unwrap_or_else(|| {
            panic!(
                "harness sanity: the 120×40 grid must have a last row to read the status \
                 bar from.\n--- screen ---\n{}",
                driver.screen()
            )
        })
    }

    /// The document tab strip: the first row carrying the §2d close glyph `×`
    /// (U+00D7, `quadraui::tui::tab_bar::TAB_CLOSE_CHAR`).
    ///
    /// Unambiguous: the shipped Board screen contains no `×` anywhere — its
    /// `[P]urge` toolbar button uses `✕` (U+2715), a different code point —
    /// and neither does `mocks/board-baseline-no-tabs.screen`.
    ///
    /// `None` when no strip is rendered (the zero-tab state, §2a — which is
    /// also §8c's "Close all" target).
    fn strip<A: AppLogic>(driver: &TuiDriver<A>) -> Option<(usize, String)> {
        rows(driver)
            .into_iter()
            .enumerate()
            .find(|(_, r)| r.contains('×'))
    }

    /// The strip row's `(row index, text)`, or a diagnosis.
    fn strip_row<A: AppLogic>(driver: &TuiDriver<A>) -> (usize, String) {
        strip(driver).unwrap_or_else(|| {
            panic!(
                "contract §2a/§2d (a PRECONDITION of every §8 clause, owned by #2282): with \
                 ≥1 document tab open the Board panel must render a tab-strip row in which \
                 every open tab carries the trailing `×` close glyph — no row on screen \
                 contains `×`.\n--- screen ---\n{}",
                driver.screen()
            )
        })
    }

    /// The strip row's text.
    fn strip_text<A: AppLogic>(driver: &TuiDriver<A>) -> String {
        strip_row(driver).1
    }

    /// How many tabs the strip shows, counted the way contract §4/§8c mandate:
    /// `×` occurrences in the strip row (§2e: "count the `×` occurrences … not
    /// tab colour/style").
    fn tab_count<A: AppLogic>(driver: &TuiDriver<A>) -> usize {
        strip_text(driver).matches('×').count()
    }

    /// Index of the `Board / Issue / Board Chat / Terminal` sub-tab bar row
    /// (contract §2a — pre-dates this milestone, renders regardless of tab
    /// state). Located by its shipped labels rather than by §2a's literal
    /// `"[Board]"`, which is a mock artifact (quadraui's `draw_tab_bar` marks
    /// the active sub-tab with colour, not brackets) — the same resolution the
    /// #2282 and #2283 slices reached and flagged in `manifest.yml`.
    fn subtab_row_index<A: AppLogic>(driver: &TuiDriver<A>) -> usize {
        rows(driver)
            .iter()
            .position(|r| r.contains(" Board ") && r.contains(" Issue ") && r.contains(" Terminal"))
            .unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2287 clause): contract §2a pins the \
                     `Board / Issue / Chat / Terminal` sub-tab bar as always rendered on the \
                     Board panel — no row carries all of \" Board \", \" Issue \" and \
                     \" Terminal\".\n--- screen ---\n{}",
                    driver.screen()
                )
            })
    }

    /// Column of the first cell of `needle` within `text`, or `None`.
    ///
    /// Char index == cell column here: contract §0 pins every glyph this
    /// milestone introduces (`∘ ▸ ‹ › × ║`) as one column wide, and the rest of
    /// the grid's content (activity-bar icons, `│`, box-drawing, ASCII, `…`) is
    /// one-column too — so no wide-cell padding can shift the two apart.
    fn col_of(text: &str, needle: &str) -> Option<usize> {
        let hay: Vec<char> = text.chars().collect();
        let pin: Vec<char> = needle.chars().collect();
        if pin.is_empty() || hay.len() < pin.len() {
            return None;
        }
        hay.windows(pin.len()).position(|w| w == pin.as_slice())
    }

    /// Click point on the *body* (the `#<N>` tag) of the tab tagged `tag`.
    fn label_pos<A: AppLogic>(driver: &TuiDriver<A>, tag: &str) -> (f32, f32) {
        let (y, text) = strip_row(driver);
        let x = col_of(&text, tag).unwrap_or_else(|| {
            panic!(
                "contract §2b (a PRECONDITION of §8c, owned by #2282): the tab strip must \
                 label each open tab with its `{tag}` issue tag so a right-click can target \
                 one tab out of several. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        (x as f32 + 0.5, y as f32 + 0.5)
    }

    /// Single-click the sidebar row whose text starts with `row`. Per §2e rule
    /// 1 this opens (or replaces) the scope's one preview tab.
    fn click_row<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let before = driver.screen();
        let (x, y) = driver.find(row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2287 clause): sidebar row {row:?} must be on screen \
                 to click.\n--- screen ---\n{before}"
            )
        });
        driver.click(x, y);
        driver.render();
    }

    /// Double-click the sidebar row whose text starts with `row`, the way the
    /// real runner delivers one: `MouseDown` first, then `DoubleClick` at the
    /// same position (quadraui's `DoubleClickDetector` *replaces* the second
    /// `MouseDown`). Per §2e rule 3 this opens-or-activates the row's tab and
    /// then promotes it to pinned.
    fn dbl_click_row<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let before = driver.screen();
        let (x, y) = driver.find(row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2287 clause): sidebar row {row:?} must be on screen \
                 to double-click.\n--- screen ---\n{before}"
            )
        });
        driver.click(x, y);
        driver.double_click(x, y);
        driver.render();
    }

    /// Open `#101`, `#102`, `#103` as three pinned tabs with `#103` active —
    /// the state `mocks/board-pinned-3-tabs.screen` depicts and the one
    /// `mocks/board-tab-context-menu.screen` right-clicks into (that mock's
    /// strip row shows the same three tabs, with `#102` the clicked one).
    ///
    /// Three separate double clicks, each landing while no preview tab is
    /// open, is the sequence §2e traces to that end state.
    fn three_pinned_tabs() -> TuiDriver<impl AppLogic> {
        let mut driver = board_driver();
        dbl_click_row(&mut driver, ROW_101);
        dbl_click_row(&mut driver, ROW_102);
        dbl_click_row(&mut driver, ROW_103);
        assert_eq!(
            tab_count(&driver),
            3,
            "contract §2e (a PRECONDITION of §8c, owned by #2282): three double clicks on \
             #101/#102/#103 must leave three pinned tabs open — \
             `mocks/board-pinned-3-tabs.screen`. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
        driver
    }

    /// Which tab is active, read off §2c's bracket convention (`[<label> ×]`).
    /// Returns the `#<N>` tag of the bracketed tab.
    ///
    /// Matches `[` only where a tab label can actually begin — `[#` (pinned)
    /// or `[∘` (§1's preview marker) — rather than the first `[` on the row,
    /// so an unrelated bracket painted in the strip row's sidebar columns can
    /// never be mistaken for the active tab.
    fn active_tag<A: AppLogic>(driver: &TuiDriver<A>) -> String {
        let text = strip_text(driver);
        let chars: Vec<char> = text.chars().collect();
        let open = (0..chars.len().saturating_sub(1))
            .find(|&i| chars[i] == '[' && (chars[i + 1] == '#' || chars[i + 1] == '∘'))
            .unwrap_or_else(|| {
                panic!(
                    "contract §2c (a PRECONDITION of §8c, owned by #2282): the active tab is \
                     wrapped in `[` `]`, so a strip with ≥1 open tab always has exactly one \
                     bracketed tab. Strip row was {text:?}.\n--- screen ---\n{}",
                    driver.screen()
                )
            });
        let tag: String = chars[open + 1..]
            .iter()
            // Skip §1's `∘ ` preview marker, which sits between the bracket
            // and the `#<N>` tag on a preview tab.
            .skip_while(|&&c| c == '∘' || c == ' ')
            .take_while(|c| !c.is_whitespace())
            .collect();
        assert!(
            tag.starts_with('#'),
            "contract §2b/§2c (a PRECONDITION of §8c): the bracketed active tab must start \
             with its `#<N>` issue tag; got {tag:?} from strip row \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        tag
    }

    /// The `#<N>` tags currently rendered in the strip, left to right — §8c's
    /// "without changing tab count or **order**".
    fn strip_tags<A: AppLogic>(driver: &TuiDriver<A>) -> Vec<String> {
        let text = strip_text(driver);
        let chars: Vec<char> = text.chars().collect();
        let mut tags = Vec::new();
        let mut i = 0;
        while i < chars.len() {
            if chars[i] == '#' {
                let tag: String = chars[i..]
                    .iter()
                    .take_while(|c| **c == '#' || c.is_ascii_digit())
                    .collect();
                i += tag.chars().count();
                if tag.len() > 1 {
                    tags.push(tag);
                }
            } else {
                i += 1;
            }
        }
        tags
    }

    // ── Context-menu helpers (harness notes 3 + 4) ─────────────────────────

    /// Right-click the tab tagged `tag` — §8c's new right-click target.
    fn right_click_tab<A: AppLogic>(driver: &mut TuiDriver<A>, tag: &str) {
        let (x, y) = label_pos(driver, tag);
        driver.right_click(x, y);
        driver.render();
    }

    /// `(row index, row text)` of the rendered menu item labelled `label`, or
    /// `None`.
    ///
    /// Rejects a row where `label` is only the prefix of a *different* item —
    /// `"Close"` inside `"Close others"` / `"Close all"` — so a menu missing
    /// the bare `Close` item cannot pass on the strength of its siblings
    /// (harness note 4).
    fn menu_item_row<A: AppLogic>(driver: &TuiDriver<A>, label: &str) -> Option<(usize, String)> {
        const SIBLING_CONTINUATIONS: [&str; 2] = [" others", " all"];
        rows(driver).into_iter().enumerate().find(|(_, row)| {
            match col_of(row, label) {
                None => false,
                Some(i) => {
                    let rest: String = row.chars().skip(i + label.chars().count()).collect();
                    !SIBLING_CONTINUATIONS.iter().any(|s| rest.starts_with(s))
                }
            }
        })
    }

    /// Assert the open menu offers `label` as an item of its own.
    fn assert_menu_item<A: AppLogic>(driver: &TuiDriver<A>, label: &str, clicked: &str) {
        assert!(
            menu_item_row(driver, label).is_some(),
            "contract §8c: right-clicking an open tab ({clicked}) opens a context menu whose \
             items are exactly `Close`, `Close others`, `Close all`, `Pin tab` (\"exact \
             labels, taken verbatim from the tracking issue\") — no {label:?} item is on \
             screen. See `mocks/board-tab-context-menu.screen`.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Activate the open menu's `label` item by clicking it (harness note 3).
    fn click_menu_item<A: AppLogic>(driver: &mut TuiDriver<A>, label: &str) {
        let (y, text) = menu_item_row(driver, label).unwrap_or_else(|| {
            panic!(
                "contract §8c: the tab context menu must offer a {label:?} item to \
                 activate.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        let x = col_of(&text, label).expect("row matched the label above");
        driver.click(x as f32 + 0.5, y as f32 + 0.5);
        driver.render();
    }

    /// Board panel with the `?` help overlay open (§8b).
    ///
    /// Opened with **zero** document tabs: §8b's testable clause is
    /// unconditional ("`driver.press('?')` while the Board panel is active"),
    /// and #2287's own rationale — the feature "is undiscoverable by anyone
    /// who did not read the issue" — is at its strongest before the operator
    /// has opened anything. `mocks/board-tabs-help-overlay.screen` happens to
    /// depict the overlay over a 3-tab board, but the contract pins no tab
    /// precondition for it. See the coordinator note in `manifest.yml`.
    fn board_with_help_overlay() -> TuiDriver<impl AppLogic> {
        let mut driver = board_driver();
        driver.type_char('?');
        driver.render();
        driver
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §8a — status-bar hint
    // ═══════════════════════════════════════════════════════════════════════

    /// §8a: "With **at least one** doc tab open on the active panel,
    /// `driver.screen_contains(\"click=preview\")` is `true`."
    ///
    /// One single click opens one preview tab (§2e rule 1) — the state
    /// `mocks/board-preview-tab.screen` depicts, which §8a names as its
    /// hint-present reference.
    #[test]
    fn status_bar_shows_the_tab_hints_while_a_tab_is_open() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_102);
        assert_eq!(
            tab_count(&driver),
            1,
            "contract §2e rule 1 (a PRECONDITION, owned by #2282): a single click on #102 \
             opens exactly one preview tab.\n--- screen ---\n{}",
            driver.screen(),
        );

        assert!(
            driver.screen_contains("click=preview"),
            "contract §8a: with at least one document tab open on the active panel the \
             status bar advertises the tab gestures — `screen_contains(\"click=preview\")` \
             must be true. `mocks/board-preview-tab.screen` is §8a's own hint-present \
             reference. Status bar row was {:?}.\n--- screen ---\n{}",
            status_bar(&driver),
            driver.screen(),
        );
    }

    /// §8a's stronger half: the hint is a **pinned, exact substring**, "taken
    /// verbatim from the issue body" — `click=preview  dbl-click=pin
    /// ctrl-w=close  ctrl-tab=next`, two spaces between segments, matching
    /// this app's existing hint-string convention and
    /// `mocks/board-preview-tab.screen`'s status-bar row.
    ///
    /// Separate from the test above deliberately: if an implementation
    /// advertises all four gestures but spaces or orders them differently,
    /// exactly one id goes red and the finding is unambiguous.
    #[test]
    fn status_bar_tab_hint_is_the_contracts_verbatim_string() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_102);

        assert!(
            driver.screen_contains(TAB_HINTS),
            "contract §8a: the status-bar hint is pinned as an EXACT substring — \
             {TAB_HINTS:?} (two spaces between segments). Status bar row was \
             {:?}.\n--- screen ---\n{}",
            status_bar(&driver),
            driver.screen(),
        );
    }

    /// §8a + #2287 AC: "with none, it does not." The dynamic half — a hint
    /// that appears when the first tab opens must disappear again when the
    /// last one closes.
    ///
    /// Distinct from the zero-tab control below, which an implementation that
    /// latches the hint on for the rest of the session (or that keys it off
    /// "this panel *has* a tab set" rather than "the set is non-empty") would
    /// still pass.
    ///
    /// `Ctrl-W` (§4, #2283) is the close gesture; it is shipped, and closing
    /// is not this issue's subject — the assertion is about the hint.
    #[test]
    fn status_bar_tab_hints_disappear_when_the_last_tab_closes() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_102);
        assert!(
            driver.screen_contains("click=preview"),
            "contract §8a: the hint must be present with one tab open before its \
             disappearance can be observed (asserted on its own in \
             `status_bar_shows_the_tab_hints_while_a_tab_is_open`). Status bar row was \
             {:?}.\n--- screen ---\n{}",
            status_bar(&driver),
            driver.screen(),
        );

        driver.ctrl_char('w');
        driver.render();
        assert!(
            strip(&driver).is_none(),
            "contract §4 (a PRECONDITION, owned by #2283): `Ctrl-W` closes the active — here \
             the only — tab, so no strip remains.\n--- screen ---\n{}",
            driver.screen(),
        );

        assert!(
            !driver.screen_contains("click=preview"),
            "contract §8a: with ZERO doc tabs open the tab hint is absent — the status bar \
             falls back to the pre-ms-65 per-panel hint \
             (`mocks/board-baseline-no-tabs.screen`). It must go away when the last tab is \
             closed, not merely be absent before the first one is opened. Status bar row was \
             {:?}.\n--- screen ---\n{}",
            status_bar(&driver),
            driver.screen(),
        );
    }

    /// CONTROL — green today and required to STAY green.
    ///
    /// §8a: "With **zero** doc tabs open, that string is **absent** — the
    /// status bar falls back to the pre-ms-65 per-panel hint (Board:
    /// `n=notify  m=merge  R=retry  P=purge  q=quit`, unchanged). See
    /// `mocks/board-baseline-no-tabs.screen`."
    ///
    /// This is the regression bar for every §8a clause above: an
    /// implementation that unconditionally appends the tab hints — or that
    /// replaces the Board's own hint set with them — satisfies the tests above
    /// and breaks this one.
    ///
    /// The fallback half is asserted as the contract's own four Board hints,
    /// each independently: `mocks/board-baseline-no-tabs.screen`'s status row
    /// is `n=notify  m=merge  R=retry  P=purge  q=quit`, and §8a says
    /// "unchanged", so this is a pre-existing string, not something #2287 is
    /// being asked to introduce.
    ///
    /// Deliberately NOT listed in `manifest.yml`'s `expected_red` block: it
    /// passed in the authoring run and must keep passing.
    #[test]
    fn zero_tabs_status_bar_keeps_the_boards_own_hints_and_shows_no_tab_hints() {
        let driver = board_driver();
        assert!(
            strip(&driver).is_none(),
            "contract §2a (a PRECONDITION, owned by #2282): a freshly seeded Board has zero \
             document tabs open and renders no strip.\n--- screen ---\n{}",
            driver.screen(),
        );

        assert!(
            !driver.screen_contains("click=preview"),
            "contract §8a: with ZERO doc tabs open the tab-hint string is ABSENT — \
             `mocks/board-baseline-no-tabs.screen` shows the plain Board hint row. Status \
             bar row was {:?}.\n--- screen ---\n{}",
            status_bar(&driver),
            driver.screen(),
        );
        for hint in ["n=notify", "R=retry", "P=purge", "q=quit"] {
            assert!(
                driver.screen_contains(hint),
                "contract §8a: with zero tabs \"the status bar falls back to the pre-ms-65 \
                 per-panel hint (Board: `n=notify  m=merge  R=retry  P=purge  q=quit`, \
                 UNCHANGED)\" — the {hint:?} segment must survive #2287's status-bar work. \
                 Status bar row was {:?}.\n--- screen ---\n{}",
                status_bar(&driver),
                driver.screen(),
            );
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §8b — `?` help overlay
    // ═══════════════════════════════════════════════════════════════════════

    /// §8b: "`driver.press('?')` while the Board panel is active →
    /// `driver.screen_contains(\"Board — Help\")`" — the title mirrors the
    /// existing `"Plans — Help"` convention (ms-38 §5b) and
    /// `mocks/board-tabs-help-overlay.screen` renders it in the overlay's top
    /// border.
    ///
    /// The em-dash title occurs nowhere else on a Board screen, so this fails
    /// red while no Board overlay is wired up.
    #[test]
    fn help_overlay_opens_on_question_mark_with_the_board_title() {
        let driver = board_with_help_overlay();
        assert!(
            driver.screen_contains("Board — Help"),
            "contract §8b: pressing `?` while the Board panel is active must open the help \
             overlay, whose title is exactly \"Board — Help\" (em-dash, mirroring the \
             shipped \"Plans — Help\").\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §8b: "`driver.screen_contains(\"open preview tab\")`,
    /// `driver.screen_contains(\"pin tab\")`,
    /// `driver.screen_contains(\"close active tab\")`,
    /// `driver.screen_contains(\"cycle tabs\")` are all `true` while the
    /// overlay is open."
    ///
    /// The contract picks each phrase so it does **not** already appear in
    /// §8a's status-bar hint string — "so the assertion cannot pass with the
    /// overlay closed". `mocks/board-tabs-help-overlay.screen` renders them as
    /// the right-hand column of the **Document tabs** section.
    #[test]
    fn help_overlay_lists_the_document_tab_key_bindings() {
        let driver = board_with_help_overlay();
        for phrase in [
            "open preview tab",
            "pin tab",
            "close active tab",
            "cycle tabs",
        ] {
            assert!(
                driver.screen_contains(phrase),
                "contract §8b: the `?` overlay lists the document-tab key bindings — \
                 {phrase:?} must be on screen while it is open (each phrase is chosen to not \
                 appear in §8a's status-bar hint, so it cannot pass with the overlay \
                 closed). See `mocks/board-tabs-help-overlay.screen`.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    /// §8b: the **Document tabs** section is one row per gesture, and two of
    /// those six rows are the mouse gestures §8a's status bar cannot advertise
    /// — `middle-click` (close) and `right-click` (the §8c menu).
    ///
    /// This is the half of #2287's rationale the hint line does not cover:
    /// "every interaction in this milestone is a mouse gesture whose meaning is
    /// not visible on screen". Needles are the mock's own left-column labels.
    #[test]
    fn help_overlay_lists_the_mouse_tab_gestures_under_a_document_tabs_heading() {
        let driver = board_with_help_overlay();
        for phrase in ["Document tabs", "middle-click tab", "right-click tab"] {
            assert!(
                driver.screen_contains(phrase),
                "contract §8b: the `?` overlay's \"Document tabs\" section has one row per \
                 gesture, including the two mouse gestures the status bar cannot advertise \
                 (middle-click = close, right-click = the §8c tab menu) — {phrase:?} must be \
                 on screen. See `mocks/board-tabs-help-overlay.screen`.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    /// §8b: "`Esc` closes it; status bar while open is `\" Esc=close \"`
    /// (mirrors ms-38 §5i / §9i's identical convention for Plans' own overlay
    /// — reuse the existing overlay chrome, do not build a second one)."
    #[test]
    fn help_overlay_advertises_and_honours_escape_to_close() {
        let mut driver = board_with_help_overlay();
        assert!(
            driver.screen_contains("Board — Help"),
            "contract §8b: the overlay must be open before its Esc behaviour can be observed \
             (asserted on its own in \
             `help_overlay_opens_on_question_mark_with_the_board_title`).\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains("Esc=close"),
            "contract §8b: while the overlay is open the status bar reads \" Esc=close \" — \
             the same chrome the shipped Plans overlay uses (ms-38 §5i). Status bar row was \
             {:?}.\n--- screen ---\n{}",
            status_bar(&driver),
            driver.screen(),
        );

        driver.press_named(NamedKey::Escape);
        driver.render();
        assert!(
            !driver.screen_contains("Board — Help"),
            "contract §8b: `Esc` closes the help overlay.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §8c — right-click tab menu
    // ═══════════════════════════════════════════════════════════════════════

    /// §8c: "`driver.screen_contains(\"Close others\")` and
    /// `driver.screen_contains(\"Close all\")` and
    /// `driver.screen_contains(\"Pin tab\")` are all `true` after right-clicking
    /// an open tab" — plus the fourth item, `"Close"`, which the section lists
    /// first ("exact labels, taken verbatim from the tracking issue") and
    /// `mocks/board-tab-context-menu.screen` renders at the top of the menu box.
    ///
    /// `#102` is the right-clicked tab, matching the mock (its strip shows
    /// `#101`, `[#102 …]`, `#103` with the menu hanging under `#102`).
    ///
    /// `Close` is checked as a standalone item, not as a bare substring: it is
    /// a prefix of both `Close others` and `Close all` (harness note 4).
    #[test]
    fn right_clicking_a_tab_opens_a_menu_with_close_close_others_close_all_and_pin_tab() {
        let mut driver = three_pinned_tabs();
        right_click_tab(&mut driver, "#102");

        for label in ["Close", "Close others", "Close all", "Pin tab"] {
            assert_menu_item(&driver, label, "#102");
        }
    }

    /// §8c: "\"Close others\" leaves exactly the clicked tab open (count check,
    /// §4's pattern)."
    ///
    /// Right-clicks the **middle** tab of three, so "leaves exactly the clicked
    /// tab" has a victim on each side and cannot pass by closing only one
    /// neighbour.
    #[test]
    fn close_others_leaves_exactly_the_right_clicked_tab() {
        let mut driver = three_pinned_tabs();
        right_click_tab(&mut driver, "#102");
        click_menu_item(&mut driver, "Close others");

        let text = strip_text(&driver);
        assert_eq!(
            text.matches('×').count(),
            1,
            "contract §8c: \"Close others\" leaves EXACTLY the clicked tab open — from three \
             open tabs the strip's `×` count must drop to 1. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            text.contains("#102"),
            "contract §8c: the tab that survives \"Close others\" is the CLICKED one (#102), \
             not whichever happened to be active. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for closed in ["#101", "#103"] {
            assert!(
                !text.contains(closed),
                "contract §8c: \"Close others\" closes every OTHER tab — {closed} must be \
                 gone. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
        assert_eq!(
            active_tag(&driver),
            "#102",
            "contract §8c + §2c: with exactly one tab left it is the active one, so #102 is \
             the bracketed tab. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §8c: "\"Close all\" leaves none open — the strip disappears, same end
    /// state as §4's \"closing the last tab\"", i.e.
    /// `mocks/board-baseline-no-tabs.screen`.
    ///
    /// Asserted the way §4's own empty-state clause is: the strip's whole glyph
    /// vocabulary is gone from the entire grid, and the sub-tab bar is back on
    /// the row it occupied before any tab was opened (so the strip reserved no
    /// row, §2a).
    #[test]
    fn close_all_leaves_no_tabs_and_restores_the_zero_tab_state() {
        let mut driver = board_driver();
        let subtab_before = subtab_row_index(&driver);
        dbl_click_row(&mut driver, ROW_101);
        dbl_click_row(&mut driver, ROW_102);
        dbl_click_row(&mut driver, ROW_103);

        right_click_tab(&mut driver, "#102");
        click_menu_item(&mut driver, "Close all");

        assert!(
            !driver.screen_contains("×"),
            "contract §8c: \"Close all\" leaves NO tab open — the strip disappears, so no `×` \
             close glyph remains anywhere on the grid \
             (`mocks/board-baseline-no-tabs.screen`).\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            strip(&driver).is_none(),
            "contract §8c: after \"Close all\" there is no tab-strip row at \
             all.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            subtab_row_index(&driver),
            subtab_before,
            "contract §8c + §2a: \"Close all\" reaches the \"same end state as §4's closing \
             the last tab\" — the strip \"renders nothing and reserves no row\", so the \
             `Board / Issue / Chat / Terminal` sub-tab bar must be back on the exact row it \
             occupied before any tab was opened.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains("click=preview"),
            "contract §8c + §8a: with every tab closed the status bar falls back to the \
             Board's own hint — the tab hints must go with the strip. Status bar row was \
             {:?}.\n--- screen ---\n{}",
            status_bar(&driver),
            driver.screen(),
        );
    }

    /// §8c's first item, `"Close"` — the menu route to §4's `×`.
    ///
    /// TODO(test-author): §8c's testable list spells out the effect of
    /// "Close others", "Close all" and "Pin tab" but not of "Close". This test
    /// takes the only reading the label admits and the one §4 already pins for
    /// the `×` route: the CLICKED tab closes, the others survive, the count
    /// drops by exactly one. If "Close" was meant to close the *active* tab
    /// rather than the right-clicked one, that needs a contract amendment —
    /// the two differ here on purpose (the right-clicked tab #102 is not the
    /// active one, #103).
    #[test]
    fn close_menu_item_closes_the_right_clicked_tab_and_no_other() {
        let mut driver = three_pinned_tabs();
        assert_eq!(
            active_tag(&driver),
            "#103",
            "contract §2e (a PRECONDITION, owned by #2282): the last double-clicked tab \
             (#103) is active, so the right-clicked tab below (#102) is deliberately NOT the \
             active one.\n--- screen ---\n{}",
            driver.screen(),
        );

        right_click_tab(&mut driver, "#102");
        click_menu_item(&mut driver, "Close");

        let text = strip_text(&driver);
        assert_eq!(
            text.matches('×').count(),
            2,
            "contract §8c: the menu's \"Close\" item closes one tab — the strip's `×` count \
             must go from 3 to 2. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains("#102"),
            "contract §8c: \"Close\" closes the RIGHT-CLICKED tab (#102), the same tab its \
             `×` would (§4). Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for survivor in ["#101", "#103"] {
            assert!(
                text.contains(survivor),
                "contract §8c: \"Close\" closes one tab and no other — {survivor} must \
                 survive. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    /// §8c: "\"Pin tab\" on a preview tab drops its `∘ ` marker (§1) without
    /// changing tab count or order." Also #2287's own AC: "\"Pin tab\" promotes
    /// a preview tab."
    ///
    /// Fixture: `#101` pinned by a double click, then a single click on `#102`
    /// appends the scope's one preview tab (§2e rule 1's append branch, the
    /// 2-tab intermediate state §2e describes). That gives both a marker to
    /// drop AND an order to preserve — a single-tab fixture could not observe
    /// the latter.
    #[test]
    fn pin_tab_promotes_a_preview_tab_without_changing_count_or_order() {
        let mut driver = board_driver();
        dbl_click_row(&mut driver, ROW_101);
        click_row(&mut driver, ROW_102);
        assert_eq!(
            tab_count(&driver),
            2,
            "contract §2e (a PRECONDITION, owned by #2282): a pinned #101 plus a single \
             click on #102 leaves two tabs open. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
        assert!(
            strip_text(&driver).contains('∘'),
            "contract §1/§2b (a PRECONDITION, owned by #2282): the single-clicked #102 is \
             the scope's PREVIEW tab, so its label carries the `∘ ` marker — otherwise \
             \"Pin tab\" has nothing to promote. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
        let tags_before = strip_tags(&driver);

        right_click_tab(&mut driver, "#102");
        click_menu_item(&mut driver, "Pin tab");

        let text = strip_text(&driver);
        assert!(
            !text.contains('∘'),
            "contract §8c + §1: \"Pin tab\" promotes the preview tab, which drops the `∘ ` \
             marker — the plain-text signal §1 pins for `is_preview`. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            text.matches('×').count(),
            2,
            "contract §8c: \"Pin tab\" promotes in place — it must not change the tab COUNT. \
             Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            strip_tags(&driver),
            tags_before,
            "contract §8c: \"Pin tab\" must not change tab ORDER either — the strip's tags \
             must read the same, left to right, as before the promotion. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §8c: "\"Pin tab\" is hidden or inert when the clicked tab is already
    /// pinned (not depicted in the mock — a dynamic state the test-author
    /// drives, not a static fact)."
    ///
    /// The contract permits **either** resolution, so neither presence nor
    /// absence of the item can be asserted on its own. What IS pinned by both
    /// readings, and is what the clause exists to prevent, is the observable
    /// outcome: right-clicking an already-pinned tab and taking whatever the
    /// menu offers must leave the tab set exactly as it was — same count, same
    /// order, and still no `∘ ` marker anywhere (an implementation that
    /// *toggled* preview state instead of promoting would fail here while
    /// passing every other §8c test).
    ///
    /// The menu itself must still open on a pinned tab — §8c gates only "Pin
    /// tab", not the menu — so `Close others` is asserted present first, which
    /// also keeps this test red rather than vacuous while no menu exists.
    #[test]
    fn pin_tab_is_hidden_or_inert_on_an_already_pinned_tab() {
        let mut driver = three_pinned_tabs();
        let tags_before = strip_tags(&driver);
        assert!(
            !strip_text(&driver).contains('∘'),
            "contract §2e (a PRECONDITION, owned by #2282): three double clicks leave three \
             PINNED tabs — no `∘ ` preview marker. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );

        right_click_tab(&mut driver, "#102");
        assert_menu_item(&driver, "Close others", "#102");

        if menu_item_row(&driver, "Pin tab").is_some() {
            // The "inert" resolution: the item renders, activating it does
            // nothing observable.
            click_menu_item(&mut driver, "Pin tab");
        } else {
            // The "hidden" resolution: dismiss the menu and assert the same
            // invariants.
            driver.press_named(NamedKey::Escape);
            driver.render();
        }

        let text = strip_text(&driver);
        assert!(
            !text.contains('∘'),
            "contract §8c: \"Pin tab\" is hidden or INERT on an already-pinned tab — it must \
             never turn a pinned tab back into a preview one, so no `∘ ` marker may appear. \
             Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            text.matches('×').count(),
            3,
            "contract §8c: neither opening the menu on a pinned tab nor an inert \"Pin tab\" \
             may close anything — all three tabs stay open. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            strip_tags(&driver),
            tags_before,
            "contract §8c: an inert \"Pin tab\" leaves the strip's order untouched. Strip row \
             was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ───────────────────────────────────────────────────────────────────────
    // NOT AUTHORED — deliberately, rather than guessed.
    //
    // TODO(test-author): §8b describes the `?` overlay as having TWO sections
    // — "Document tabs" (asserted above) and "Split" (§9's four `Ctrl-W`
    // chords), and `mocks/board-tabs-help-overlay.screen` renders the latter's
    // four rows (`Ctrl-W v  split right`, `Ctrl-W s  split down`,
    // `Ctrl-W w  next pane`, `Ctrl-W x  close pane`). Those rows are NOT
    // asserted here. #2287's own acceptance criteria say only "`?` lists the
    // tab key bindings", the split keys are §9's (#2288, which the milestone's
    // work order puts AFTER this issue), and asserting them in this slice
    // would gate #2287's acceptance run on a later issue's surface. If the
    // Split section is meant to be gated by acceptance, it belongs in #2288's
    // slice — flagged to the coordinator in `manifest.yml`.
    //
    // TODO(test-author): §8a pins the hint for "whenever the active panel owns
    // a tab set" and its testable clauses are written for the Board. The
    // Pipeline panel owns its own set (§3), so the same hint presumably
    // applies there, but the contract names no Pipeline hint-present mock and
    // finding 7 in `manifest.yml` (the #2284 author's) documents a live
    // fixture-wipe blocker on entering the Pipeline panel from this external
    // test crate. No Pipeline hint assertion is authored for that reason.
    //
    // TODO(test-author): §8c pins no keyboard route to the tab context menu
    // and no accelerators for its four items, so none are asserted (harness
    // notes 3 and 4). It also does not say whether the menu closes on `Esc`;
    // `pin_tab_is_hidden_or_inert_on_an_already_pinned_tab` sends `Esc` only
    // on the "hidden" branch and asserts nothing about its effect on the menu.
    // ───────────────────────────────────────────────────────────────────────
}
