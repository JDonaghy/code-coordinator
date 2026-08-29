// Sealed acceptance slice for **issue #2283** — coord-tui document tabs:
// close (`×`, middle-click, `Ctrl-W`), `Ctrl-Tab` / `Ctrl-Shift-Tab` cycling,
// overflow scroll affordances, and the close-the-last-tab empty state —
// milestone ms-65 (tracking issue #2289, "coord-tui: per-panel document tabs
// (preview/pin)").
//
// Authored independently from `tests/acceptance/ms-65/contract.md` (Gate A)
// and its mocks, with **zero** worker/implementation context: no work branch,
// PR or commit for #2283 (or any other ms-65 issue) was read. Every assertion
// below is derived from contract §4 and the mock that section indexes
// (`mocks/board-tabs-overflow.screen`), plus §4's explicit empty-state target
// `mocks/board-baseline-no-tabs.screen`.
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-65 mock declares (contract §0).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2283 may run it
// (`coord acceptance run --issue 2283`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §4 only. §2 (#2282 Board strip: where it renders, label format,
// active bracket, open semantics, reveal-on-activate) is a *precondition* of
// everything here — every scenario below has to open tabs before it can close
// or cycle them — but it is #2282's slice (`board_tabs_2282.rs`) that asserts
// it, and this file never re-asserts a §2 clause as its own subject. §3
// (#2284 Pipeline's own set), §5 (#2285 per-tab sub-state), §6 (#2286
// persistence), §8 (#2287 discoverability — including §8b's `?` overlay row
// for `Ctrl-W`/`Ctrl-Tab`, and §8c's right-click menu whose "Close" /
// "Close others" / "Close all" items close tabs by a *different* route) and §9
// (#2288 split, whose `Ctrl-W v` / `Ctrl-W w` / `Ctrl-W x` chords share this
// issue's `Ctrl-W` leader) are other issues' slices and are untouched here.
//
// ── Harness facts this slice had to design around ─────────────────────────
//
// 1. **The quadraui pin moved.** `tui/Cargo.toml` now pins
//    `d6ae247c007721284dc895d4fbf42b5f4a5ba47f`, at which quadraui#592 and
//    #594 HAVE landed: `TuiDriver` has real `double_click()`, `middle_click()`
//    and `set_double_click_folding()` helpers. (The sibling `board_tabs_2282.rs`
//    slice was authored against the older `d70da7d` pin and hand-rolls the
//    `MouseDown` + `DoubleClick` pair for that reason; this file does not need
//    to.) `set_double_click_folding(false)` is set in the fixture so that two
//    `click()`s at the same spot are two *single* clicks, never a wall-clock
//    race against the 400 ms `DoubleClickDetector` window, and `dbl_click()`
//    sends the `MouseDown` **then** `DoubleClick` pair a real double click
//    delivers.
//
// 2. **Targeting one tab's `×` out of several.** Every tab paints the same
//    §2d close glyph, so `driver.find("×")` can only ever hit the leftmost
//    one. quadraui#594's `TuiDriver::tab_close_center(&WidgetId, idx)` would
//    be the ergonomic answer, but it needs the `WidgetId` the *implementation*
//    gives the doc-tab bar — an implementation detail a sealed, independently
//    authored slice must not guess at. So `close_col()` / `label_col()` below
//    resolve a target purely from the rendered grid: locate the tab's `#<N>`
//    tag in the strip row, then take the first `×` at or after it. Per
//    contract §0 every glyph in play (`× ∘ ‹ › … ▸ │`) is one column wide, so
//    a char index into a `screen()` row IS its cell column.
//
// 3. **Label truncation is deliberately never pinned here.** §2b pins 20/22
//    columns and `mocks/board-pinned-3-tabs.screen` renders that
//    (`#101 Fix login race… ×`), but `mocks/board-tabs-overflow.screen` — §4's
//    own mock — renders a visibly narrower label (`#102 Auth… ×`). That
//    discrepancy is §2b's to resolve (#2282), not §4's, so every assertion
//    below keys off the `#<N>` tag, the `×` count and the `[`/`]` active
//    bracket, all of which both mocks agree on. See the note to the
//    coordinator in `manifest.yml`.
//
// 4. **`Ctrl-Shift-Tab` has two plausible wire encodings.** The contract pins
//    the user-facing chord, not the `KeyCode`. A real terminal may deliver it
//    as `Key::Named(Tab)` + `{ctrl, shift}` or as `Key::Named(BackTab)` +
//    `{ctrl, shift}` (crossterm folds Shift-Tab into `KeyCode::BackTab`;
//    quadraui's `tui::events` maps that to `NamedKey::BackTab`). Pinning one
//    would be inventing a detail the contract is silent on, so
//    `ctrl_shift_tab()` sends the `Tab` form and, only if the frame did not
//    change, the `BackTab` form — either encoding satisfies the clause, and
//    handling neither still fails.

