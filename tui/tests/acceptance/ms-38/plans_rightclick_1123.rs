// Sealed acceptance slice for **issue #1123** — "Plans panel: right-click
// menus everywhere + discoverable plan creation" — milestone ms-38 (tracking
// issue #1120, "Plans panel -> rich client" epic).
//
// Authored independently from `tests/acceptance/ms-38/contract.md` (Gate A),
// with **zero** worker/implementation context: no work branch, PR or commit
// for #1123 was read. Every assertion below is derived from contract §4
// (CC-3) and its three mocks (`mocks/plans-rightclick-stub.screen`,
// `mocks/plans-rightclick-header.screen`, `mocks/plans-rightclick-epic.screen`)
// alone.
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-38 mock declares
// (`driver_with_shell(app, CoordApp::shell_config(), 120, 40)`, contract §7).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #1123 may run it
// (`coord acceptance run --issue 1123`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §4 only (CC-3). §3 (CC-2 detail pane, #1122) and §5 (CC-4 help
// overlay + palette, #1124) are other issues' slices and are deliberately
// untouched here. #1124's slice already lives in
// `tests/acceptance/ms-38/plans_help_1124.rs`; this file adds a second,
// independent module beside it.
//
// ── What is authored here, and what is NOT (and why) ──────────────────────
// Contract §4 has six clauses. Three are data-independent and are asserted
// below:
//
//   §4c  right-click on **empty space in the main panel** → the
//        "New plan > Quick capture" / "New plan > Guided chat…" / "Refresh"
//        menu.  §4c names "the repo group header … **or** empty space in the
//        main panel" as two equivalent targets for the same menu, and empty
//        space needs no plan-roster row to exist.
//   §4e  the single-letter keys survive as accelerators rendered *inside*
//        the menu items (`Refresh` … `r`), not as bare status-bar letters.
//   §4f  the post-CC-3 status bar advertises `right-click=menu` and `?=help`.
//
// The other three (§4a gate removal, §4b stub-row menu, §4d epic-row CRUD
// menu) all require a **seeded plan row**, and no plan row can be seeded from
// an external integration-test crate. `BoardData`'s fields and
// `PlanRosterEntry` itself are `pub(crate)` (`tui/src/app/types.rs`), and
// `CoordApp`'s fields are private with no public setter; the only public
// fixture seams are `make_test_app(BoardData)` /
// `make_app_with_assignments(..)` / `make_app_with_audit_json(..)`, none of
// which accept plan rows. Contract §7 assumes those clauses are covered by
// *in-crate* `#[cfg(test)]` tests ("Tests are **in-crate** … to access
// `make_test_app` and related `#[cfg(test)]`-only fixtures"), but the sealed
// oracle driver for this repo is the external `--test acceptance` target.
// This is the same external-seam gap #1039's ms-33 slice and #1124's slice
// both documented.
//
// They are left UNAUTHORED with a TODO at the bottom rather than written as
// permanently-impossible assertions: a test that can never go green would
// block the gate for a fully conformant implementation, and a test that
// "skips" when no row is found would be a silent green. See the summary for
// `tests/acceptance/ms-38` — this needs a contract amendment adding a public
// plan-roster fixture seam, not a guess here.
//
// NOTE FOR THE IMPLEMENTOR (not a contract clause — an observed fact about
// the fixture): with `BoardData::default()` the Plans main area renders its
// "Plans unavailable — not receiving plan-roster data" state, because
// `plan_roster_supported` is false. Contract §4c is **unconditional** — it
// pins no data precondition on the empty-space target — and #1123's whole
// point is *discoverable plan creation*, which matters most precisely when
// the operator has no plans yet. So the New-plan menu must open on a
// right-click in the empty main area in that state too. If the context menu
// is gated behind "has plan-roster data", these tests stay red against an
// otherwise-complete implementation; that would be an implementation gate
// the contract does not authorise, not a test defect.

mod plans_rightclick_1123 {
    use coord_tui::fixtures::{make_test_app, BoardData};
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::{Modifiers, MouseButton, Point, UiEvent};

