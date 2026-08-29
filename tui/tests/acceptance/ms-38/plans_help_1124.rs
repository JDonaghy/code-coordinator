// Sealed acceptance slice for **issue #1124** — "Plans panel: wire in `?`
// help overlay + command palette" — milestone ms-38 (tracking issue #1120,
// "Plans panel -> rich client" epic).
//
// Authored independently from `tests/acceptance/ms-38/contract.md` (Gate A),
// with **zero** worker/implementation context: no work branch, PR or commit
// for #1124 was read. Every assertion below is derived from contract §5
// (CC-4) and its two mocks (`mocks/plans-help-overlay.screen`,
// `mocks/plans-palette.screen`) alone.
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-38 mock declares
// (`driver_with_shell(app, CoordApp::shell_config(), 120, 40)`, contract §7).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #1124 may run it
// (`coord acceptance run --issue 1124`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §5 only (CC-4). §3 (CC-2 detail pane, #1122) and §4 (CC-3
// right-click menus, #1123) are other issues' slices and are deliberately
// untouched here.
//
// ── Why these tests need no plan-roster seeding ───────────────────────────
// Contract §6 sketches a shared fixture surface whose `BoardData` carries a
// populated `plan_roster`. That surface is **not reachable from an external
// integration-test crate**: `BoardData`'s fields and `PlanRosterEntry` itself
// are `pub(crate)` (`tui/src/app/types.rs`), and the only public seams are
// `make_test_app(BoardData)` / `make_app_with_assignments(..)` /
// `make_app_with_audit_json(..)` — none of which accept plan rows. This is
// the same external-seam gap #1039's ms-33 slice documented.
//
// It costs this slice nothing: **every** §5 assertion is data-independent.
//
// NOTE FOR THE IMPLEMENTOR (not a contract clause — an observed fact about
// the fixture): with `BoardData::default()` the Plans main area renders its
// "Plans unavailable — not receiving plan-roster data" state, because
// `plan_roster_supported` is false. Contract §5a and §5e are **unconditional**
// ("Pressing `?` / `/` while `active_view == SidebarView::Plans` opens …"):
// they carry no data or selection precondition, so the help overlay and the
// command palette must open — and render their full §5c/§5d/§5g content — in
// that state too. If `?` / `/` are wired behind "has plan data", these tests
// stay red against an otherwise-complete implementation. That would be an
// implementation gate the contract does not authorise, not a test defect.
// The overlay title, its key/chip legend, the palette title, its Plans-action
// entries, the search filter and the `Esc=close` hint are all properties of
// the *view*, not of any particular plan row. Where a §5 clause would need a
// selected plan (there is none), it is flagged as a TODO at the bottom rather
// than guessed. See the summary for `tests/acceptance/ms-38`.

mod plans_help_1124 {
    use coord_tui::fixtures::{make_test_app, BoardData};
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::NamedKey;

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
    /// CC-1 (#1121) baseline** pinned in contract §1 — not part of #1124 — so
    /// a failure here means the baseline regressed, not that CC-4 is missing.
    /// Every §5 assertion below is meaningless unless the Plans view is
    /// genuinely active, hence the `" PLANS "` header check (contract §1
    /// `PanelDefinition.title == "PLANS"`).
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

    /// Plans panel active with the `?` help overlay open (contract §5a).
    fn plans_with_help_overlay() -> TuiDriver<impl quadraui::AppLogic> {
        let mut driver = plans_driver();
        nav_to_plans(&mut driver);
        driver.type_char('?');
        driver.render();
        driver
    }

