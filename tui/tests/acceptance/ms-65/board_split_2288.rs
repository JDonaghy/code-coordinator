// Sealed acceptance slice for **issue #2288** — coord-tui tabs: side-by-side
// split, two tab groups in one panel (quadraui `SplitTree`) — milestone ms-65
// (tracking issue #2289, "coord-tui: per-panel document tabs (preview/pin)").
//
// Authored independently from `tests/acceptance/ms-65/contract.md` (Gate A) and
// its mocks, with **zero** worker/implementation context: no work branch, PR or
// commit for #2288 (or any other ms-65 issue) was read. Every assertion below
// is derived from contract §9, from `mocks/board-split-side-by-side.screen`
// (the one mock §11 indexes for this issue) and — for the one help-overlay
// clause §8b explicitly hands to §9 — from
// `mocks/board-tabs-help-overlay.screen`.
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-65 mock declares (contract §0).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2288 may run it
// (`coord acceptance run --issue 2288`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §9 only (plus §8b's Split section, see harness note 5). §2
// (#2282 Board strip), §3 (#2284 Pipeline's own set), §4 (#2283
// close/navigate/overflow), §5 (#2285 per-tab sub-state), §6 (#2286
// persistence) and §8a/§8c (#2287 hints + menu) are other issues' slices.
// §2 and §4 are *preconditions* of everything here — a split is only
// observable once panes hold tabs, and §9's collapse clause closes one — but
// this file never re-asserts one of their clauses as its own subject: every
// helper assertion that leans on them says so in its panic message.
//
// ── Harness facts and contract readings this slice had to design around ────
//
// 1. **Orientation is asserted against the rendered grid, never against an
//    enum name** — #2288's own ⚠ warning, and contract Note 1, both say a
//    wrong `SplitDirection` guess "compiles and type-checks". `divider_cells()`
//    below therefore proves the divider is *vertical* structurally: every `║`
//    on the grid must sit at ONE column, spanning ≥2 rows. A top/bottom split
//    that painted the same glyph horizontally would put several `║` on a
//    single row and fail with a message naming Note 1.
//
// 2. **`Ctrl-W v` is sent as the literal two-key chord** `ctrl_char('w')` then
//    `type_char('v')` — the only reading "`Ctrl-W v`" admits. See the
//    `TODO(test-author)` at the bottom: contract §4 also pins a BARE `Ctrl-W`
//    as "close the active tab", so §4 and §9's key table collide on the same
//    prefix. This slice does not resolve that collision (it cannot — a
//    test-author may not amend a contract); it sends the chord §9 pins and
//    lets the panic message say so. Flagged to the coordinator in
//    `manifest.yml`.
//
// 3. **Which pane holds focus after `Ctrl-W v` is not pinned in prose**, but
//    the mock settles it: `mocks/board-split-side-by-side.screen` shows the
//    LEFT pane holding two pinned tabs (`#101`, `[#102 …]`) and the RIGHT pane
//    holding the one preview (`[∘ #103 …]`). Per §2e rule 1 a single click
//    opens/replaces the preview of the *focused* group, so the post-split
//    single click landed in the new (right) pane — i.e. `Ctrl-W v` focuses the
//    pane it creates. Only ONE test (`the_split_renders_the_contracts_left_
//    and_right_pane_content`) leans on the left/right assignment; every other
//    test here is deliberately **side-agnostic**, asserting the partition
//    rather than which half is which, so a focus-order surprise reds one id
//    instead of nine.
//
// 4. **Targeting one tab out of several.** Every tab paints the same §2d `×`
//    close glyph, so `driver.find("×")` can only ever hit the leftmost one, and
//    `TuiDriver::tab_close_center()` needs the `WidgetId` the *implementation*
//    gives the doc-tab bar — an implementation detail a sealed slice must not
//    guess at. `close_glyph_pos()` resolves a target purely from the rendered
//    grid: locate the tab's `#<N>` tag in the strip row and take the first `×`
//    at or after it. Per contract §0 every glyph in play (`× ∘ ‹ › … ▸ │ ║`) is
//    one column wide, so a char index into a `screen()` row IS its cell column.
//
// 5. **§8b's "Split" section is asserted here, not in #2287's slice.** §8b
//    pins the `?` overlay as having two sections — "Document tabs" AND "Split
//    (§9's four keys)" — and `mocks/board-tabs-help-overlay.screen` renders
//    the four split rows. #2287's author deliberately left them out (their
//    slice's own closing TODO says so: the split keys are §9's, and #2287
//    precedes #2288 in the work order). Rather than let a pinned contract
//    clause go unasserted by anybody, the one id
//    `help_overlay_lists_the_split_key_bindings` is authored here — it is a
//    §9 fact rendered in a #2287 surface. Flagged in `manifest.yml`.

mod board_split_2288 {
    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::AppLogic;

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
    /// label is `#<N> <title>` with a *single* space, §2b/§9).
    const ROW_101: &str = "#101  Fix login race";
    const ROW_102: &str = "#102  Auth token refresh";
    const ROW_103: &str = "#103  Race condition in";

    /// §9's pinned pane divider: U+2551 DOUBLE VERTICAL, "reused from an
    /// existing mock's already-cleared glyph budget, ms-38 §7's list". One
    /// column wide (§0).
    ///
    /// Distinct from the sidebar/main separator `│` (U+2502) at column 2, which
    /// every ms-65 mock renders and which is NOT a pane divider.
    const DIVIDER: char = '║';

    /// Contract §0: "columns 38–119 = main panel content (82 cols)". The pane
    /// divider lives inside that span; anything a pane paints is at column ≥38.
    const MAIN_PANEL_FIRST_COL: usize = 38;

    // ═══════════════════════════════════════════════════════════════════════
    // Fixture + grid helpers
    //
    // Everything in this block is a *precondition* harness, not a #2288
    // clause: each panic message says so, so a failure here reads as a
    // fixture/§2/§4 finding rather than as a missing split behaviour.
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
        // wall-clock-dependent fold into a `DoubleClick`.
        driver.set_double_click_folding(false);
        driver.render();

