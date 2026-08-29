// Sealed acceptance slice for **issue #1122** — "Plans panel: rich in-app
// plan detail pane" (CC-2) — milestone ms-38 (tracking issue #1120, "Plans
// panel -> rich client" epic).
//
// Authored independently from `tests/acceptance/ms-38/contract.md` (Gate A),
// with **zero** worker/implementation context: no work branch, PR or commit
// for #1122 was read, and `tui/src/app/plans.rs` (the issue's scope file) was
// deliberately never opened. Every assertion below is derived from contract
// §3 (CC-2) and `mocks/plans-detail-pane.screen` alone.
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-38 mock declares
// (`driver_with_shell(app, CoordApp::shell_config(), 120, 40)`, contract §7).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #1122 may run it
// (`coord acceptance run --issue 1122`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §3 only (CC-2). §4 (CC-3 right-click menus, #1123) and §5 (CC-4
// help overlay + palette, #1124) are other issues' slices, already authored
// in `plans_rightclick_1123.rs` / `plans_help_1124.rs`, and are untouched.
//
// ═══════════════════════════════════════════════════════════════════════════
// ⚠ HARNESS BLOCKER — READ THIS BEFORE TREATING A FAILURE AS AN IMPL DEFECT
// ═══════════════════════════════════════════════════════════════════════════
// **Every** clause of contract §3 is predicated on a *selected plan row*:
// §3a ("pressing Enter on a selected plan row whose `tracking_issue` is
// `Some(_)`"), and §3b–§3f on the detail pane that row opens. No plan row can
// be seeded from this external `--test acceptance` crate, so as the repo
// stands **none of §3 is reachable here** and all five tests below fail at
// their shared precondition rather than on a §3 assertion.
//
// This was established empirically, not assumed. Building the only fixture
// the external crate can build — `make_test_app(BoardData::default())` — and
// activating the Plans panel renders:
//
//     ╭──────────────────────────────────────────────────────╮
//     │   Plans unavailable — not receiving plan-roster data. │
//
// with an "All repos" root and no repo or milestone rows at all. The cause:
//
//   * `BoardData::plan_roster` / `plan_roster_supported` and
//     `PlanRosterEntry` are all `pub(crate)` (`tui/src/app/types.rs`).
//     `BoardData` itself is `pub` but derives only `Default` — no
//     `Deserialize`, no public builder, no public field — so
//     `BoardData::default()` is literally the only value an external crate
//     can construct.
//   * `CoordApp`'s fields are private with no public setter, and
//     `driver.app_mut()` hands back the quadraui shell wrapper anyway.
//   * The public fixture seams (`make_test_app`, `make_app_with_assignments`,
//     `make_app_with_audit_json`, `make_app_with_one_completed_issue`,
//     `make_assignment_typed`) take no plan data. `Assignment` carries no
//     milestone field, so `make_app_with_assignments` cannot induce plan rows
//     either.
//   * The real-network seam (`set_test_board_service` + `MockBoardService`,
//     both `pub`) does **not** rescue this: the `/board` load runs through
//     `start_data_load`, which is `pub(crate)` *and* spawns a thread —
//     `resolve_board_service()` therefore executes on the spawned thread,
//     where the deliberately thread-local `TEST_BOARD_SERVICE_OVERRIDE` is
//     invisible. It would fall through to the local SQLite path and read the
//     developer's real `coord.db`: non-deterministic, and a silent-green
//     hazard if that DB happens to contain a matching plan. Not viable for a
//     sealed oracle.
//
// The root cause is a contract/driver mismatch, not an oversight in any one
// clause: contract §7 states "Tests are **in-crate** (`#[cfg(test)]` in
// `tui/src/app/tests.rs` or a nearby module) to access `make_test_app` and
// related `#[cfg(test)]`-only fixtures", but this repo's declared sealed
// oracle driver (`tui-tuidriver`, `cargo test --test acceptance`) is an
// *external* integration-test binary. Contract §6 mandates the fixture
// ("The `BoardData` must carry `plan_roster` with at least: one entry with
// `tracking_issue: Some(1120)`, `has_work_order: true` …") but names no
// public constructor for it. The same wall stopped §4a/§4b/§4d in
// `plans_rightclick_1123.rs`; for #1123 three of six clauses survived, but
// for #1122 it takes the entire issue.
//
// **Why these tests were still authored, rather than left out.** An empty
// slice reports zero tests for #1122 and the gate reads that as a vacuous
// pass, merging the milestone's largest child with no acceptance evidence at
// all. Failing loudly in the safe direction is the lesser evil. The §3
// assertions are written out in full below rather than stubbed, so once the
// seam lands the only edit needed is to the `plans_driver()` helper.
//
// **Why an invented seam was NOT named instead.** Writing
// `make_app_with_plan_roster_json(..)` here would not fail — it would be a
// *compile error*, and because every ms-38 slice is `include!`d into one
// crate root that would take down the whole `--test acceptance` target,
// turning #1123's and #1124's currently-green tests red too.
//
// **Resolution needed (coordinator action, before #1122's worker is dispatched):**
// amend the contract to pin a public plan-roster fixture seam and land it —
//     `coord_tui::fixtures::make_app_with_plan_roster_json(data, json)`
// mirroring the existing `make_app_with_audit_json` (itself added in exactly
// this way, in response to the ms-33 slice's TODO). A JSON seam is the right
// shape here: `PlanRosterEntry` already derives `serde::Deserialize`, so the
// §6 wire block can be fed verbatim — which additionally guards the #632
// blank-board class that contract §3e warns about, since a mistyped field
// would blank the fixture exactly as it blanks the real board. Then
// re-dispatch a test-author for this slice.
// ═══════════════════════════════════════════════════════════════════════════