    /// Plans panel active with the `/` command palette open (contract §5e).
    fn plans_with_palette() -> TuiDriver<impl quadraui::AppLogic> {
        let mut driver = plans_driver();
        nav_to_plans(&mut driver);
        driver.type_char('/');
        driver.render();
        driver
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §5a–5d — `?` help overlay
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §5a (trigger) + §5b (required title): pressing `?` while
    /// `active_view == SidebarView::Plans` opens a cheatsheet modal titled
    /// exactly `"Plans — Help"`.
    ///
    /// `"Plans — Help"` (em-dash) occurs nowhere else on any Plans screen, so
    /// this fails RED while no overlay is wired up.
    #[test]
    fn help_overlay_opens_on_question_mark() {
        let driver = plans_with_help_overlay();
        assert!(
            driver.screen_contains("Plans — Help"),
            "contract §5a/§5b: pressing '?' in the Plans panel must open the \
             cheatsheet modal, whose title is exactly \"Plans — Help\".\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §5c: the cheatsheet lists every required key entry.
    ///
    /// Each needle is a *phrase* that occurs only inside the overlay — never
    /// in the Plans base or detail status bars — exactly as the 2026-07-28
    /// amendment's rationale requires. In particular `"quick capture plan"`
    /// and `"toggle untracked milestones"` are deliberately longer than the
    /// status bar's `capture plan` / `toggle untracked`, and are lowercase
    /// where the palette's own entries (§5g) are capitalised
    /// (`screen_contains` is case-sensitive, so the two never collide).
    #[test]
    fn help_overlay_lists_key_entries() {
        let driver = plans_with_help_overlay();
        let screen = driver.screen();
        for needle in [
            "open context menu",
            "open detail pane",
            "close / back",
            "this help overlay",
            "command palette",
            "quick capture plan",
            "guided chat (new plan)",
            "toggle untracked milestones",
        ] {
            assert!(
                screen.contains(needle),
                "contract §5c: the '?' help overlay must list the entry \
                 {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    /// Contract §5d: the overlay includes a health-chip legend naming each
    /// `needs_you` chip verbatim.
    #[test]
    fn help_overlay_health_chip_legend() {
        let driver = plans_with_help_overlay();
        let screen = driver.screen();
        for needle in ["ready_waiting", "stalled", "chat_pending", "no_work_order"] {
            assert!(
                screen.contains(needle),
                "contract §5d: the '?' help overlay's health-chip legend must \
                 name the chip {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    /// Contract §5i: while the help overlay is open the status bar shows
    /// `"Esc=close"` (see `mocks/plans-help-overlay.screen`:
    /// `?=help  Esc=close  q=quit`).
    #[test]
    fn help_overlay_status_bar_shows_esc_close() {
        let driver = plans_with_help_overlay();
        assert!(
            driver.screen_contains("Esc=close"),
            "contract §5i: with the help overlay open the status bar must \
             contain \"Esc=close\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §5a: **Esc** closes the help overlay — the `"Plans — Help"`
    /// title is gone and the Plans view is shown again.
    #[test]
    fn help_overlay_esc_closes() {
        let mut driver = plans_with_help_overlay();
        assert!(
            driver.screen_contains("Plans — Help"),
            "precondition: '?' must open the help overlay before Esc can close \
             it.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver.press_named(NamedKey::Escape);
        driver.render();
        assert!(
            !driver.screen_contains("Plans — Help"),
            "contract §5a: Esc must close the help overlay — the \"Plans — Help\" \
             title must no longer be rendered.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains(" PLANS "),
            "contract §5a: after Esc the Plans panel must still be the active \
             view (its \" PLANS \" sidebar header is rendered again).\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §5e–5i — `/` command palette
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §5e (trigger — `/`, locked by the 2026-07-28 amendment /
    /// note 4) + §5f (required title): pressing `/` in the Plans panel opens
    /// the command palette.
    ///
    /// **Both** §5f strings are asserted. `"command palette"` alone does not
    /// prove the palette is open, because §5c requires that same string
    /// inside the help overlay; `"Plans actions"` is unique to the palette.
    /// The overlay title is additionally asserted *absent* to close that gap
    /// from the other side.
    #[test]
    fn palette_opens_on_slash() {
        let driver = plans_with_palette();
        let screen = driver.screen();
        assert!(
            screen.contains("command palette"),
            "contract §5e/§5f: pressing '/' in the Plans panel must open the \
             command palette, titled \"command palette\".\n\
             --- screen ---\n{screen}",
        );
        assert!(
            screen.contains("Plans actions"),
            "contract §5f: the open palette must render the \"Plans actions\" \
             section header (the string that distinguishes the palette from the \
             help overlay, which also contains \"command palette\").\n\
             --- screen ---\n{screen}",
        );
        assert!(
            !screen.contains("Plans — Help"),
            "contract §5e: '/' must open the command palette, not the '?' help \
             overlay — \"Plans — Help\" must not be on screen.\n\
             --- screen ---\n{screen}",
        );
    }

    /// Contract §5g: every registered Plans action label is listed while the
    /// palette is open and unfiltered.
    ///
    /// The contract's "Required" call-outs name three of these; the §5g table
    /// declares all eight labels as "an exact string that must appear in the
    /// palette while it is open and unfiltered", so all eight are asserted.
    #[test]
    fn palette_lists_plans_actions() {
        let driver = plans_with_palette();
        let screen = driver.screen();
        for needle in [
            "Dispatch milestone",
            "Open milestone chat",
            "Quick capture plan",
            "Guided chat (new plan)",
            "View order / DAG",
            "Edit milestone…",
            "Add issue to milestone…",
            "Toggle untracked milestones",
        ] {
            assert!(
                screen.contains(needle),
                "contract §5g: the open, unfiltered command palette must list \
                 the Plans action {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    /// Contract §5h: typing a search string narrows the palette — after
    /// typing `"dispatch"`, `"Dispatch milestone"` is still listed and
    /// non-matching entries are gone.
    ///
    /// TODO(test-author): the contract does not pin the *matching algorithm*
    /// (case-insensitive substring vs. fuzzy subsequence vs. description-text
    /// matching), only the outcome for the `"dispatch"` example. The two
    /// negative needles below are chosen so they are absent under any of
    /// those readings: neither `"Quick capture plan"` nor
    /// `"Toggle untracked milestones"` contains `"dispatch"` as a substring,
    /// as a subsequence, or in its `mocks/plans-palette.screen` description.
    #[test]
    fn palette_search_filters_entries() {
        let mut driver = plans_with_palette();
        for c in "dispatch".chars() {
            driver.type_char(c);
        }
        driver.render();
        let screen = driver.screen();
        assert!(
            screen.contains("Dispatch milestone"),
            "contract §5h: after typing \"dispatch\" the palette must still \
             list the matching entry \"Dispatch milestone\".\n\
             --- screen ---\n{screen}",
        );
        for absent in ["Quick capture plan", "Toggle untracked milestones"] {
            assert!(
                !screen.contains(absent),
                "contract §5h: after typing \"dispatch\" the palette must hide \
                 entries that do not match — {absent:?} is still displayed.\n\
                 --- screen ---\n{screen}",
            );
        }
    }

    /// Contract §5i: while the command palette is open the status bar shows
    /// `"Esc=close"` (see `mocks/plans-palette.screen`:
    /// `/=palette  Esc=close  q=quit`).
    #[test]
    fn palette_status_bar_shows_esc_close() {
        let driver = plans_with_palette();
        assert!(
            driver.screen_contains("Esc=close"),
            "contract §5i: with the command palette open the status bar must \
             contain \"Esc=close\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §5e: **Esc** closes the palette and returns to the Plans view.
    #[test]
    fn palette_esc_closes() {
        let mut driver = plans_with_palette();
        assert!(
            driver.screen_contains("Plans actions"),
            "precondition: '/' must open the command palette before Esc can \
             close it.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver.press_named(NamedKey::Escape);
        driver.render();
        assert!(
            !driver.screen_contains("Plans actions"),
            "contract §5e: Esc must close the command palette — the \"Plans \
             actions\" section header must no longer be rendered.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains(" PLANS "),
            "contract §5e: after Esc the Plans panel must still be the active \
             view (its \" PLANS \" sidebar header is rendered again).\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    // ───────────────────────────────────────────────────────────────────────
    // NOT AUTHORED — deliberately, rather than guessed.
    //
    // TODO(test-author): contract §5e permits `Ctrl+P` as an *optional*
    // additional palette alias ("An implementation may register Ctrl+P as an
    // additional alias; it may not replace `/`"). Optional behaviour is not
    // assertable in either direction — a passing `ctrl_char('p')` test would
    // fail a conformant implementation that skipped the alias, and a failing
    // one would forbid it. Only the locked `/` trigger is tested.
    //
    // TODO(test-author): contract §5g binds each palette entry to an action
    // ID (`dispatch-milestone`, `capture-plan-quick`, …), but §5 pins no
    // observable outcome for *activating* a palette entry — no resulting
    // screen state, dialog title or command string is specified anywhere in
    // the contract, and `CommandRunner::new_for_test()`'s recorded
    // `spawned_calls` is not reachable from an external test crate. Selecting
    // an entry and asserting what it fires is therefore left unauthored
    // rather than invented. If a later contract round pins the post-activation
    // surface, add e.g. `palette_entry_fires_bound_action`.
    //
    // TODO(test-author): §5g's `"Dispatch milestone"` / `"Open milestone
    // chat"` entries act on "the selected plan", but no plan row can be
    // seeded from an external acceptance crate (see the header note:
    // `PlanRosterEntry` and `BoardData`'s fields are `pub(crate)`). The
    // contract nonetheless requires these labels to be listed "while it is
    // open and unfiltered" with no selection precondition, so
    // `palette_lists_plans_actions` asserts the labels only — never any
    // selection-dependent state. If a future round adds a public
    // plan-roster seam (e.g. `make_app_with_plan_roster_json(data, json)`,
    // mirroring `make_app_with_audit_json`), the §5g entries could
    // additionally be checked against a real selected plan.
    // ───────────────────────────────────────────────────────────────────────
}