        if !driver.screen_contains(ROW_101) {
            let before = driver.screen();
            let (x, y) = driver.find("No milestone (5)").unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2288 clause): the Board sidebar must render \
                     the seeded repo's collapsed \"No milestone (5)\" group header for \
                     contract §7's five-issue fixture — not found.\n--- screen ---\n{before}"
                )
            });
            driver.click(x, y);
            driver.render();
        }
        assert!(
            driver.screen_contains(ROW_101),
            "ms-65 baseline (NOT a #2288 clause): the Board sidebar must render a row for \
             contract §7's issue #101.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver
    }

    /// Screen rows, 0-indexed, as the grid the mocks are written in.
    fn rows<A: AppLogic>(driver: &TuiDriver<A>) -> Vec<String> {
        driver.screen().lines().map(str::to_string).collect()
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

    /// The document tab strip: the first row carrying the §2d close glyph `×`
    /// (U+00D7, `quadraui::tui::tab_bar::TAB_CLOSE_CHAR`).
    ///
    /// Unambiguous: the shipped Board screen contains no `×` anywhere — its
    /// `[P]urge` toolbar button uses `✕` (U+2715), a different code point —
    /// and neither does `mocks/board-baseline-no-tabs.screen`.
    ///
    /// In the split state this is ONE row carrying BOTH panes' strips either
    /// side of the divider — `mocks/board-split-side-by-side.screen` row 1.
    ///
    /// `None` when no strip is rendered (the zero-tab state, §2a).
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
                "contract §2a/§2d (a PRECONDITION of every §9 clause, owned by #2282): with \
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

    /// Index of the `Board / Issue / Chat / Terminal` sub-tab bar row
    /// (contract §2a — pre-dates this milestone, renders regardless of tab
    /// state). Located by its shipped labels rather than by §2a's literal
    /// `"[Board]"`, which is a mock artifact (quadraui's `draw_tab_bar` marks
    /// the active sub-tab with colour, not brackets) — the same resolution the
    /// #2282, #2283 and #2287 slices reached and flagged in `manifest.yml`.
    fn subtab_row_index<A: AppLogic>(driver: &TuiDriver<A>) -> usize {
        rows(driver)
            .iter()
            .position(|r| r.contains(" Board ") && r.contains(" Issue ") && r.contains(" Terminal"))
            .unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2288 clause): contract §2a pins the \
                     `Board / Issue / Chat / Terminal` sub-tab bar as always rendered on the \
                     Board panel — no row carries all of \" Board \", \" Issue \" and \
                     \" Terminal\".\n--- screen ---\n{}",
                    driver.screen()
                )
            })
    }

    /// Index of the panel toolbar row — §9: "the panel toolbar row still spans
    /// the full panel width above both panes". Located by two of the four
    /// buttons `panel_toolbar()` renders for the Board
    /// (`[ [A]dd ]  [ [N]otify ]  [ [R]etry ]  [ [P]urge ]`, contract §2a,
    /// "unchanged by this milestone").
    fn toolbar_row_index<A: AppLogic>(driver: &TuiDriver<A>) -> usize {
        rows(driver)
            .iter()
            .position(|r| r.contains("[A]dd") && r.contains("[P]urge"))
            .unwrap_or_else(|| {
                panic!(
                    "ms-65 baseline (NOT a #2288 clause): contract §2a pins the Board panel's \
                     toolbar row (`[ [A]dd ]  [ [N]otify ]  [ [R]etry ]  [ [P]urge ]`) as \
                     unchanged by this milestone — no row carries both \"[A]dd\" and \
                     \"[P]urge\".\n--- screen ---\n{}",
                    driver.screen()
                )
            })
    }

    // ── Gestures (§2e's, reused as preconditions) ──────────────────────────

    /// Single-click the sidebar row whose text starts with `row`. Per §2e rule
    /// 1 this opens (or replaces) the **focused tab group's** one preview tab —
    /// which under §9 is a *pane*, not a whole panel.
    fn click_row<A: AppLogic>(driver: &mut TuiDriver<A>, row: &str) {
        let before = driver.screen();
        let (x, y) = driver.find(row).unwrap_or_else(|| {
            panic!(
                "ms-65 baseline (NOT a #2288 clause): sidebar row {row:?} must be on screen \
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
                "ms-65 baseline (NOT a #2288 clause): sidebar row {row:?} must be on screen \
                 to double-click.\n--- screen ---\n{before}"
            )
        });
        driver.click(x, y);
        driver.double_click(x, y);
        driver.render();
    }

    // ── §9's pinned key chords (harness note 2) ────────────────────────────

    /// `Ctrl-W v` — "split the focused pane right" (§9's key table).
    fn split_right<A: AppLogic>(driver: &mut TuiDriver<A>) {
        driver.ctrl_char('w');
        driver.type_char('v');
        driver.render();
    }

    /// `Ctrl-W w` — "move focus to the next pane" (§9's key table).
    fn focus_next_pane<A: AppLogic>(driver: &mut TuiDriver<A>) {
        driver.ctrl_char('w');
        driver.type_char('w');
        driver.render();
    }

    /// `Ctrl-W x` — "close the focused pane (if it is not the last pane in the
    /// scope)" (§9's key table).
    fn close_pane<A: AppLogic>(driver: &mut TuiDriver<A>) {
        driver.ctrl_char('w');
        driver.type_char('x');
        driver.render();
    }

    // ── Divider / pane geometry (harness note 1) ───────────────────────────

    /// Every `(row, column)` on the grid carrying §9's `║` pane divider.
    fn divider_cells<A: AppLogic>(driver: &TuiDriver<A>) -> Vec<(usize, usize)> {
        rows(driver)
            .into_iter()
            .enumerate()
            .flat_map(|(y, row)| {
                row.chars()
                    .enumerate()
                    .filter(|(_, c)| *c == DIVIDER)
                    .map(|(x, _)| (y, x))
                    .collect::<Vec<_>>()
            })
            .collect()
    }

    /// The single column the pane divider occupies, having proved it really is
    /// a **vertical** divider: one column, ≥2 rows, strictly inside the main
    /// panel span (§0) so neither pane is degenerate.
    ///
    /// This is #2288's own ⚠ made executable (harness note 1 / contract
    /// Note 1): a `SplitDirection` mix-up that stacked the panes top/bottom
    /// would either paint no `║` at all or paint a horizontal run of them on
    /// one row, and both die here with a message naming the inversion.
    fn divider_col<A: AppLogic>(driver: &TuiDriver<A>) -> usize {
        let cells = divider_cells(driver);
        assert!(
            !cells.is_empty(),
            "contract §9: after splitting, the Board panel's `main_content_bounds` \"divides \
             into two panes separated by a `║` (U+2551, double vertical)\" — no `║` is on the \
             grid at all. NOTE (contract Note 1 / #2288's own ⚠): `SplitDirection` is INVERTED \
             between quadraui (`Horizontal` = panes side-by-side, first = left) and vimcode \
             (= top/bottom); a wrong guess compiles and type-checks. See \
             `mocks/board-split-side-by-side.screen`.\n--- screen ---\n{}",
            driver.screen(),
        );

        let mut cols: Vec<usize> = cells.iter().map(|(_, x)| *x).collect();
        cols.sort_unstable();
        cols.dedup();
        assert_eq!(
            cols.len(),
            1,
            "contract §9: the pane divider is a single VERTICAL `║` column — every `║` on the \
             grid must share one column. Found `║` at columns {cols:?}, i.e. a horizontal run, \
             which is what an inverted `SplitDirection` (contract Note 1) renders: the panes \
             stacked top/bottom instead of side by side. Assert orientation against the \
             rendered grid, never against the enum name.\n--- screen ---\n{}",
            driver.screen(),
        );
        let col = cols[0];

        let mut divider_rows: Vec<usize> = cells.iter().map(|(y, _)| *y).collect();
        divider_rows.sort_unstable();
        divider_rows.dedup();
        assert!(
            divider_rows.len() >= 2,
            "contract §9: the pane divider runs DOWN the panel — \
             `mocks/board-split-side-by-side.screen` paints `║` on every content row of the \
             split (its rows 1–5). A one-row `║` is not a side-by-side divider. Divider rows \
             were {divider_rows:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            col > MAIN_PANEL_FIRST_COL && col < 119,
            "contract §9 + §0: the divider splits the MAIN PANEL (columns 38–119) into two \
             non-degenerate panes — it must sit strictly inside that span, not at its edge \
             and not in the sidebar. `mocks/board-split-side-by-side.screen` puts it at column \
             78 (a 40-column left pane and a 41-column right one, §9's \"roughly 40 columns of \
             content width\"). Found column {col}.\n--- screen ---\n{}",
            driver.screen(),
        );
        col
    }

    /// Split `text` at the divider column into `(left pane, right pane)`, with
    /// the left half restricted to the main panel span (§0) so the sidebar's
    /// own content can never be mistaken for pane-1 content.
    fn panes_of(text: &str, divider: usize) -> (String, String) {
        let chars: Vec<char> = text.chars().collect();
        let left: String = chars
            .iter()
            .skip(MAIN_PANEL_FIRST_COL)
            .take(divider.saturating_sub(MAIN_PANEL_FIRST_COL))
            .collect();
        let right: String = chars.iter().skip(divider + 1).collect();
        (left, right)
    }

    /// `(left pane, right pane)` halves of the doc-tab strip row.
    ///
    /// Asserts §9's own structural claim on the way through: the strip row is
    /// one of the rows the divider crosses, because "each pane owns its own
    /// doc-tab strip" and both strips render on the same panel row
    /// (`mocks/board-split-side-by-side.screen` row 1).
    fn strip_panes<A: AppLogic>(driver: &TuiDriver<A>) -> (String, String) {
        let divider = divider_col(driver);
        let (y, text) = strip_row(driver);
        assert!(
            text.chars().nth(divider) == Some(DIVIDER),
            "contract §9: \"Each pane owns its own doc-tab strip\" — the divider must cross \
             the strip row, so both panes' strips render on the same panel row either side of \
             `║` (`mocks/board-split-side-by-side.screen` row 1). The strip row (row {y}) \
             carries no `║` at the divider column {divider}. Strip row was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        panes_of(&text, divider)
    }

    // ── Reading a pane's tab set off the grid ──────────────────────────────

    /// The `#<N>` tags rendered in `pane_text`, left to right.
    fn tags_in(pane_text: &str) -> Vec<String> {
        let chars: Vec<char> = pane_text.chars().collect();
        let mut tags = Vec::new();
        let mut i = 0;
        while i < chars.len() {
            if chars[i] == '#' {
                let tag: String = chars[i..]
                    .iter()
                    .take_while(|c| **c == '#' || c.is_ascii_digit())
                    .collect();
                i += tag.chars().count();
                if tag.chars().count() > 1 {
                    tags.push(tag);
                }
            } else {
                i += 1;
            }
        }
        tags
    }

    /// How many tabs `pane_text` shows, counted the way contract §2e mandates:
    /// `×` occurrences ("count the `×` occurrences … not tab colour/style").
    fn tab_count_in(pane_text: &str) -> usize {
        pane_text.matches('×').count()
    }

    /// The `#<N>` tag of the tab §2c brackets as active within `pane_text`, or
    /// `None` if no tab there is bracketed.
    ///
    /// Matches `[` only where a tab label can actually begin — `[#` (pinned) or
    /// `[∘` (§1's preview marker) — so an unrelated bracket painted in the row
    /// can never be mistaken for the active tab.
    fn active_tag_in(pane_text: &str) -> Option<String> {
        let chars: Vec<char> = pane_text.chars().collect();
        let open = (0..chars.len().saturating_sub(1))
            .find(|&i| chars[i] == '[' && (chars[i + 1] == '#' || chars[i + 1] == '∘'))?;
        let tag: String = chars[open + 1..]
            .iter()
            // Skip §1's `∘ ` preview marker, which sits between the bracket
            // and the `#<N>` tag on a preview tab.
            .skip_while(|&&c| c == '∘' || c == ' ')
            .take_while(|c| !c.is_whitespace())
            .collect();
        tag.starts_with('#').then_some(tag)
    }

    /// Click point on the `×` close glyph of the tab tagged `tag` — the first
    /// `×` at or after that tab's `#<N>` label (harness note 4).
    fn close_glyph_pos<A: AppLogic>(driver: &TuiDriver<A>, tag: &str) -> (f32, f32) {
        let (y, text) = strip_row(driver);
        let chars: Vec<char> = text.chars().collect();
        let start = col_of(&text, tag).unwrap_or_else(|| {
            panic!(
                "contract §2b/§9 (a PRECONDITION, owned by #2282): the tab strip must label \
                 each open tab with its `{tag}` issue tag so one tab out of several can be \
                 targeted. Strip row was {text:?}.\n--- screen ---\n{}",
                driver.screen()
            )
        });
        let x = (start..chars.len())
            .find(|&i| chars[i] == '×')
            .unwrap_or_else(|| {
                panic!(
                    "contract §2d (a PRECONDITION, owned by #2282): every open closable tab \
                     renders a trailing `×`; none follows {tag:?}. Strip row was \
                     {text:?}.\n--- screen ---\n{}",
                    driver.screen()
                )
            });
        (x as f32 + 0.5, y as f32 + 0.5)
    }

    // ── Composite fixtures ─────────────────────────────────────────────────

    /// Two pinned Board tabs, `#101` and `#102`, with `#102` active — the
    /// left-pane state `mocks/board-split-side-by-side.screen` depicts, before
    /// the split.
    ///
    /// Two separate double clicks, each landing while no preview tab is open,
    /// is the sequence §2e traces to a pair of pinned tabs.
    fn two_pinned_tabs() -> TuiDriver<impl AppLogic> {
        let mut driver = board_driver();
        dbl_click_row(&mut driver, ROW_101);
        dbl_click_row(&mut driver, ROW_102);
        assert_eq!(
            tab_count_in(&strip_text(&driver)),
            2,
            "contract §2e (a PRECONDITION of every §9 clause, owned by #2282): two double \
             clicks on #101/#102 must leave two pinned tabs open. Strip row was \
             {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
        driver
    }

    /// The exact state `mocks/board-split-side-by-side.screen` renders: two
    /// pinned tabs, split right, then a single click on `#103` opening the new
    /// pane's preview tab (harness note 3).
    fn split_board() -> TuiDriver<impl AppLogic> {
        let mut driver = two_pinned_tabs();
        split_right(&mut driver);
        click_row(&mut driver, ROW_103);
        driver
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §9 — the split itself
    // ═══════════════════════════════════════════════════════════════════════

    /// §9 + #2288 AC 1: "Splitting the Board panel yields two panes side by
    /// side, each with its own tab strip, proven by the rendered screen."
    ///
    /// The orientation half is the point (contract Note 1, #2288's own ⚠):
    /// `divider_col()` proves the `║` run is one column deep-spanning rows, not
    /// one row spanning columns, and that it sits strictly inside the main
    /// panel so both panes are real. Then both halves of the strip row must
    /// carry ink — panes side by side, not one pane and a margin.
    #[test]
    fn ctrl_w_v_splits_the_board_panel_into_two_panes_side_by_side() {
        let driver = split_board();

        let divider = divider_col(&driver);
        let (left, right) = strip_panes(&driver);
        assert!(
            !left.trim().is_empty(),
            "contract §9: the pane LEFT of the `║` divider (column {divider}) must render its \
             own content — `mocks/board-split-side-by-side.screen` row 1 shows \
             `#101 Fix logi… ×  [#102 Auth tok… ×]` there. Left pane was \
             {left:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !right.trim().is_empty(),
            "contract §9: the pane RIGHT of the `║` divider (column {divider}) must render its \
             own content — `mocks/board-split-side-by-side.screen` row 1 shows \
             `[∘ #103 Race con… ×]` there. An empty right half means the panel reserved a \
             divider without ever laying out a second pane. Right pane was \
             {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §9 + #2288 AC 1's second half: "each with its own tab strip".
    ///
    /// Asserted as the §2d close glyph appearing on BOTH sides of the divider
    /// on the strip row: a single shared strip painted across the whole panel
    /// (the obvious wrong implementation of "split the detail area only") puts
    /// every `×` on one side and fails here.
    #[test]
    fn each_split_pane_renders_its_own_document_tab_strip() {
        let driver = split_board();
        let (left, right) = strip_panes(&driver);

        assert!(
            tab_count_in(&left) >= 1,
            "contract §9: \"Each pane owns its own doc-tab strip, active tab and preview \
             slot\" — the LEFT pane must render its own tabs (each carrying §2d's `×`). Left \
             pane of the strip row was {left:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            tab_count_in(&right) >= 1,
            "contract §9: \"Each pane owns its own doc-tab strip, active tab and preview \
             slot\" — the RIGHT pane must render its own tabs too. A strip that stays \
             panel-wide (one tab group spanning both panes) fails exactly here. Right pane of \
             the strip row was {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §9: "T-3's per-scope model becomes per-pane-within-scope … the detail
    /// renderer must accept a narrower rect."
    /// `mocks/board-split-side-by-side.screen` row 2 renders the
    /// `Board / Issue / Chat / Terminal` sub-tab bar **twice**, once per pane,
    /// either side of the same `║`.
    ///
    /// Located by the shipped sub-tab labels rather than §2a's literal
    /// `"[Board]"` (a mock artifact — see the coordinator findings in
    /// `manifest.yml`), then checked on both sides of the divider.
    #[test]
    fn each_split_pane_renders_its_own_sub_tab_bar() {
        let driver = split_board();
        let divider = divider_col(&driver);
        let y = subtab_row_index(&driver);
        let text = rows(&driver)[y].clone();

        assert!(
            text.chars().nth(divider) == Some(DIVIDER),
            "contract §9: everything below the panel toolbar is duplicated per pane, so the \
             divider must cross the sub-tab bar row too — \
             `mocks/board-split-side-by-side.screen` row 2 renders the bar on both sides of \
             `║`. Sub-tab bar row (row {y}) was {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        let (left, right) = panes_of(&text, divider);
        for (label, pane, text_of) in [("LEFT", "left", &left), ("RIGHT", "right", &right)] {
            let _ = pane;
            for needle in ["Board", "Issue"] {
                assert!(
                    text_of.contains(needle),
                    "contract §9: each pane renders its OWN \
                     `Board / Issue / Chat / Terminal` sub-tab bar in its narrower rect \
                     (`mocks/board-split-side-by-side.screen` row 2) — the {label} pane's half \
                     of that row must contain {needle:?}. That half was \
                     {text_of:?}.\n--- screen ---\n{}",
                    driver.screen(),
                );
            }
        }
    }

    /// §9: "**the panel toolbar row still spans the full panel width above both
    /// panes** (toolbar is panel-scoped, not pane-scoped — only the doc-tab
    /// strip and everything below it is duplicated per pane)."
    ///
    /// `mocks/board-split-side-by-side.screen` row 0 is the toolbar and carries
    /// no `║`; the divider starts on row 1, the strip row.
    #[test]
    fn the_panel_toolbar_spans_the_full_width_above_both_panes() {
        let driver = split_board();
        let divider = divider_col(&driver);
        let toolbar = toolbar_row_index(&driver);
        let text = rows(&driver)[toolbar].clone();

        assert!(
            !text.contains(DIVIDER),
            "contract §9: the panel toolbar is PANEL-scoped, not pane-scoped — its row \"still \
             spans the full panel width above both panes\", so no `║` may cross it \
             (`mocks/board-split-side-by-side.screen` row 0). Toolbar row (row {toolbar}) was \
             {text:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let first_divider_row = divider_cells(&driver)
            .into_iter()
            .map(|(y, _)| y)
            .min()
            .expect("divider_col above proved at least one `║` exists");
        assert!(
            toolbar < first_divider_row,
            "contract §9: the toolbar sits ABOVE both panes — the divider (column {divider}) \
             must start on a row below it. Toolbar row {toolbar}, first divider row \
             {first_divider_row}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §9's independence clause: "The two panes' tab sets, active tabs and
    /// preview slots never merge — same independence proof pattern as §3,
    /// applied within one panel instead of across two."
    ///
    /// Deliberately **side-agnostic** (harness note 3): it asserts the
    /// partition, not which half is which. Three separate merges are ruled out
    /// — a document open in both panes, a second bracketed active tab in one
    /// pane, and a preview marker in both panes (§9: each pane owns "its own
    /// … preview slot", and §2e rule 4 caps a tab group at one preview).
    #[test]
    fn the_two_panes_tab_sets_never_merge() {
        let driver = split_board();
        let (left, right) = strip_panes(&driver);
        let left_tags = tags_in(&left);
        let right_tags = tags_in(&right);

        assert!(
            !left_tags.is_empty() && !right_tags.is_empty(),
            "contract §9: both panes hold their own tab set — neither may be empty in the \
             state `mocks/board-split-side-by-side.screen` depicts. Left {left_tags:?}, right \
             {right_tags:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        for tag in &left_tags {
            assert!(
                !right_tags.contains(tag),
                "contract §9: the two panes' TAB SETS never merge — {tag:?} is rendered in \
                 both panes. Left {left_tags:?}, right {right_tags:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }

        for (label, pane) in [("LEFT", &left), ("RIGHT", &right)] {
            assert!(
                active_tag_in(pane).is_some(),
                "contract §9 + §2c: each pane owns its own ACTIVE TAB, so each pane's strip \
                 brackets exactly one of its own tabs `[<label> ×]` \
                 (`mocks/board-split-side-by-side.screen` row 1 brackets `#102` on the left \
                 and `#103` on the right). The {label} pane brackets none: \
                 {pane:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
        }

        let previews = left.matches('∘').count() + right.matches('∘').count();
        assert_eq!(
            previews, 1,
            "contract §9 + §1/§2e rule 4: each pane owns its own PREVIEW SLOT and a tab group \
             holds at most one preview, so after two pinned tabs plus a single click there is \
             exactly one `∘ ` marker across the whole strip. Found {previews}. Left {left:?}, \
             right {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §9's own worked example, quoted verbatim from the contract:
    /// "`mocks/board-split-side-by-side.screen` row 2 (the doc-tab strip row)
    /// is the truthful rendering of this budget: left pane
    /// `\"#101 Fix logi… ×  [#102 Auth tok… ×]\"` … and right pane
    /// `\"[∘ #103 Race con… ×]\"`."
    ///
    /// This is the ONE test here that pins which side is which (harness note
    /// 3). The mock's assignment is what makes it derivable: §2e rule 1 opens
    /// the preview in the *focused* group, and the post-split single click's
    /// preview lands on the RIGHT — i.e. `Ctrl-W v` focuses the pane it
    /// creates, and quadraui's `Horizontal` (first = left, contract Note 1)
    /// keeps the original pane on the left.
    ///
    /// TODO(test-author): §9's prose never states which pane holds focus after
    /// `Ctrl-W v`; only the mock's left/right content implies it. If the
    /// intended behaviour is "focus stays in the original pane", this id is the
    /// only one in the slice that would be wrongly red — the rest are
    /// side-agnostic on purpose. Flagged to the coordinator in `manifest.yml`.
    #[test]
    fn the_split_renders_the_contracts_left_and_right_pane_content() {
        let driver = split_board();
        let (left, right) = strip_panes(&driver);

        assert_eq!(
            tags_in(&left),
            vec!["#101".to_string(), "#102".to_string()],
            "contract §9: the LEFT pane keeps the tabs the split was performed from — \
             `mocks/board-split-side-by-side.screen` row 1 renders \
             `#101 Fix logi… ×  [#102 Auth tok… ×]` there (quadraui's `Horizontal` puts the \
             first pane on the left, contract Note 1). Left pane was \
             {left:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            tags_in(&right),
            vec!["#103".to_string()],
            "contract §9: the RIGHT pane is the one `Ctrl-W v` created, and the single click \
             that followed opened ITS preview — \
             `mocks/board-split-side-by-side.screen` row 1 renders `[∘ #103 Race con… ×]` \
             there and nothing else. Right pane was {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            active_tag_in(&left).as_deref(),
            Some("#102"),
            "contract §9 + §2c: the left pane's own active tab is the last one pinned there \
             (#102), bracketed `[#102 Auth tok… ×]` in the mock — the split must not move or \
             clear it. Left pane was {left:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            active_tag_in(&right).as_deref(),
            Some("#103"),
            "contract §9 + §2c: the right pane's own active tab is its single preview, \
             bracketed `[∘ #103 Race con… ×]` in the mock. Right pane was \
             {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §9 — the split-pane label budget (14 / 16 columns)
    // ═══════════════════════════════════════════════════════════════════════

    /// §9: "**Split-pane tab label truncation is a separate, narrower, pinned
    /// constant: 14 columns** … not the §2b single-pane 20/22 budget."
    ///
    /// `#101 Fix login race timeout` truncated to 14 inclusive of the trailing
    /// `…` is `#101 Fix logi…`, exactly as the mock renders it; the §2b
    /// single-pane form of the same tab is `#101 Fix login race…` (20). Both
    /// are asserted — the presence of the narrow one and the absence of the
    /// wide one — so an implementation that splits the layout but keeps the
    /// panel-wide budget fails here rather than silently overflowing.
    #[test]
    fn split_pane_tab_labels_truncate_at_fourteen_columns() {
        let driver = split_board();
        // Reading the panes first also asserts the split happened at all.
        let (left, _right) = strip_panes(&driver);

        assert!(
            left.contains("#101 Fix logi…"),
            "contract §9: a split pane's tab label is truncated to 14 columns inclusive of the \
             `#<N> ` prefix, with a trailing `…` — `#101 Fix login race timeout` renders as \
             `#101 Fix logi…` (`mocks/board-split-side-by-side.screen` row 1). Left pane was \
             {left:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            left.contains("#102 Auth tok…"),
            "contract §9: the same 14-column budget applies to every tab in the pane — \
             `#102 Auth token refresh bug` renders as `#102 Auth tok…`. Left pane was \
             {left:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains("#101 Fix login race…"),
            "contract §9: the split-pane budget is 14 columns, NOT §2b's single-pane 20 — the \
             wide form `#101 Fix login race…` must not survive the split (\"This is not \
             derived by halving 20/22 … it is its own pinned value\").\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §9: "14 columns (**16 for a preview tab**, i.e. the same `+2` marker
    /// rule as §2b, just applied to the smaller base)".
    ///
    /// `∘ #103 Race condition in poller` truncated to 16 inclusive of §1's
    /// `∘ ` marker is `∘ #103 Race con…`, exactly the mock's right pane; the
    /// §2b single-pane form is `∘ #103 Race condition…` (22).
    #[test]
    fn split_pane_preview_tab_labels_truncate_at_sixteen_columns() {
        let driver = split_board();
        let (_left, right) = strip_panes(&driver);

        assert!(
            right.contains("∘ #103 Race con…"),
            "contract §9 + §1: a split pane's PREVIEW tab label gets the same +2 marker \
             allowance on the narrower base — 16 columns total, so \
             `∘ #103 Race condition in poller` renders as `∘ #103 Race con…` \
             (`mocks/board-split-side-by-side.screen` row 1, right pane). The `∘ ` marker is \
             never itself truncated away. Right pane was {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !driver.screen_contains("∘ #103 Race condition…"),
            "contract §9: the split-pane preview budget is 16 columns, NOT §2b's single-pane \
             22 — the wide form `∘ #103 Race condition…` must not survive the \
             split.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §9 — per-pane preview slot and focus movement
    // ═══════════════════════════════════════════════════════════════════════

    /// #2288 AC 2: "Each pane has an independent active tab and an independent
    /// preview slot: a single click while pane 2 is focused replaces pane 2's
    /// preview and leaves pane 1's alone." Contract §9: "Each pane owns its own
    /// doc-tab strip, active tab and preview slot."
    ///
    /// Both panes hold a PREVIEW here — the state that makes the clause
    /// falsifiable, since a single shared preview slot can only hold one of
    /// them. Pane 1 previews `#101`; after the split, two consecutive single
    /// clicks in the new pane must exercise §2e rule 1's *replace* branch there
    /// (`#102` evicted by `#103`) while `#101` sits untouched in the other
    /// pane.
    ///
    /// Side-agnostic (harness note 3): asserts the partition, not which half.
    #[test]
    fn each_pane_owns_its_own_preview_slot() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_101);
        assert_eq!(
            tab_count_in(&strip_text(&driver)),
            1,
            "contract §2e rule 1 (a PRECONDITION, owned by #2282): a single click on #101 \
             opens exactly one preview tab. Strip row was {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );

        split_right(&mut driver);
        click_row(&mut driver, ROW_102);
        click_row(&mut driver, ROW_103);

        let (left, right) = strip_panes(&driver);
        let total = tab_count_in(&left) + tab_count_in(&right);
        assert_eq!(
            total, 2,
            "contract §9 + §2e rule 4: each pane caps its own preview slot at one tab, so two \
             single clicks in the focused pane leave ONE preview there (the second replaced \
             the first, rule 1's replace branch) plus the other pane's untouched one — two \
             tabs across the panel. Found {total}. Left {left:?}, right \
             {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let mut all: Vec<String> = tags_in(&left);
        all.extend(tags_in(&right));
        all.sort();
        assert_eq!(
            all,
            vec!["#101".to_string(), "#103".to_string()],
            "contract §9 + #2288 AC 2: the click in the focused pane \"replaces pane 2's \
             preview and leaves pane 1's alone\" — #102 must be evicted from the focused \
             pane's own preview slot and #101 must survive in the other pane. A SHARED \
             preview slot loses #101 instead. Left {left:?}, right \
             {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !tags_in(&left).is_empty() && !tags_in(&right).is_empty(),
            "contract §9: the two surviving previews sit in DIFFERENT panes — one each side of \
             the `║`. Left {left:?}, right {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §9's key table: "`Ctrl-W w` — move focus to the next pane."
    ///
    /// Focus has no direct glyph in a symbols-only grid, so it is read through
    /// its one pinned consequence: §2e rule 1 opens/replaces the preview of the
    /// **focused** tab group. Each pane starts with exactly one preview
    /// (`#101` and `#103`); after `Ctrl-W w` a single click on `#102` must
    /// replace the *other* pane's preview than the one the previous click hit.
    ///
    /// If `Ctrl-W w` did nothing, `#102` would replace `#103` and `#101` would
    /// survive — which the `#101`-is-gone assertion below rules out.
    /// Side-agnostic (harness note 3).
    #[test]
    fn ctrl_w_w_moves_focus_to_the_other_pane() {
        let mut driver = board_driver();
        click_row(&mut driver, ROW_101);
        split_right(&mut driver);
        click_row(&mut driver, ROW_103);

        let (left_before, right_before) = strip_panes(&driver);
        let mut before: Vec<String> = tags_in(&left_before);
        before.extend(tags_in(&right_before));
        before.sort();
        assert_eq!(
            before,
            vec!["#101".to_string(), "#103".to_string()],
            "contract §9 (a PRECONDITION of the focus clause, asserted on its own in \
             `each_pane_owns_its_own_preview_slot`): each pane must hold its own preview — \
             #101 in the pane the split came from, #103 in the new one — before a focus move \
             can be observed. Left {left_before:?}, right \
             {right_before:?}.\n--- screen ---\n{}",
            driver.screen(),
        );

        focus_next_pane(&mut driver);
        click_row(&mut driver, ROW_102);

        let (left, right) = strip_panes(&driver);
        let mut after: Vec<String> = tags_in(&left);
        after.extend(tags_in(&right));
        after.sort();
        assert_eq!(
            after,
            vec!["#102".to_string(), "#103".to_string()],
            "contract §9 key table: `Ctrl-W w` moves focus to the NEXT pane, so the single \
             click that follows lands in the pane holding #101 and replaces its preview with \
             #102 (§2e rule 1) — #103's pane must be untouched. If focus had not moved, #102 \
             would have replaced #103 and #101 would still be on screen. Left {left:?}, right \
             {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            !tags_in(&left).is_empty() && !tags_in(&right).is_empty(),
            "contract §9: after the focus move each pane still holds exactly its own one \
             preview — the two must not collapse into a single pane's set. Left {left:?}, \
             right {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §9 — divider drag
    // ═══════════════════════════════════════════════════════════════════════

    /// #2288 AC 3 / §9's Design bullet: "Dragging the divider resizes both
    /// panes and the content reflows."
    ///
    /// Dragged 10 columns left from the divider's own cell. The contract pins
    /// no resize arithmetic (no minimum pane width, no snapping, no
    /// proportional-vs-absolute rule), so this asserts only what §9 does say:
    /// the divider moves in the drag's direction, it is still a single vertical
    /// divider afterwards, and BOTH panes still render their own tab strip —
    /// i.e. the resize reflowed the content instead of clipping a pane away.
    ///
    /// TODO(test-author): §9 does not pin how far the divider travels for a
    /// given drag, so the exact landing column is not asserted. If a specific
    /// mapping (1 cell per column, a minimum pane width, snap-to-half) is
    /// intended, it needs a contract amendment.
    #[test]
    fn dragging_the_divider_resizes_both_panes_and_the_content_reflows() {
        let mut driver = split_board();
        let before = divider_col(&driver);
        let grab_row = divider_cells(&driver)
            .into_iter()
            .map(|(y, _)| y)
            .max()
            .expect("divider_col above proved at least one `║` exists");

        let target = before - 10;
        driver.drag(
            before as f32 + 0.5,
            grab_row as f32 + 0.5,
            target as f32 + 0.5,
            grab_row as f32 + 0.5,
        );
        driver.render();

        let after = divider_col(&driver);
        assert!(
            after < before,
            "contract §9 / #2288 AC 3: dragging the `║` divider resizes both panes — a drag \
             from column {before} to column {target} must move it LEFT (making the first pane \
             narrower and the second wider). It is still at column {after}.\n--- screen ---\n{}",
            driver.screen(),
        );

        let (left, right) = strip_panes(&driver);
        assert!(
            tab_count_in(&left) >= 1 && tab_count_in(&right) >= 1,
            "contract §9 / #2288 AC 3: after the resize the content REFLOWS into the new pane \
             widths — both panes must still render their own doc-tab strip. Left {left:?}, \
             right {right:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §9 — collapsing back to one pane
    // ═══════════════════════════════════════════════════════════════════════

    /// #2288 AC 4: "Closing the last tab in one pane collapses that pane back
    /// to a single-pane layout."
    ///
    /// The right pane holds exactly one tab in `mocks/board-split-side-by-side.screen`,
    /// so clicking its `×` (§4's shipped close route, #2283) empties that pane.
    /// §9's own invariant — "a scope always has ≥1 pane" — plus AC 4 mean the
    /// emptied pane goes away entirely rather than lingering as a blank half:
    /// no `║` may remain anywhere.
    #[test]
    fn closing_the_last_tab_in_a_pane_collapses_back_to_a_single_pane() {
        let mut driver = split_board();
        let (left_before, right_before) = strip_panes(&driver);
        let (only_tag, survivors) = if tags_in(&right_before).len() == 1 {
            (tags_in(&right_before)[0].clone(), tags_in(&left_before))
        } else {
            assert_eq!(
                tags_in(&left_before).len(),
                1,
                "contract §9 (a PRECONDITION, asserted on its own in \
                 `the_two_panes_tab_sets_never_merge`): exactly one of the two panes must hold \
                 a single tab for \"closing the last tab in one pane\" to be observable. Left \
                 {left_before:?}, right {right_before:?}.\n--- screen ---\n{}",
                driver.screen(),
            );
            (tags_in(&left_before)[0].clone(), tags_in(&right_before))
        };

        let (x, y) = close_glyph_pos(&driver, &only_tag);
        driver.click(x, y);
        driver.render();

        assert!(
            !driver.screen_contains("║"),
            "contract §9 + #2288 AC 4: \"Closing the last tab in one pane collapses that pane \
             back to a single-pane layout\" — after closing {only_tag}, the pane that held it \
             is gone and no `║` divider remains on the grid (§9: `screen_contains(\"║\")` is \
             true \"only when a panel has ≥2 panes\").\n--- screen ---\n{}",
            driver.screen(),
        );
        let remaining = tags_in(&strip_text(&driver));
        assert_eq!(
            remaining, survivors,
            "contract §9 + #2288 AC 4: collapsing the emptied pane must not disturb the other \
             pane's tab set — it becomes the single pane's set, in the same order. Expected \
             {survivors:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §9's key table: "`Ctrl-W x` — close the focused pane (if it is not the
    /// last pane in the scope)."
    ///
    /// Side-agnostic (harness note 3): §9 pins that a pane closes, not which
    /// one holds focus, so the surviving strip must be exactly ONE of the two
    /// panes' sets — whole and in order, never a merge of both.
    #[test]
    fn ctrl_w_x_closes_the_focused_pane() {
        let mut driver = split_board();
        let (left_before, right_before) = strip_panes(&driver);
        let left_tags = tags_in(&left_before);
        let right_tags = tags_in(&right_before);

        close_pane(&mut driver);

        assert!(
            !driver.screen_contains("║"),
            "contract §9 key table: `Ctrl-W x` closes the focused pane, leaving one pane — so \
             no `║` divider remains (§9: the glyph renders \"only when a panel has ≥2 \
             panes\").\n--- screen ---\n{}",
            driver.screen(),
        );
        let remaining = tags_in(&strip_text(&driver));
        assert!(
            remaining == left_tags || remaining == right_tags,
            "contract §9 key table + §9's independence clause: closing one pane leaves the \
             OTHER pane's tab set intact and untouched — the surviving strip must be exactly \
             {left_tags:?} or exactly {right_tags:?}, never a merge of the two or a \
             re-ordering. Found {remaining:?}.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// §9: "Closing the last remaining pane in a scope is a no-op (or disabled)
    /// — a scope always has ≥1 pane."
    ///
    /// Driven with two open tabs so a no-op is distinguishable from "it closed
    /// something": the strip must read exactly as it did before the chord, and
    /// no divider may appear.
    ///
    /// TODO(test-author): contract §4 pins a BARE `Ctrl-W` as "close the active
    /// tab" while §9 pins `Ctrl-W x` as a pane chord on the same prefix. This
    /// test asserts §9's clause as written — an implementation that treats the
    /// chord's first key as §4's close and then drops the `x` fails here. The
    /// collision is a contract-level question (flagged to the coordinator in
    /// `manifest.yml`), not something a test-author may resolve by picking one.
    #[test]
    fn closing_the_last_remaining_pane_is_a_no_op() {
        let mut driver = two_pinned_tabs();
        assert!(
            !driver.screen_contains("║"),
            "contract §9 (a PRECONDITION): one pane is the default, so a freshly seeded Board \
             renders no `║` divider before any split.\n--- screen ---\n{}",
            driver.screen(),
        );
        let before = strip_text(&driver);
        let tags_before = tags_in(&before);

        close_pane(&mut driver);

        assert!(
            !driver.screen_contains("║"),
            "contract §9: \"a scope always has ≥1 pane\" — `Ctrl-W x` on the last pane must \
             not split, spawn or otherwise produce a divider.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            tags_in(&strip_text(&driver)),
            tags_before,
            "contract §9: \"Closing the last remaining pane in a scope is a no-op (or \
             disabled)\" — the pane's tab set must be exactly as it was. Strip row was \
             {before:?} before the chord and {:?} after.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );
    }

    /// §9 + #2288 AC 5: "With one pane, rendering is **byte-identical** to the
    /// non-split case … there is no separate \"single-pane-but-split-capable\"
    /// visual state."
    ///
    /// The round trip is what makes this falsifiable: an implementation that
    /// reserves a divider column, a pane gutter or a narrower label budget the
    /// moment the panel becomes split-capable passes the never-split case
    /// trivially and fails here. The split is asserted to have happened first,
    /// so this cannot pass vacuously on an app where `Ctrl-W v` does nothing.
    ///
    /// TODO(test-author): §9 states the byte-identity for "one pane" without
    /// saying whether a pane reached by *collapsing* a split must be
    /// byte-identical to one that was never split. This test takes the reading
    /// §9's own "there is no separate single-pane-but-split-capable visual
    /// state" implies — there is only one single-pane rendering, however it was
    /// reached. If restoring focus/scroll differently after a collapse is
    /// intended, that needs a contract amendment.
    #[test]
    fn collapsing_a_split_restores_the_unsplit_rendering_byte_for_byte() {
        let mut driver = two_pinned_tabs();
        let unsplit = driver.screen();

        split_right(&mut driver);
        assert!(
            driver.screen_contains("║"),
            "contract §9 key table: `Ctrl-W v` must actually split the focused pane before a \
             collapse can be observed — no `║` divider appeared (asserted on its own in \
             `ctrl_w_v_splits_the_board_panel_into_two_panes_side_by_side`).\n--- screen ---\n{}",
            driver.screen(),
        );

        close_pane(&mut driver);
        let collapsed = driver.screen();
        assert_eq!(
            collapsed, unsplit,
            "contract §9 + #2288 AC 5: \"With one pane, rendering is BYTE-IDENTICAL to the \
             non-split case\" — after splitting and closing the new pane the grid must match \
             the pre-split frame exactly, with no reserved divider column, pane gutter or \
             narrowed label budget left behind.\n--- before split ---\n{unsplit}\n--- after \
             collapse ---\n{collapsed}",
        );
    }

    /// CONTROL — green today and required to STAY green.
    ///
    /// §9: "`driver.screen_contains(\"║\")` is `true` only when a panel has ≥2
    /// panes; `false` on every mock in §§2–4 and §8 (single pane, the default)"
    /// and "One pane is the default".
    ///
    /// This is the regression bar for every clause above: an implementation
    /// that renders the `SplitTree`'s divider unconditionally — or that starts
    /// the Board with two leaves because "the tree shape allows more later" —
    /// satisfies the split tests and breaks exactly this one. Driven with three
    /// pinned tabs so it covers the state
    /// `mocks/board-pinned-3-tabs.screen` depicts, which §9 names as "exactly
    /// what one pane of a would-be split looks like".
    ///
    /// Deliberately NOT listed in `manifest.yml`'s `expected_red` block: it
    /// passes today and must keep passing.
    #[test]
    fn a_single_pane_panel_renders_no_divider() {
        let mut driver = two_pinned_tabs();
        dbl_click_row(&mut driver, ROW_103);
        assert_eq!(
            tab_count_in(&strip_text(&driver)),
            3,
            "contract §2e (a PRECONDITION, owned by #2282): three double clicks leave three \
             pinned tabs — `mocks/board-pinned-3-tabs.screen`. Strip row was \
             {:?}.\n--- screen ---\n{}",
            strip_text(&driver),
            driver.screen(),
        );

        assert!(
            !driver.screen_contains("║"),
            "contract §9: \"One pane is the default\" and `screen_contains(\"║\")` is true \
             \"only when a panel has ≥2 panes\" — no `║` may appear anywhere on an unsplit \
             Board, on any of §§2–4/§8's mocks. `mocks/board-pinned-3-tabs.screen` is \
             \"exactly what one pane of a would-be split looks like; there is no separate \
             'single-pane-but-split-capable' visual state\".\n--- screen ---\n{}",
            driver.screen(),
        );
        assert_eq!(
            divider_cells(&driver).len(),
            0,
            "contract §9: not one `║` cell may be painted while the panel has a single \
             pane.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §8b's "Split" section — a §9 fact rendered in #2287's surface
    // (harness note 5)
    // ═══════════════════════════════════════════════════════════════════════

    /// §8b: the `?` help overlay has "two sections: **Document tabs** … and
    /// **Split** (§9's four keys)", and
    /// `mocks/board-tabs-help-overlay.screen` renders them as
    /// `Ctrl-W v  split right`, `Ctrl-W s  split down`, `Ctrl-W w  next pane`,
    /// `Ctrl-W x  close pane` under a `Split` heading.
    ///
    /// Authored in THIS slice rather than #2287's because the four keys are
    /// §9's — #2287's own slice defers them explicitly (harness note 5). The
    /// overlay chrome itself is #2287's to build; what is asserted here is only
    /// that §9's key table is discoverable in it.
    ///
    /// Note this asserts the `Ctrl-W s` ROW, not that the chord does anything:
    /// §9 Note 2 pins `Ctrl-W s` as a reserved forward-compatibility
    /// placeholder that ms-65 does not ship, and "no acceptance test in this
    /// contract requires `Ctrl-W s` to do anything in ms-65". Its help row is a
    /// rendering fact, not a behaviour.
    #[test]
    fn help_overlay_lists_the_split_key_bindings() {
        let mut driver = board_driver();
        driver.type_char('?');
        driver.render();
        assert!(
            driver.screen_contains("Board — Help"),
            "contract §8b (a PRECONDITION, owned by #2287): pressing `?` on the Board panel \
             must open the \"Board — Help\" overlay before its Split section can be \
             read.\n--- screen ---\n{}",
            driver.screen(),
        );

        for phrase in [
            "Split",
            "Ctrl-W v",
            "split right",
            "Ctrl-W s",
            "split down",
            "Ctrl-W w",
            "next pane",
            "Ctrl-W x",
            "close pane",
        ] {
            assert!(
                driver.screen_contains(phrase),
                "contract §8b + §9: the `?` overlay's second section lists §9's four pane keys \
                 — {phrase:?} must be on screen while the overlay is open. See \
                 `mocks/board-tabs-help-overlay.screen` rows 11–15 (`Split`, `Ctrl-W v  split \
                 right`, `Ctrl-W s  split down`, `Ctrl-W w  next pane`, `Ctrl-W x  close \
                 pane`).\n--- screen ---\n{}",
                driver.screen(),
            );
        }
    }

    // ───────────────────────────────────────────────────────────────────────
    // NOT AUTHORED — deliberately, rather than guessed.
    //
    // TODO(test-author): `Ctrl-W s` ("split the focused pane down") has NO
    // behavioural test. §9's own Note 2 forbids one: "ms-65 ships side-by-side
    // (`v`) only … no acceptance test in this contract requires `Ctrl-W s` to
    // do anything in ms-65." Only its help-overlay row is asserted, above.
    //
    // TODO(test-author): contract §4 pins a BARE `Ctrl-W` as "closes the active
    // tab" (a shipped #2283 clause with its own sealed ids) while §9's key
    // table makes `Ctrl-W` the leader of four pane chords. The two collide on
    // the same prefix and the contract never says how they coexist — a
    // pending-leader state that waits for the next key would delay §4's close;
    // an immediate close would swallow §9's chords. This slice sends §9's
    // chords as written and asserts §9's outcomes; it does not re-assert §4's
    // bare-`Ctrl-W` behaviour (that is #2283's slice's subject, and taking a
    // side here would pin one of the two readings). Flagged to the coordinator
    // in `manifest.yml`.
    //
    // TODO(test-author): `mocks/board-split-side-by-side.screen`'s status-bar
    // row reads `click=preview  dbl-click=pin  ctrl-w v/s=split  ctrl-w w=next
    // pane  q=quit` — it DROPS §8a's pinned `ctrl-w=close  ctrl-tab=next`
    // segments and adds two split ones. §8a pins its own string as an "exact
    // substring, taken verbatim from the issue body", and §9 pins no status-bar
    // string at all for the split state. Asserting either would contradict the
    // other, so no status-bar clause is authored here. Flagged to the
    // coordinator in `manifest.yml`.
    //
    // TODO(test-author): §9 says nothing about what a NEWLY created pane holds
    // at the instant `Ctrl-W v` returns — an empty tab set, a copy of the
    // focused document (vim's split semantics), or the moved active tab. The
    // mock only shows the state after a subsequent single click, which every
    // reading can reach. No test asserts the new pane's initial contents.
    //
    // TODO(test-author): the split is asserted on the BOARD panel only. §9 says
    // "per-pane-within-scope", implying Pipeline splits too, but names no
    // Pipeline split mock, and finding 7 in `manifest.yml` (the #2284 author's)
    // documents a live fixture-wipe blocker on entering the Pipeline panel from
    // this external test crate.
    //
    // TODO(test-author): §9's "Out of scope" (via the tracking issue) excludes
    // 2×2 quadrants, polymorphic PTY tabs, the decision lane, and dragging or
    // tearing a tab between panes. Nothing here drives any of them.
    // ───────────────────────────────────────────────────────────────────────
}