mod board_tabs_close_2283 {
    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::{AppLogic, Key, Modifiers, NamedKey, UiEvent};

    /// The fixture issue set contract §7 pins for every ms-65 mock: five
    /// `claude-coordinator` issues, #101–#105, with the exact titles the mocks'
    /// tab labels were rendered from. Five is also exactly what §4's overflow
    /// scenario needs ("5 Board tabs open, only 4 fit the strip width").
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
    const ROW_104: &str = "#104  Flaky CI on macOS";
    const ROW_105: &str = "#105  Memory leak in watch";

    // ═══════════════════════════════════════════════════════════════════════
    // Fixture + grid helpers
    //
    // Everything in this block is a *precondition* harness, not a #2283
    // clause: each panic message says so, so a failure here reads as a
    // fixture/baseline finding rather than as a missing close/navigate
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
                    "ms-65 baseline (NOT a #2283 clause): the Board sidebar must render \
                     the seeded repo's collapsed \"No milestone (5)\" group header for \
                     contract §7's five-issue fixture — not found.\n--- screen ---\n{before}"
                )
            });
            driver.click(x, y);
            driver.render();
        }
        assert!(
            driver.screen_contains(ROW_101),
            "ms-65 baseline (NOT a #2283 clause): the Board sidebar must render a row for \
             contract §7's issue #101.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver
    }

    /// Screen rows, 0-indexed, as the grid the mocks are written in.
    fn rows<A: AppLogic>(driver: &TuiDriver<A>) -> Vec<String> {
        driver.screen().lines().map(str::to_string).collect()
    }

    /// The document tab strip: the first row carrying the §2d close glyph `×`
    /// (U+00D7, `quadraui::tui::tab_bar::TAB_CLOSE_CHAR`).
    ///
    /// Unambiguous: the shipped Board screen contains no `×` anywhere — its
    /// `[P]urge` toolbar button uses `✕` (U+2715), a different code point —
    /// and neither does `mocks/board-baseline-no-tabs.screen`.
    ///
    /// `None` when no strip is rendered (the zero-tab state, §2a — which is
    /// exactly §4's close-the-last-tab target).
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
                "contract §2a/§2d (a PRECONDITION of every §4 clause, owned by #2282): with \
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

    /// How many tabs the strip shows, counted the way contract §4 mandates:
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
    /// #2282 slice reached and flagged in `manifest.yml`.
    fn subtab_row_index<A: AppLogic>(driver: &TuiDriver<A>) -> usize {
        rows(driver)
            .iter()
            .position(|r| r.contains(" Board ") && r.contains(" Issue ") && r.contains(" Terminal"))
            .unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2283 clause): contract §2a pins the \
                     `Board / Issue / Chat / Terminal` sub-tab bar as always rendered on the \
                     Board panel — no row carries all of \" Board \", \" Issue \" and \
                     \" Terminal\".\n--- screen ---\n{}",
                    driver.screen()
                )
            })
    }

    /// Column of the first cell of `needle` within the strip row, or `None`.
    ///
    /// Char index == cell column here: contract §0 pins every glyph this
    /// milestone introduces (`∘ ▸ ‹ › × ║`) as one column wide, and the strip
    /// row's remaining content (activity-bar icons, `│`, ASCII, `…`) is
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
                "contract §2b (a PRECONDITION of §4, owned by #2282): the tab strip must \
                 label each open tab with its `{tag}` issue tag so §4's clauses can target \
                 one tab out of several. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        (x as f32 + 0.5, y as f32 + 0.5)
    }

    /// Click point on the `×` close button belonging to the tab tagged `tag` —
    /// the first `×` at or after that tab's label. See harness note 2 for why
    /// this resolves against the rendered grid rather than via
    /// `TuiDriver::tab_close_center`.
    fn close_pos<A: AppLogic>(driver: &TuiDriver<A>, tag: &str) -> (f32, f32) {
        let (y, text) = strip_row(driver);
        let start = col_of(&text, tag).unwrap_or_else(|| {
            panic!(
                "contract §2b (a PRECONDITION of §4, owned by #2282): the tab strip must \
                 label each open tab with its `{tag}` issue tag. Strip row was \
                 {text:?}.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        let chars: Vec<char> = text.chars().collect();
        let x = (start..chars.len())
            .find(|&i| chars[i] == '×')
            .unwrap_or_else(|| {
                panic!(
                    "contract §2d (a PRECONDITION of §4, owned by #2282): every open, \
                     closable tab renders a trailing `×`, so the tab tagged {tag} must have \
                     one at or after its label. Strip row was \
                     {text:?}.\n--- screen ---\n{}",
                    driver.screen()
                )
            });
        (x as f32 + 0.5, y as f32 + 0.5)
    }

    /// Single-click the sidebar row whose text starts with `row`.
    fn click_row<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let before = driver.screen();
        let (x, y) = driver.find(row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2283 clause): sidebar row {row:?} must be on screen \
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
                "ms-65 baseline (NOT a #2283 clause): sidebar row {row:?} must be on screen \
                 to double-click.\n--- screen ---\n{before}"
            )
        });
        driver.click(x, y);
        driver.double_click(x, y);
        driver.render();
    }

    /// Open `#101`, `#102`, `#103` as three pinned tabs with `#103` active —
    /// the state `mocks/board-pinned-3-tabs.screen` depicts, and the smallest
    /// fixture in which §4's "closes exactly that tab **and no other**" and
    /// its left-neighbour rule are both observable.
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
            "contract §2e (a PRECONDITION of §4, owned by #2282): three double clicks on \
             #101/#102/#103 must leave three pinned tabs open — \
             `mocks/board-pinned-3-tabs.screen`. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
        driver
    }

    /// Open all five fixture issues as pinned tabs, `#105` active — the state
    /// `mocks/board-tabs-overflow.screen` depicts ("5 Board tabs open, only 4
    /// fit the strip width; the active tab (`#105`…) is kept on-screen").
    ///
    /// Note what is deliberately NOT asserted here: that five `×` are on
    /// screen. In the overflow scenario they cannot be — hiding some is the
    /// whole point — so a visible-count precondition would be unsatisfiable by
    /// any correct implementation. The precondition is instead the pair the
    /// mock states about this fixture and that holds however many tabs fit:
    /// a strip renders, and the last-double-clicked tab (#105) is the active
    /// one.
    fn five_pinned_tabs() -> TuiDriver<impl AppLogic> {
        let mut driver = board_driver();
        for row in [ROW_101, ROW_102, ROW_103, ROW_104, ROW_105] {
            dbl_click_row(&mut driver, row);
        }
        assert_eq!(
            active_tag(&driver),
            "#105",
            "contract §2e (a PRECONDITION of §4's overflow clauses, owned by #2282): five \
             double clicks on #101–#105 must leave the last one, #105, as the active tab — \
             the state `mocks/board-tabs-overflow.screen` depicts. Strip row was \
             {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
        driver
    }

    /// `Ctrl-W` — §4's "closes the active tab".
    fn ctrl_w<A: AppLogic>(driver: &mut TuiDriver<A>) {
        driver.ctrl_char('w');
        driver.render();
    }

    /// `Ctrl-Tab` — §4's "moves active to the next tab, wrapping from the last
    /// to the first".
    fn ctrl_tab<A: AppLogic>(driver: &mut TuiDriver<A>) {
        driver.dispatch(UiEvent::KeyPressed {
            key: Key::Named(NamedKey::Tab),
            modifiers: Modifiers {
                ctrl: true,
                ..Modifiers::default()
            },
            repeat: false,
        });
        driver.render();
    }

    /// `Ctrl-Shift-Tab` — §4's "moves to the previous, wrapping from the first
    /// to the last".
    ///
    /// Sends the `Ctrl`+`Shift`+`Tab` encoding first and, only if the *tab
    /// strip* is unchanged, the `Ctrl`+`Shift`+`BackTab` encoding a
    /// crossterm-backed terminal would deliver instead. The contract pins the
    /// chord, not the `KeyCode`, so accepting either is the honest reading —
    /// see harness note 4.
    ///
    /// The fallback is gated on the strip row specifically, not on the whole
    /// frame: an unhandled `Tab` chord still repaints incidental chrome (the
    /// sidebar's focus ring, measured), so a whole-screen comparison would
    /// swallow the fallback and report the wrong encoding as "handled".
    fn ctrl_shift_tab<A: AppLogic>(driver: &mut TuiDriver<A>) {
        let before = strip_text(driver);
        let mods = Modifiers {
            ctrl: true,
            shift: true,
            ..Modifiers::default()
        };
        driver.dispatch(UiEvent::KeyPressed {
            key: Key::Named(NamedKey::Tab),
            modifiers: mods,
            repeat: false,
        });
        driver.render();
        if strip_text(driver) == before {
            driver.dispatch(UiEvent::KeyPressed {
                key: Key::Named(NamedKey::BackTab),
                modifiers: mods,
                repeat: false,
            });
            driver.render();
        }
    }

    /// Which tab is active, read off §2c's bracket convention (`[<label> ×]`).
    /// Returns the `#<N>` tag of the bracketed tab.
    ///
    /// Matches `[` only where a tab label can actually begin — `[#` (pinned)
    /// or `[∘` (§1's preview marker) — rather than the first `[` on the row,
    /// so an unrelated bracket painted in the strip row's sidebar columns
    /// (e.g. the sidebar's own `[ ↻ Sync (S) ]` / `[Filter issues… ]` chrome)
    /// can never be mistaken for the active tab.
    fn active_tag<A: AppLogic>(driver: &TuiDriver<A>) -> String {
        let text = strip_text(driver);
        let chars: Vec<char> = text.chars().collect();
        let open = (0..chars.len().saturating_sub(1))
            .find(|&i| chars[i] == '[' && (chars[i + 1] == '#' || chars[i + 1] == '∘'))
            .unwrap_or_else(|| {
                panic!(
                    "contract §2c (a PRECONDITION of §4, owned by #2282): the active tab is \
                     wrapped in `[` `]`, so a strip with ≥1 open tab always has exactly one \
                     bracketed tab — §4's navigation clauses are asserted through it. Strip \
                     row was {text:?}.\n--- screen ---\n{}",
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
            "contract §2b/§2c (a PRECONDITION of §4): the bracketed active tab must start \
             with its `#<N>` issue tag; got {tag:?} from strip row \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        tag
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4 — close by `×`, and the click that must NOT close
    // ═══════════════════════════════════════════════════════════════════════

    /// §4: "Clicking a tab's `×` closes exactly that tab (verify via the
    /// `#<N>` count in the strip row before/after — down by exactly one
    /// occurrence)." The issue's own acceptance criterion adds "…and no
    /// other", so the two untouched tabs and the untouched active tab are
    /// asserted too.
    ///
    /// `#102` — an *inactive*, middle tab — is the target, so "no other"
    /// covers a neighbour on each side.
    #[test]
    fn clicking_a_tabs_close_glyph_closes_exactly_that_tab() {
        let mut driver = three_pinned_tabs();
        let (x, y) = close_pos(&driver, "#102");
        driver.click(x, y);
        driver.render();

        let text = strip_text(&driver);
        assert_eq!(
            text.matches('×').count(),
            2,
            "contract §4: clicking a tab's `×` closes exactly that tab — the strip's `×` \
             count must go down by exactly one, from 3 to 2. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains("#102"),
            "contract §4: clicking #102's `×` must close #102's tab. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for survivor in ["#101", "#103"] {
            assert!(
                text.contains(survivor),
                "contract §4 / #2283 AC: clicking a tab's `×` closes that tab AND NO OTHER \
                 — {survivor}'s tab must survive #102's close. Strip row was \
                 {text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
        assert_eq!(
            active_tag(&driver),
            "#103",
            "contract §4 / #2283 AC: closing an INACTIVE tab closes \"that tab and no \
             other\", so it must not move the active tab either — #103 was active before \
             #102's `×` was clicked and must still be. (§4 defines a new active tab only \
             for the close-the-ACTIVE-tab case.) Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// #2283 AC: "Clicking a tab's `×` closes that tab and no other; clicking
    /// its **body** activates it." The negative half — a body click must not
    /// close — is what makes the `×` hit-test (`TabBarHit::TabClose(i)` vs.
    /// `TabBarHit::Tab(i)`) observable from outside.
    #[test]
    fn clicking_a_tab_body_activates_it_without_closing_it() {
        let mut driver = three_pinned_tabs();
        assert_eq!(
            active_tag(&driver),
            "#103",
            "contract §2e (a PRECONDITION, owned by #2282): the last double-clicked tab \
             (#103) is the active one before this test's click.\n--- screen ---\n{}",
            driver.screen(),
        );

        let (x, y) = label_pos(&driver, "#101");
        driver.click(x, y);
        driver.render();

        let text = strip_text(&driver);
        assert_eq!(
            text.matches('×').count(),
            3,
            "contract §4 / #2283 AC: clicking a tab's BODY activates it — it must not \
             close anything, so all three tabs stay open. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            active_tag(&driver),
            "#101",
            "contract §4 / #2283 AC: clicking a tab's body ACTIVATES that tab, so #101 \
             becomes the bracketed one (§2c). Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4 — middle-click close
    // ═══════════════════════════════════════════════════════════════════════

    /// §4: "Middle-click anywhere on a tab (not just its `×`) closes it — same
    /// count check." Deliberately aimed at the tab's `#<N>` label body, i.e.
    /// the cell where a *left* click activates instead (test above) — that
    /// contrast is the whole content of "anywhere on a tab, not just its `×`".
    #[test]
    fn middle_clicking_a_tab_body_closes_it() {
        let mut driver = three_pinned_tabs();
        let (x, y) = label_pos(&driver, "#102");
        driver.middle_click(x, y);
        driver.render();

        let text = strip_text(&driver);
        assert_eq!(
            text.matches('×').count(),
            2,
            "contract §4: middle-clicking anywhere on a tab — here on #102's label body, \
             not on its `×` — closes it, so the strip's `×` count goes from 3 to 2. Strip \
             row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains("#102"),
            "contract §4: the middle-clicked tab (#102) is the one that closes. Strip row \
             was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for survivor in ["#101", "#103"] {
            assert!(
                text.contains(survivor),
                "contract §4: a middle click closes the tab it lands on and no other — \
                 {survivor} must survive. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4 — `Ctrl-W` and the pinned neighbour rule
    // ═══════════════════════════════════════════════════════════════════════

    /// §4: "`Ctrl-W` closes the active tab and activates a **defined
    /// neighbour**: the tab immediately to its left…"
    ///
    /// Three pinned tabs, `#103` active and rightmost → `Ctrl-W` → two tabs,
    /// `#103` gone, `#102` (its left neighbour) active.
    #[test]
    fn ctrl_w_closes_the_active_tab_and_activates_its_left_neighbour() {
        let mut driver = three_pinned_tabs();
        ctrl_w(&mut driver);

        let text = strip_text(&driver);
        assert_eq!(
            text.matches('×').count(),
            2,
            "contract §4: `Ctrl-W` closes the ACTIVE tab, so the strip's `×` count goes \
             from 3 to 2. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains("#103"),
            "contract §4: the tab `Ctrl-W` closes is the active one (#103), not some other. \
             Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            active_tag(&driver),
            "#102",
            "contract §4: `Ctrl-W` activates a DEFINED neighbour — \"the tab immediately to \
             its left\". #103's left neighbour is #102, so #102 must be the bracketed tab \
             (§2c). Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §4, the other half of the pinned rule: "…or if it was the leftmost, the
    /// new leftmost."
    ///
    /// Three pinned tabs; `#101` (the leftmost) is activated by clicking its
    /// body, then `Ctrl-W` closes it — `#102`, the new leftmost, must be
    /// active. Note this is the branch a naive `index - 1` implementation
    /// silently gets wrong (it would underflow to the last tab, or clamp onto
    /// nothing).
    #[test]
    fn ctrl_w_on_the_leftmost_tab_activates_the_new_leftmost() {
        let mut driver = three_pinned_tabs();
        let (x, y) = label_pos(&driver, "#101");
        driver.click(x, y);
        driver.render();
        assert_eq!(
            active_tag(&driver),
            "#101",
            "contract §4 precondition (asserted as its own clause in \
             `clicking_a_tab_body_activates_it_without_closing_it`): clicking #101's body \
             makes it the active, leftmost tab.\n--- screen ---\n{}",
            driver.screen(),
        );

        ctrl_w(&mut driver);

        let text = strip_text(&driver);
        assert_eq!(
            text.matches('×').count(),
            2,
            "contract §4: `Ctrl-W` closes the active tab (#101), taking the strip from 3 \
             tabs to 2. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains("#101"),
            "contract §4: `Ctrl-W` closes the active tab, which here is the leftmost \
             (#101). Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            active_tag(&driver),
            "#102",
            "contract §4: closing the LEFTMOST tab has no left neighbour, so the rule's \
             second branch applies — \"the new leftmost\" becomes active, i.e. #102, NOT \
             #103 (an index-1 underflow wrapping to the end). Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4 — `Ctrl-Tab` / `Ctrl-Shift-Tab` cycling (wrapping at both ends)
    // ═══════════════════════════════════════════════════════════════════════

    /// §4: "`Ctrl-Tab` moves active to the next tab, wrapping from the last to
    /// the first."
    ///
    /// Starts on `#103` (last), so the very first press exercises the wrap —
    /// the end a "next = index + 1" implementation forgets — and the second
    /// press proves plain forward movement still works afterwards. Tab count
    /// is asserted throughout: cycling must never close anything.
    #[test]
    fn ctrl_tab_moves_to_the_next_tab_and_wraps_at_the_end() {
        let mut driver = three_pinned_tabs();

        ctrl_tab(&mut driver);
        assert_eq!(
            active_tag(&driver),
            "#101",
            "contract §4: `Ctrl-Tab` moves to the next tab, WRAPPING from the last to the \
             first — from #103 (last of #101/#102/#103) it must land on #101. Strip row \
             was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );

        ctrl_tab(&mut driver);
        assert_eq!(
            active_tag(&driver),
            "#102",
            "contract §4: a second `Ctrl-Tab` moves on to the next tab again — #101 → #102. \
             Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );

        assert_eq!(
            tab_count(&driver),
            3,
            "contract §4: `Ctrl-Tab` only CYCLES — it must never close a tab, so all three \
             stay open. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
    }

    /// §4: "`Ctrl-Shift-Tab` moves to the previous, wrapping from the first to
    /// the last."
    ///
    /// From `#103`: → `#102` → `#101` → (wrap) `#103`. The third press is the
    /// clause's real subject; the first two pin the direction, so a
    /// `Ctrl-Shift-Tab` wired to the *forward* action can't pass by accident.
    #[test]
    fn ctrl_shift_tab_moves_to_the_previous_tab_and_wraps_at_the_start() {
        let mut driver = three_pinned_tabs();

        ctrl_shift_tab(&mut driver);
        assert_eq!(
            active_tag(&driver),
            "#102",
            "contract §4: `Ctrl-Shift-Tab` moves to the PREVIOUS tab — from #103 that is \
             #102, not #101 (which is where the forward action would wrap to). Strip row \
             was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );

        ctrl_shift_tab(&mut driver);
        assert_eq!(
            active_tag(&driver),
            "#101",
            "contract §4: a second `Ctrl-Shift-Tab` continues backwards — #102 → #101. \
             Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );

        ctrl_shift_tab(&mut driver);
        assert_eq!(
            active_tag(&driver),
            "#103",
            "contract §4: `Ctrl-Shift-Tab` WRAPS from the first tab to the last — from #101 \
             it must land on #103. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );

        assert_eq!(
            tab_count(&driver),
            3,
            "contract §4: `Ctrl-Shift-Tab` only CYCLES — it must never close a tab. Strip \
             row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4 — overflow: `‹` / `›` affordances, active tab always visible
    // ═══════════════════════════════════════════════════════════════════════

    /// §4 + `mocks/board-tabs-overflow.screen`: five tabs open, `#105` active
    /// and rightmost. `#101` is scrolled out to the left, so `‹` renders;
    /// nothing is scrolled out to the right, so **`›` is absent** — the
    /// contract states this explicitly and warns "do not assert `›` against
    /// this mock". The active tab's `#<N>` is present regardless of scroll
    /// offset.
    ///
    /// Note this test does NOT pin how many tabs fit (that is
    /// `fit_active_scroll_offset`'s math, which the contract deliberately does
    /// not re-derive) nor the truncated label width (see harness note 3) — only
    /// that with five tabs on the mock's own 120×40 grid the strip overflows,
    /// which is the scenario the mock declares.
    #[test]
    fn overflowing_tabs_render_the_left_scroll_affordance_and_keep_the_active_tab_visible() {
        let driver = five_pinned_tabs();
        let text = strip_text(&driver);

        assert!(
            text.contains('‹'),
            "contract §4: with more tabs than fit the strip width and the leftmost tab \
             scrolled out, the `‹` scroll affordance renders — \
             `mocks/board-tabs-overflow.screen` shows it at the strip's left edge with \
             five tabs open and #105 active. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            text.contains("#105"),
            "contract §4: \"the active tab's `#<N>` substring is always present in the strip \
             row while that tab is active, regardless of scroll offset\" — #105 is active. \
             Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            active_tag(&driver),
            "#105",
            "contract §4 + §2c: #105 is the active tab in \
             `mocks/board-tabs-overflow.screen`, so it is the bracketed one — scrolling the \
             strip must not move the active tab. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains('›'),
            "contract §4: `›` renders only when tabs exist beyond the rightmost VISIBLE \
             one. In `mocks/board-tabs-overflow.screen` the active tab #105 is also the \
             fixture's actual last tab, so nothing is scrolled out to the right and `›` is \
             absent — the contract calls this out explicitly (\"do not assert `›` against \
             this mock\"). Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains("#101"),
            "contract §4: `‹` renders precisely BECAUSE the leftmost tab is scrolled out, \
             so #101 must not still be in the strip — otherwise the arrow is decoration, \
             not an overflow affordance. `mocks/board-tabs-overflow.screen` shows \
             `#102`–`#105` with #101 gone. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §4's stated symmetry: "`›` is exercised the same way `‹` is here, just
    /// from the other direction — e.g. activating `#101` instead of `#105` in
    /// this same fixture would scroll to show `#101`–`#104` with `›` present
    /// and `‹` absent … a test-author asserting `›` should drive that case".
    ///
    /// `#101`'s tab is scrolled out of the strip, so it cannot be clicked;
    /// `Ctrl-Tab`'s wrap from the last tab to the first is the in-scope §4 way
    /// to activate it. (Composing two §4 clauses is deliberate — both belong
    /// to this issue; `ctrl_tab_moves_to_the_next_tab_and_wraps_at_the_end`
    /// pins the wrap on its own, so a failure here is readable.)
    #[test]
    fn activating_a_scrolled_out_tab_scrolls_it_back_into_view_and_renders_the_right_affordance() {
        let mut driver = five_pinned_tabs();
        ctrl_tab(&mut driver); // #105 (last) → wraps to #101 (first)

        let text = strip_text(&driver);
        assert_eq!(
            active_tag(&driver),
            "#101",
            "contract §4: `Ctrl-Tab` wraps from the last tab to the first, so #101 is now \
             active. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            text.contains("#101"),
            "contract §4: \"the active tab's `#<N>` substring is always present in the strip \
             row while that tab is active, regardless of scroll offset\" — activating a \
             scrolled-out tab must scroll it back into view \
             (`fit_active_scroll_offset`), never leave it off-screen. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            text.contains('›'),
            "contract §4: with #101 active the strip shows #101–#104 and #105 is scrolled \
             out to the RIGHT, so the `›` affordance renders — the mirror of \
             `mocks/board-tabs-overflow.screen`'s `‹`, which §4 says a test-author should \
             drive rather than assert against that mock. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains('‹'),
            "contract §4: `‹` renders only when tabs exist before the leftmost visible one. \
             With the first tab (#101) active and visible, nothing is scrolled out to the \
             left, so `‹` must be absent — the exact mirror of the mock's `›`-absent case. \
             Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !text.contains("#105"),
            "contract §4: `›` must mean something — with #101 active and the strip \
             overflowing, the tabs beyond the rightmost visible one are scrolled out, so \
             the last tab (#105) is no longer in the strip. This is the exact mirror of \
             `mocks/board-tabs-overflow.screen`, where #105 is active and #101 is the one \
             scrolled away. Strip row was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        // TODO(test-author): the contract does not pin HOW MANY tabs fit the
        // strip — §4 defers that to `TabBar::fit_active_scroll_offset` and
        // explicitly declines to re-derive the math, and the two mocks
        // disagree on label width (see harness note 3), so
        // `mocks/board-tabs-overflow.screen`'s "only 4 fit" is not assertable
        // as a number. Only the direction of the scroll is asserted.
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4 — the empty state (closing the last tab)
    // ═══════════════════════════════════════════════════════════════════════

    /// §4: "Closing the **last** open tab returns to
    /// `mocks/board-baseline-no-tabs.screen`'s exact state — no strip row,
    /// sub-tab bar directly under the toolbar, and … the pane returns to
    /// selection-follows-tree."
    ///
    /// Asserted as: the strip's whole glyph vocabulary (`×`, §1's `∘`, §4's
    /// `‹`/`›`) is gone from the entire grid; the sub-tab bar is back on the
    /// row it occupied before any tab was ever opened (so the strip reserved
    /// no row, §2a); and the detail pane still renders the sidebar-selected
    /// issue, i.e. selection drives the pane again.
    ///
    /// TODO(test-author): the contract says "returns to
    /// `mocks/board-baseline-no-tabs.screen`'s **exact** state" but does not
    /// specify what the sidebar selection should be after the last tab closes.
    /// The baseline mock has `▸ #101` selected; this scenario necessarily
    /// arrives with #102 selected (opening its tab selected it, §2f). A
    /// literal full-frame comparison would therefore be red against any
    /// correct implementation, so the selection-follows-tree half is asserted
    /// as the *relation* (detail pane header matches whichever row is marked
    /// `▸`) rather than by pinning a row.
    #[test]
    fn closing_the_last_tab_restores_the_zero_tab_baseline() {
        let mut driver = board_driver();
        let subtab_before = subtab_row_index(&driver);

        click_row(&mut driver, ROW_102);
        assert_eq!(
            tab_count(&driver),
            1,
            "contract §2e rule 1 (a PRECONDITION, owned by #2282): a single click on #102 \
             opens exactly one preview tab, which is then the LAST open tab this test \
             closes.\n--- screen ---\n{}",
            driver.screen(),
        );

        ctrl_w(&mut driver);

        assert!(
            !driver.screen_contains("×"),
            "contract §4: closing the last open tab hides the strip entirely — no `×` close \
             glyph anywhere on the grid, exactly as \
             `mocks/board-baseline-no-tabs.screen`.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains("∘"),
            "contract §4 + §1: the closed tab was a preview tab, so its `∘` marker must go \
             with it — `mocks/board-baseline-no-tabs.screen` carries no `∘` \
             anywhere.\n--- screen ---\n{}",
            driver.screen(),
        );
        for arrow in ['‹', '›'] {
            assert!(
                !driver.screen_contains(&arrow.to_string()),
                "contract §4: with zero tabs there is nothing to scroll, so the `{arrow}` \
                 overflow affordance must not render — the strip is hidden, not merely \
                 emptied.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
        assert_eq!(
            subtab_row_index(&driver),
            subtab_before,
            "contract §4 + §2a: closing the last tab returns the panel to its zero-tab \
             layout — the strip \"renders nothing and reserves no row\", so the \
             `Board / Issue / Chat / Terminal` sub-tab bar must be back on the exact row it \
             occupied before any tab was opened.\n--- screen ---\n{}",
            driver.screen(),
        );

        // …and the pane is back to selection-follows-tree: whatever row the
        // sidebar marks selected (`▸`, §2f) is what the detail pane shows.
        let screen = driver.screen();
        let selected = screen
            .lines()
            .find_map(|r| r.split_once("▸ #").map(|(_, rest)| rest))
            .and_then(|rest| {
                let n: String = rest.chars().take_while(char::is_ascii_digit).collect();
                (!n.is_empty()).then_some(n)
            })
            .unwrap_or_else(|| {
                panic!(
                    "contract §4 + §2f: after the last tab closes the Board sidebar still \
                     marks a selected issue row with `▸`, which is what \
                     selection-follows-tree follows.\n--- screen ---\n{screen}"
                )
            });
        assert!(
            driver.screen_contains(&format!("claude-coordinator #{selected}")),
            "contract §4: closing the last tab returns the pane to selection-follows-tree — \
             sidebar selection alone drives the detail pane again, so the pane must show \
             the `claude-coordinator #{selected}` header for the `▸`-marked row (the shape \
             `mocks/board-baseline-no-tabs.screen` renders for its own selected \
             row).\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// CONTROL — green today and required to STAY green.
    ///
    /// §4's glyph vocabulary is strip-only: with zero document tabs open,
    /// neither the close glyph nor either overflow affordance may appear
    /// anywhere on the Board grid (`mocks/board-baseline-no-tabs.screen` has
    /// none of them). This is the regression bar for every close/overflow
    /// clause above — an implementation that unconditionally paints the strip
    /// chrome would satisfy the `‹`/`›` tests and break this one.
    ///
    /// Deliberately NOT listed in `manifest.yml`'s `expected_red` block: it
    /// passed in the authoring run and must keep passing.
    #[test]
    fn zero_tabs_render_no_close_or_overflow_glyphs() {
        let driver = board_driver();
        for (glyph, what) in [
            ('×', "§2d close glyph"),
            ('‹', "§4 left overflow affordance"),
            ('›', "§4 right overflow affordance"),
        ] {
            assert!(
                !driver.screen_contains(&glyph.to_string()),
                "contract §4 + §2a: with zero document tabs open the strip renders nothing \
                 at all, so the {what} `{glyph}` must not appear anywhere on the Board grid \
                 — `mocks/board-baseline-no-tabs.screen` carries \
                 none.\n--- screen ---\n{}",
                driver.screen(),
            );
        }
        assert!(
            strip(&driver).is_none(),
            "contract §4 + §2a: with zero document tabs open there is no tab-strip row at \
             all.\n--- screen ---\n{}",
            driver.screen(),
        );
    }
}