mod plans_detail_1122 {
    use coord_tui::fixtures::{make_test_app, BoardData};
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::NamedKey;

    /// Build the app and hand back a driver on the 120×40 grid every ms-38
    /// mock declares (contract §7).
    ///
    /// `BoardData::default()` carries no `plan_roster`, which is what blocks
    /// this whole slice — see the HARNESS BLOCKER banner above. This is the
    /// **single** function that needs to change once a public plan-roster
    /// fixture seam exists: swap in the contract §6 fixture (milestone #38,
    /// `tracking_issue: Some(1120)`, `has_work_order: true`, `done: 2`,
    /// `total: 6`, `needs_you: ["ready_waiting"]`) and every assertion below
    /// becomes live as written.
    fn plans_driver() -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_test_app(BoardData::default());
        driver_with_shell(app, CoordApp::shell_config(), 120, 40)
    }

    /// Activate the Plans panel by clicking its activity-bar icon, then
    /// repaint.
    ///
    /// `panel:plans` / `SidebarView::Plans` with icon `◆` is the **shipped
    /// CC-1 (#1121) baseline** pinned in contract §1 — not part of #1122 — so
    /// a failure here means the baseline regressed, not that CC-2 is missing.
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

    /// Cell coordinates of the sidebar row for the contract §6 fixture plan
    /// (milestone #38, `tracking_issue: Some(1120)`) — the "selected plan row
    /// whose `tracking_issue` is `Some(_)`" that contract §3a requires.
    ///
    /// Located by its milestone number rather than by the `●` tracked-leaf
    /// glyph of contract §2, because the contract and its own mock disagree
    /// on that glyph (§2's table says `●`; `mocks/plans-detail-pane.screen`
    /// line 3 renders the same row as `▸ #38 …`). `"#38"` is what both agree
    /// on. The hit is then constrained to the sidebar columns so a `#38`
    /// occurring in the main panel can never be mistaken for the row.
    ///
    /// Panics with the HARNESS BLOCKER diagnosis when no plan row exists,
    /// which is the state of the sealed suite today.
    fn tracked_plan_row<A: quadraui::AppLogic>(driver: &TuiDriver<A>) -> (f32, f32) {
        let (x, y) = driver.find("#38").unwrap_or_else(|| {
            panic!(
                "HARNESS BLOCKER (not an implementation defect) — contract §3 is \
                 unreachable from the sealed external acceptance crate.\n\n\
                 Contract §3a needs a selected plan row with `tracking_issue: \
                 Some(_)`; contract §6 mandates that fixture (milestone #38, \
                 `tracking_issue: Some(1120)`). No plan row is on screen because \
                 `BoardData::default()` is the only fixture an external crate can \
                 build: `plan_roster` / `plan_roster_supported` / `PlanRosterEntry` \
                 are `pub(crate)`, `BoardData` derives only `Default`, and no \
                 public fixture seam takes plan data.\n\n\
                 This is NOT evidence that #1122 is unimplemented — it is the \
                 contract §7 (\"tests are in-crate\") vs. declared-driver \
                 (external `--test acceptance`) mismatch. Fix by landing \
                 `coord_tui::fixtures::make_app_with_plan_roster_json(data, json)` \
                 (mirroring `make_app_with_audit_json`), amending the contract to \
                 pin it, and re-authoring this slice's `plans_driver()` helper.\n\n\
                 --- screen ---\n{}",
                driver.screen(),
            )
        });
        assert!(
            x < 38.0,
            "harness sanity: the \"#38\" hit must be the *sidebar* plan row \
             (contract §2 milestone leaf), not a main-panel string — the ms-38 \
             mocks put the sidebar/main divider at column 38, but this hit is at \
             x = {x}.\n--- screen ---\n{}",
            driver.screen(),
        );
        (x, y)
    }

    /// Plans panel active, the contract §6 fixture plan row selected, and
    /// **Enter** pressed on it — contract §3a's trigger.
    fn plans_with_detail_pane() -> TuiDriver<impl quadraui::AppLogic> {
        let mut driver = plans_driver();
        nav_to_plans(&mut driver);
        let (x, y) = tracked_plan_row(&driver);
        driver.click(x, y);
        driver.render();
        driver.press_named(NamedKey::Enter);
        driver.render();
        driver
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §3a + §3b — Enter opens the detail pane; required header strings
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §3a (trigger) + §3b (required header strings): pressing Enter
    /// on a plan row with `tracking_issue: Some(_)` opens the in-app detail
    /// pane, whose header carries the milestone number, the milestone title,
    /// the tracking-epic ref and the done percentage.
    ///
    /// The four needles are exactly §3b's table plus its worked example for
    /// fixture milestone #38. `"Plans panel -> rich"` is used rather than the
    /// full title because `mocks/plans-detail-pane.screen` shows the header
    /// eliding it (`Plans panel -> rich cl…`) when the pane is narrow, and
    /// §3b requires the *title* to be present, not a particular truncation.
    /// `"% done"` likewise follows §3b's example rather than pinning `33%`,
    /// since the contract derives the number from `done / total` and does not
    /// pin a rounding rule.
    ///
    /// `"epic:#1120"` is the load-bearing needle: it appears on no other
    /// Plans screen state, so it cannot be satisfied by the list view.
    #[test]
    fn detail_pane_opens_on_enter_with_header() {
        let driver = plans_with_detail_pane();
        let screen = driver.screen();
        for needle in ["#38", "Plans panel -> rich", "epic:#1120", "% done"] {
            assert!(
                screen.contains(needle),
                "contract §3a/§3b: Enter on a plan row with `tracking_issue: \
                 Some(_)` must open the in-app detail pane, whose header \
                 contains {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §3c — work-order checklist
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §3c: with `has_work_order == true` the detail pane renders a
    /// work-order checklist — the `"Work order"` section heading plus at
    /// least one status glyph on a checklist row (`✓` done / `▶` in-flight /
    /// `·` ready / `—` blocked).
    ///
    /// Deliberately does **not** assert per-issue rows. Contract §8 note 1
    /// leaves it to the implementor whether per-child issue detail is added
    /// to the `/board` wire or the checklist is derived from the aggregate
    /// counts alone, and §3c's "Required strings" is explicitly written to
    /// admit both; asserting `"#1121"` or `"CC-1: repo tree sidebar"` from
    /// `mocks/plans-detail-pane.screen` would forbid the aggregate approach
    /// the contract permits.
    #[test]
    fn detail_pane_shows_work_order_checklist() {
        let driver = plans_with_detail_pane();
        let screen = driver.screen();
        assert!(
            screen.contains("Work order"),
            "contract §3c: the detail pane must render the \"Work order\" \
             checklist section heading.\n--- screen ---\n{screen}",
        );
        let glyphs = ['✓', '▶', '·', '—'];
        assert!(
            screen.lines().any(|line| {
                line.contains("#") && glyphs.iter().any(|g| line.contains(*g))
            }),
            "contract §3c: at least one work-order row must carry a status \
             glyph — `✓` (done), `▶` (in-flight), `·` (ready) or `—` (blocked). \
             No row containing an issue ref and one of {glyphs:?} was \
             rendered.\n--- screen ---\n{screen}",
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §3d — actions row
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §3d: the detail pane renders an actions row carrying every
    /// required action label.
    ///
    /// All five §3d labels are asserted, not just the two the clause repeats
    /// as explicit "Required" call-outs — the §3d table itself introduces the
    /// column as "Required action labels (exact strings)".
    ///
    /// `"Open in browser"` is the load-bearing one for this issue's whole
    /// premise: §3a demotes `gh issue view --web` from Enter's behaviour to
    /// one action among several, and this string appears nowhere on the
    /// pre-CC-2 Plans screens.
    #[test]
    fn detail_pane_shows_actions_row() {
        let driver = plans_with_detail_pane();
        let screen = driver.screen();
        for needle in [
            "Dispatch next",
            "Open chat",
            "View DAG",
            "Edit",
            "Open in browser",
        ] {
            assert!(
                screen.contains(needle),
                "contract §3d: the detail pane's actions row must offer \
                 {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §3f — status bar while the detail pane is open
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §3f: with the detail pane open the status bar shows
    /// `"Esc=back"` (`mocks/plans-detail-pane.screen` line 40 spells the full
    /// hint `Esc=back to list`; `"Esc=back"` is the prefix §3f pins, so an
    /// implementation using either wording passes).
    #[test]
    fn detail_pane_status_bar_shows_esc_back() {
        let driver = plans_with_detail_pane();
        assert!(
            driver.screen_contains("Esc=back"),
            "contract §3f: with the detail pane open the status bar must \
             contain \"Esc=back\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §3a — Esc returns to the list view
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §3a: "Pressing **Esc** returns to the list view." The detail
    /// pane is the full main-area content, so returning means its header ref
    /// `"epic:#1120"` is gone while the Plans panel stays active.
    ///
    /// Absence is asserted on `"epic:#1120"` rather than on the whole pane
    /// because §3a pins that the *list* comes back, not what the list looks
    /// like — and `"#38"` legitimately survives in the sidebar row either
    /// way.
    #[test]
    fn detail_pane_esc_returns_to_list() {
        let mut driver = plans_with_detail_pane();
        assert!(
            driver.screen_contains("epic:#1120"),
            "precondition: Enter must open the detail pane before Esc can \
             close it.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver.press_named(NamedKey::Escape);
        driver.render();
        assert!(
            !driver.screen_contains("epic:#1120"),
            "contract §3a: Esc must return to the list view — the detail \
             pane's \"epic:#1120\" header ref must no longer be rendered.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains(" PLANS "),
            "contract §3a: after Esc the Plans panel must still be the active \
             view (its \" PLANS \" sidebar header is rendered again).\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    // ───────────────────────────────────────────────────────────────────────
    // NOT AUTHORED — deliberately, rather than guessed.
    //
    // TODO(test-author): contract §3a says the detail pane "is the full
    // main-area content (not a sub-split of the list — the list itself is
    // replaced or pushed offscreen by the detail)". "Replaced **or** pushed
    // offscreen" are two different renders and the clause pins neither, so no
    // assertion is made about the list's absence while the pane is open. Only
    // the pane's own required strings (§3b–§3d) are checked.
    //
    // TODO(test-author): contract §3b's header table requires health — the
    // issue text spells it "health (`(warn) needs you`, blocked count)" and
    // `mocks/plans-detail-pane.screen` renders `⚠ ready_waiting   1 blocked`
    // — but §3b's own table omits health entirely, listing only the four
    // strings asserted above, and §3e closes by saying "the acceptance test
    // only verifies the strings in §3b–3d". The mock's exact wording is
    // therefore not pinned by any normative clause and is not asserted; a
    // conformant pane might render `⚠ 1 blocked`, `(warn) needs you`, or a
    // chip. Needs a contract amendment to become testable.
    //
    // TODO(test-author): contract §3f's second bullet is self-contradictory —
    // it is prefixed "**Required:**" but its body says
    // `"Open in browser"` or `"Enter=detail"` "**may** be absent". Permissive
    // language is not assertable in either direction, so only the first
    // bullet (`"Esc=back"`) is tested. Note the two readings also conflict
    // with §3d, which *requires* `"Open in browser"` in the actions row of the
    // very same screen state.
    //
    // TODO(test-author): the pre-CC-2 base status bar advertises
    // `Enter=open epic` (verified on the live render), while
    // `mocks/plans-detail-pane.screen` line 40 shows `Enter=detail`. Whether
    // CC-2 must rewrite that base-view hint is pinned nowhere in §3 — §3f
    // speaks only to the status bar "when the detail pane is open", and §4f
    // (CC-3's clause) governs the base bar. Asserting it here would be
    // inventing behaviour, so it is left out.
    //
    // TODO(test-author): contract §3c says work-order rows "are themselves
    // right-clickable (defer the row context menu to CC-3)". The row context
    // menu is explicitly deferred to #1123 and its contents are not specified
    // in §4 either, so nothing is asserted about right-clicking a work-order
    // row.
    // ───────────────────────────────────────────────────────────────────────
}