    /// Build the app on an empty board and hand back a driver on the 120×40
    /// grid every ms-38 mock declares (contract §7).
    fn plans_driver() -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_test_app(BoardData::default());
        driver_with_shell(app, CoordApp::shell_config(), 120, 40)
    }

    /// Activate the Plans panel by clicking its activity-bar icon, then
    /// repaint.
    ///
    /// `panel:plans` / `SidebarView::Plans` with icon `◆` is the **shipped
    /// CC-1 (#1121) baseline** pinned in contract §1 — not part of #1123 — so
    /// a failure here means the baseline regressed, not that CC-3 is missing.
    fn nav_to_plans<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let (x, y) = driver.find("◆").expect(
            "contract §1 (shipped CC-1 baseline): the activity bar must render \
             the '◆' Plans icon so the Plans panel can be activated — not found",
        );
        assert!(
            x < 3.0,
            "contract §1: the '◆' Plans icon must live in the activity-bar \
             columns 0–2 (x < 3.0); found x = {x}",
        );
        driver.click(x, y);
        driver.render();
        assert!(
            driver.screen_contains(" PLANS "),
            "contract §1 (shipped CC-1 baseline): clicking the '◆' activity-bar \
             icon must activate SidebarView::Plans, whose sidebar header renders \
             the padded panel title \" PLANS \".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Cell coordinates of a blank cell in the **main panel** — contract
    /// §4c's "empty space in the main panel" target — on the 120×40 grid.
    ///
    /// Column 80 and row 30 are constants rather than a `find()` because
    /// nothing the contract pins is rendered there (that is the point: the
    /// cell must be *empty*). Both are then verified against the actual
    /// render instead of trusted: the cell must be blank, and there must be
    /// a vertical panel divider somewhere to its left, which is what makes
    /// it main-panel space rather than a sidebar row. On any plausible
    /// layout the activity bar + sidebar occupy well under 80 columns (the
    /// ms-38 mocks put the divider at column 39), and row 30 is blank in
    /// every ms-38 mock — the panel's bottom border is row 38 and the status
    /// bar row 39.
    fn empty_main_area_cell<A: quadraui::AppLogic>(driver: &TuiDriver<A>) -> (f32, f32) {
        const COL: usize = 80;
        const ROW: usize = 30;
        let screen = driver.screen();
        let row: Vec<char> = screen
            .lines()
            .nth(ROW)
            .unwrap_or_else(|| {
                panic!(
                    "harness sanity: the 120×40 grid must have a row {ROW}.\n\
                     --- screen ---\n{screen}",
                )
            })
            .chars()
            .collect();
        assert!(
            row.get(COL) == Some(&' '),
            "harness sanity: cell ({COL}, {ROW}) was picked as \"empty space in \
             the main panel\" (contract §4c) but is not blank on this render — \
             row {ROW} is {:?}.\n--- screen ---\n{screen}",
            row.iter().collect::<String>(),
        );
        assert!(
            row[..COL].iter().any(|c| "│┃".contains(*c)),
            "harness sanity: cell ({COL}, {ROW}) must sit to the RIGHT of the \
             sidebar/main-panel divider so the right-click lands in the main \
             panel, not the sidebar. No vertical divider found left of column \
             {COL} on row {ROW}: {:?}.\n--- screen ---\n{screen}",
            row.iter().collect::<String>(),
        );
        (COL as f32, ROW as f32)
    }

    /// Deliver a real right-click at backend cell coordinates `(x, y)` and
    /// repaint.
    ///
    /// `TuiDriver` has no `right_click` helper (its `click`/`mouse_down`
    /// hard-code `MouseButton::Left`), so the `UiEvent::MouseDown { button:
    /// Right, .. }` a real right-click produces is dispatched directly —
    /// the same full `event → handle → open_context_menu → render` chain,
    /// not a direct call into any menu-building function.
    fn right_click<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>, x: f32, y: f32) {
        driver.dispatch(UiEvent::MouseDown {
            widget: None,
            button: MouseButton::Right,
            position: Point::new(x, y),
            modifiers: Modifiers::default(),
        });
        driver.render();
    }

    /// Plans panel active, with a right-click delivered to **empty space in
    /// the main panel** (contract §4c's second named target).
    fn plans_with_main_area_right_click() -> TuiDriver<impl quadraui::AppLogic> {
        let mut driver = plans_driver();
        nav_to_plans(&mut driver);
        let (x, y) = empty_main_area_cell(&driver);
        right_click(&mut driver, x, y);
        driver
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4c — right-click empty main-panel space → discoverable plan creation
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §4c: right-clicking empty space in the main panel shows a
    /// menu containing the labelled `"New plan > Quick capture"` item (action
    /// `capture-plan-quick`, firing `coord milestone capture`).
    ///
    /// This is the clause that fixes the cryptic `c` binding — the label is a
    /// full phrase that appears nowhere else on any Plans screen (the base
    /// status bar's wording is `c=capture plan`), so it fails RED while no
    /// such menu exists.
    #[test]
    fn right_click_empty_main_area_offers_new_plan_quick_capture() {
        let driver = plans_with_main_area_right_click();
        assert!(
            driver.screen_contains("New plan > Quick capture"),
            "contract §4c: right-clicking empty space in the Plans main panel \
             must open a menu containing the item \"New plan > Quick capture\".\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4c: the same menu contains `"New plan > Guided chat…"`
    /// (action `capture-plan-chat`, firing `coord milestone chat --new`) —
    /// the labelled replacement for the cryptic `C` binding.
    ///
    /// Note the trailing character is a single-glyph ellipsis `…` (U+2026),
    /// exactly as the contract table and `mocks/plans-rightclick-header.screen`
    /// spell it — not three periods.
    #[test]
    fn right_click_empty_main_area_offers_new_plan_guided_chat() {
        let driver = plans_with_main_area_right_click();
        assert!(
            driver.screen_contains("New plan > Guided chat…"),
            "contract §4c: right-clicking empty space in the Plans main panel \
             must open a menu containing the item \"New plan > Guided chat…\" \
             (single-glyph ellipsis U+2026).\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4c + §4e: the New-plan menu also carries `"Refresh"`, and
    /// its single-letter key survives as an accelerator rendered **inside**
    /// the menu item (`.with_shortcut('r')`) rather than as a bare status-bar
    /// letter.
    ///
    /// Asserted structurally — a standalone `r` token somewhere to the right
    /// of `"Refresh"` on the same rendered row — rather than as a fixed
    /// column, because the contract pins the accelerator's presence but not
    /// its position (`mocks/plans-rightclick-header.screen` right-aligns it
    /// inside the menu box; `mocks/plans-rightclick-stub.screen` puts it a
    /// column further left).
    #[test]
    fn right_click_empty_main_area_menu_shows_refresh_with_r_accelerator() {
        let driver = plans_with_main_area_right_click();
        let screen = driver.screen();
        let row = screen
            .lines()
            .find(|line| line.contains("Refresh"))
            .unwrap_or_else(|| {
                panic!(
                    "contract §4c: the empty-main-area right-click menu must \
                     contain a \"Refresh\" item.\n--- screen ---\n{screen}",
                )
            });
        // "Refresh" is ASCII, so this byte offset is a char boundary even on
        // a row full of multi-byte box-drawing glyphs.
        let tail = &row[row.find("Refresh").expect("substring just matched") + "Refresh".len()..];
        let has_accelerator = tail
            .split(|c: char| c.is_whitespace() || "│┃|┆╎┊".contains(c))
            .any(|token| token == "r");
        assert!(
            has_accelerator,
            "contract §4c/§4e: the \"Refresh\" menu item must show its \
             single-letter accelerator `r` inside the item (`.with_shortcut()`), \
             not only as a status-bar letter. No standalone `r` token was found \
             to the right of \"Refresh\" on this row:\n{row:?}\n\
             --- screen ---\n{screen}",
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4f — status bar points at right-click + `?`, not a cryptic letter row
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §4f: the post-CC-3 Plans status bar advertises the context
    /// menu itself — `"right-click=menu"`.
    ///
    /// Red before CC-3: the CC-1 status bar is
    /// `j/k=nav  Enter=open epic  c=capture plan  u=toggle untracked  q=quit`,
    /// which contains no such hint. Asserted on the plain Plans view (no menu
    /// open) so it is the *base* hint set being checked.
    #[test]
    fn plans_status_bar_advertises_right_click_menu() {
        let mut driver = plans_driver();
        nav_to_plans(&mut driver);
        assert!(
            driver.screen_contains("right-click=menu"),
            "contract §4f: after CC-3 the Plans status bar must contain \
             \"right-click=menu\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4f: the post-CC-3 Plans status bar points at the `?` help
    /// overlay — `"?=help"`.
    ///
    /// The overlay itself is #1124's surface (contract §5) and is asserted in
    /// that slice; this checks only that CC-3's status-bar rewrite advertises
    /// it, which §4f requires of CC-3.
    ///
    /// **Already green when authored**, because CC-4 (#1124) shipped first
    /// (§8 note 6) and its status bar already reads `… ?=help  /=palette …`.
    /// Kept anyway rather than dropped: §4f requires this hint of CC-3, and
    /// #1123 explicitly rewrites the status bar ("clean up the status-bar
    /// hint so it points to right-click + `?` + palette"), so this is the
    /// regression guard that the rewrite does not drop what CC-4 added.
    #[test]
    fn plans_status_bar_advertises_help_key() {
        let mut driver = plans_driver();
        nav_to_plans(&mut driver);
        assert!(
            driver.screen_contains("?=help"),
            "contract §4f: after CC-3 the Plans status bar must contain \
             \"?=help\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ───────────────────────────────────────────────────────────────────────
    // NOT AUTHORED — deliberately, rather than guessed or faked green.
    //
    // TODO(test-author): contract §4a (drop the `tracking_issue: Some(_)`
    // gate so *every* plan row is right-clickable), §4b (right-click a stub
    // row → "Create work order / promote to epic…" + "Refresh") and §4d
    // (right-click a full epic row → the eleven-item CRUD menu, of which the
    // contract makes "Open milestone chat", "Dispatch milestone" and
    // "Close / archive plan" load-bearing) all require a **seeded plan row**.
    // No plan row is reachable from this external `--test acceptance` crate:
    // `BoardData::plan_roster` / `plan_roster_supported` and `PlanRosterEntry`
    // are `pub(crate)`, `CoordApp`'s fields are private with no public setter,
    // and the public fixture seams (`make_test_app` / `make_app_with_assignments`
    // / `make_app_with_audit_json`) take no plan data. Triggering a real board
    // load is not an option either: `start_data_load` is `pub(crate)`, and the
    // `#[cfg(test)]` short-circuit that makes it deterministic in-crate is NOT
    // active when the lib is compiled as this crate's dependency — it would
    // spawn a thread reading the developer's real `coord.db` / daemon.
    //
    // Contract §6 *mandates* the fixture ("The `BoardData` must carry
    // `plan_roster` with at least: one entry with `tracking_issue: Some(1120)`
    // …; one entry with `tracking_issue: None` … (stub — exercises CC-3 §4b)"),
    // but names no public constructor for it, and §7 assumes these tests are
    // in-crate. Naming an invented seam here would be a compile error in an
    // `include!`d slice, which errors out the WHOLE acceptance target for
    // every ms-38 issue rather than failing this one.
    //
    // Resolution needed before these three clauses can be sealed: amend the
    // contract to pin a public seam — e.g.
    // `coord_tui::fixtures::make_app_with_plan_roster_json(data, json)`
    // mirroring the existing `make_app_with_audit_json` (a JSON seam works:
    // `PlanRosterEntry` already derives `serde::Deserialize`, so the wire
    // shape in §6 can be fed verbatim). Once that lands, add:
    //   * `right_click_stub_row_offers_create_work_order`   (§4a/§4b)
    //   * `right_click_stub_row_menu_offers_refresh`        (§4b)
    //   * `right_click_epic_row_shows_full_crud_menu`       (§4d)
    //   * `right_click_repo_header_offers_new_plan_menu`    (§4c, header target)
    //
    // TODO(test-author): contract §4b leaves the "Create work order / promote
    // to epic…" *action* deliberately unpinned ("TBD — new action"; §8 note 2:
    // "The contract pins only the menu label, not the action implementation").
    // Nothing observable is specified for activating it, so no post-activation
    // assertion is authored.
    //
    // TODO(test-author): contract §4f says the cryptic `c=capture plan` and
    // `u=toggle untracked` hints "**may** be removed from the status bar".
    // Permissive language is not assertable in either direction — asserting
    // their absence would forbid a conformant implementation that kept them,
    // and asserting their presence would forbid one that dropped them. Only
    // the two required additions are tested.
    //
    // TODO(test-author): contract §4e says the accelerators `c`, `C`, `d`,
    // `u`, `r` "remain as accelerators shown inside the right-click menu",
    // but only pins which item carries which letter for `d` ("Dispatch
    // milestone", §4d) and `r` ("Refresh", §4c/§4d). `c`/`C`/`u` are not
    // mapped to a menu item anywhere in §4, and the two `New plan >` items in
    // §4c carry no shortcut column in the contract's table (nor in
    // `mocks/plans-rightclick-header.screen`). Only the `r` accelerator is
    // asserted here; `d` belongs to the §4d epic-row menu, blocked above.
    // ───────────────────────────────────────────────────────────────────────
}
