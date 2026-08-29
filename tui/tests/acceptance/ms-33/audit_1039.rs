// Sealed acceptance slice for **issue #1039** — "TUI Audit panel:
// SidebarView::Audit list + dedicated /audit fetch" — milestone ms-33
// (tracking issue #1041, Audit Trail epic).
//
// Authored independently from `tests/acceptance/ms-33/contract.md` (Gate A),
// with **zero** worker/implementation context. Drives the whole app through
// the real `event → handle → render` path via quadraui's `TuiDriver` against
// ratatui's headless `TestBackend`, exactly like the in-crate `#[cfg(test)]`
// suite (docs/ORACLE_LOOP.md, coord-tui `tui-tuidriver` driver).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It is compiled only under `--features test-support`.
// It is SEALED: the worker implementing #1039 may run it
// (`coord acceptance run --issue 1039`) but may not read or edit it.
//
// ── Scope note (why this slice is deliberately narrow) ────────────────────
// The **populated-list** (contract §4a), **entry-detail pane** (§4c) and
// **count / recent-badge** (§3) behaviours require *seeding audit entries*
// into the app with no live daemon. Contract §5 states the cache field name
// is "TBD by implementor", and today `BoardData`'s fields are `pub(crate)`
// with no `Deserialize`, so an **external** integration-test crate has no
// stable public seam to seed audit rows. Those assertions are therefore
// captured as a documented TODO block at the bottom of this file rather than
// guessed — see `tests/acceptance/ms-33` summary. The tests below cover the
// part of the #1039 contract that needs **no seeding**: panel registration
// (§1/§2), sidebar header (§3), empty state (§4b) and status-bar hints (§7).
// They compile against today's public seam and are genuinely RED (they run
// and fail on assertions) until #1039 lands.

mod audit_1039 {
    use coord_tui::fixtures::{make_app_with_audit_json, make_test_app, BoardData};
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::{Key, NamedKey};

    /// A §6-shaped `/audit` response body used to seed the populated-list,
    /// entry-detail and count assertions via the contract-§5 seam
    /// `make_app_with_audit_json`. Six entries, **newest first** (`ts DESC`)
    /// exactly as `serve_app.py` orders them (contract §6). Covers every
    /// category / actor string the §4a "testable strings" table requires,
    /// plus two unique summary tokens (`ZuluNewest…` / `AlphaOldest…`) used to
    /// verify the newest-first ordering invariant without depending on the
    /// wall-clock-relative `time_ago` text.
    ///
    /// Timestamps are ~1 year in the past so `format_unix_time` always yields
    /// an `"… ago"` string (it clamps future timestamps to `0s ago`), which is
    /// what the §4a "ago substring" assertion checks — the exact minutes/hours
    /// depend on the machine clock and are deliberately not asserted.
    const AUDIT_JSON_6: &str = r#"{
      "entries": [
        {"id":6,"ts":1752156191.0,"tier":"business","category":"dispatch","event_type":"dispatched","actor":"coordinator","repo":"claude-coordinator","issue":1039,"assignment_id":"a1b2c3d4","machine":"laptop","summary":"ZuluNewest dispatched work to laptop","details":{"branch":"issue-1039-audit","type":"work"}},
        {"id":5,"ts":1752156000.0,"tier":"business","category":"test","event_type":"test_passed","actor":"user","repo":"claude-coordinator","issue":1039,"assignment_id":"b2c3d4e5","machine":"laptop","summary":"Test passed run","details":null},
        {"id":4,"ts":1752155000.0,"tier":"business","category":"review","event_type":"review_approved","actor":"worker","repo":"claude-coordinator","issue":1038,"assignment_id":"c3d4e5f6","machine":"precision","summary":"Review approved change","details":null},
        {"id":3,"ts":1752150000.0,"tier":"business","category":"merge","event_type":"merged","actor":"coordinator","repo":"claude-coordinator","issue":1036,"assignment_id":"d4e5f6a7","machine":"laptop","summary":"Merged branch to main","details":null},
        {"id":2,"ts":1752100000.0,"tier":"operational","category":"dispatch","event_type":"dispatched","actor":"coordinator","repo":"claude-coordinator","issue":1036,"assignment_id":"e5f6a7b8","machine":"precision","summary":"Dispatched to precision","details":null},
        {"id":1,"ts":1752000000.0,"tier":"operational","category":"dispatch","event_type":"dispatched","actor":"coordinator","repo":"claude-coordinator","issue":1042,"assignment_id":"f6a7b8c9","machine":"laptop","summary":"AlphaOldest dispatched laptop","details":null}
      ],
      "next_cursor": null,
      "has_more": false
    }"#;

    /// Build the app on an empty board but with the 6-entry audit page
    /// pre-seeded (contract §5 seam), and hand back a driver on the 120×40 grid
    /// every mock declares. No live daemon, no background fetch thread — the
    /// `audit_page` cache is populated directly from `AUDIT_JSON_6`.
    fn populated_audit_driver() -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_app_with_audit_json(BoardData::default(), AUDIT_JSON_6);
        driver_with_shell(app, CoordApp::shell_config(), 120, 40)
    }

    /// Build the app on an empty board and hand back a driver on a
    /// 120×40 grid — the fixture surface every contract mock declares
    /// (`driver_with_shell(app, CoordApp::shell_config(), 120, 40)`).
    fn empty_audit_driver() -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_test_app(BoardData::default());
        driver_with_shell(app, CoordApp::shell_config(), 120, 40)
    }

    /// Activate the Audit panel by clicking its activity-bar icon, then
    /// repaint. Fails loudly (RED) if the `§` icon isn't rendered yet — i.e.
    /// `panel:audit` / `SidebarView::Audit` has not been registered. This is
    /// the pre-implementation failure mode for every audit-view assertion.
    fn nav_to_audit<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let (x, y) = driver.find("§").expect(
            "contract §1/§2: activity bar must render the '§' audit icon so \
             the Audit panel can be activated — not found, meaning \
             panel:audit / SidebarView::Audit is not registered yet (#1039)",
        );
        assert!(
            x < 3.0,
            "contract §2: the '§' audit icon must live in the activity-bar \
             columns 0–2 (x < 3.0); found x = {x}",
        );
        driver.click(x, y);
        driver.render();
    }

    /// Contract §1 (panel registration) + §2 (activity-bar rendered text):
    /// the `§` audit icon must appear in the activity bar (columns 0–2).
    #[test]
    fn activity_bar_shows_section_icon() {
        let driver = empty_audit_driver();
        let pos = driver.find("§");
        assert!(
            pos.is_some(),
            "contract §1/§2: the activity bar must render the '§' audit panel \
             icon once panel:audit is registered in shell_config(); not found",
        );
        let (x, _y) = pos.unwrap();
        assert!(
            x < 3.0,
            "contract §2: '§' must be within activity-bar columns 0–2 \
             (x < 3.0); found x = {x}",
        );
    }

    /// Contract §1 (panel registration — *placement*): the Audit panel is
    /// registered "After `panel:sessions` (◉), before the bottom-pinned
    /// `panel:settings` (⚙)". That is a distinct claim from mere presence
    /// (covered by `activity_bar_shows_section_icon`): the `§` icon must sit
    /// **below** the sessions icon and **above** the bottom-pinned settings
    /// icon in the activity-bar column. Verified purely from `find()`
    /// y-coordinates — no audit seeding required — so it runs and fails RED
    /// (`§` absent) until `panel:audit` is registered in the correct slot.
    #[test]
    fn activity_bar_panel_order() {
        let driver = empty_audit_driver();

        // Sessions (◉) and Settings (⚙) already exist in shell_config() today;
        // they anchor the required slot. Audit (§) is the one that must appear
        // between them once #1039 registers it.
        let (sx, sy) = driver.find("◉").expect(
            "activity bar must render the '◉' sessions icon (pre-existing anchor)",
        );
        let (audit_x, audit_y) = driver.find("§").expect(
            "contract §1: the '§' audit icon must be registered in the activity \
             bar so its placement can be checked; not found (#1039 not landed)",
        );
        let (gx, gy) = driver.find("⚙").expect(
            "activity bar must render the '⚙' settings icon (pre-existing, \
             bottom-pinned anchor)",
        );

        // All three live in the activity-bar columns (0–2).
        for (label, x) in [("◉", sx), ("§", audit_x), ("⚙", gx)] {
            assert!(
                x < 3.0,
                "contract §1: the {label} activity-bar icon must be within \
                 columns 0–2 (x < 3.0); found x = {x}",
            );
        }

        // Ordering: sessions above audit, audit above the bottom-pinned settings.
        assert!(
            sy < audit_y,
            "contract §1: '§' audit must be positioned AFTER (below) the '◉' \
             sessions icon; got sessions y = {sy}, audit y = {audit_y}\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            audit_y < gy,
            "contract §1: '§' audit must be positioned BEFORE (above) the \
             bottom-pinned '⚙' settings icon; got audit y = {audit_y}, \
             settings y = {gy}\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3: with the Audit panel active, the sidebar header (rendered
    /// by `ShellConfig.with_status_bar()` from `title: \"AUDIT\"`) shows the
    /// padded title `\" AUDIT \"`.
    #[test]
    fn sidebar_header_shows_title() {
        let mut driver = empty_audit_driver();
        nav_to_audit(&mut driver);
        assert!(
            driver.screen_contains(" AUDIT "),
            "contract §3: the sidebar header must show \" AUDIT \" when \
             SidebarView::Audit is active.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4b (empty-state edge case): on an empty board — no audit
    /// entries, fetch not (or never) completed — the main content area shows
    /// `\"No audit events yet.\"`.
    #[test]
    fn empty_state_message() {
        let mut driver = empty_audit_driver();
        nav_to_audit(&mut driver);
        assert!(
            driver.screen_contains("No audit events yet."),
            "contract §4b: an empty Audit panel must render \
             \"No audit events yet.\" in the main content area.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §7: status-bar hints shown when the Audit view is active and
    /// no detail pane is open. `Enter=detail` and `r=refresh` are audit-
    /// specific; `j/k=nav` and `q=quit` complete the required set.
    #[test]
    fn status_bar_hints_list_mode() {
        let mut driver = empty_audit_driver();
        nav_to_audit(&mut driver);
        let screen = driver.screen();
        for needle in ["j/k=nav", "Enter=detail", "r=refresh", "q=quit"] {
            assert!(
                screen.contains(needle),
                "contract §7: Audit list-mode status bar must contain {needle:?}\
                 .\n--- screen ---\n{screen}",
            );
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // Seeded assertions (contract §5 seam `make_app_with_audit_json`, pinned by
    // the 2026-07-12 / #1095 amendment). Authored in the JIT round the §5 note
    // explicitly calls for: the §3-count / §4a-populated / §4c-detail / §7-
    // detail-hint behaviours that the original slice deferred while the seam
    // name was "TBD by implementor".
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §4a: with a non-empty `AuditPage`, each row shows the
    /// `entry.category` verbatim. The §4a "testable strings" table requires at
    /// least one row of each of `dispatch` / `test` / `review` / `merge` — all
    /// present in `AUDIT_JSON_6`.
    #[test]
    fn populated_list_shows_categories() {
        let mut driver = populated_audit_driver();
        nav_to_audit(&mut driver);
        let screen = driver.screen();
        for needle in ["dispatch", "test", "review", "merge"] {
            assert!(
                screen.contains(needle),
                "contract §4a: the populated audit list must render the \
                 category string {needle:?} verbatim in a row.\n\
                 --- screen ---\n{screen}",
            );
        }
    }

    /// Contract §4a: each row shows `entry.actor` verbatim. The seeded page has
    /// `coordinator` actors (the §4a table's named "actor string" example).
    #[test]
    fn populated_list_shows_actor() {
        let mut driver = populated_audit_driver();
        nav_to_audit(&mut driver);
        assert!(
            driver.screen_contains("coordinator"),
            "contract §4a: the populated audit list must render the actor \
             string \"coordinator\" verbatim.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4a: the `time_ago` column is `format_unix_time(entry.ts)`,
    /// which the §4a table pins to a string ending in `" ago"`. Every seeded
    /// entry is in the past, so at least one `"ago"` must appear.
    #[test]
    fn populated_list_rows_are_relative_time() {
        let mut driver = populated_audit_driver();
        nav_to_audit(&mut driver);
        assert!(
            driver.screen_contains("ago"),
            "contract §4a: every audit row's relative-time column is rendered \
             by format_unix_time() and must contain the substring \"ago\".\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4a: **newest first** ordering is invariant (highest `ts` at
    /// the top). `AUDIT_JSON_6` carries a newest entry summarised
    /// `"ZuluNewest…"` (ts 1752156191) and an oldest `"AlphaOldest…"`
    /// (ts 1752000000); the newest token must render on an *earlier* row
    /// (smaller `y`) than the oldest. This depends only on row order, not on
    /// the wall clock.
    #[test]
    fn populated_list_newest_first() {
        let mut driver = populated_audit_driver();
        nav_to_audit(&mut driver);
        let (_nx, newest_y) = driver.find("ZuluNewest").unwrap_or_else(|| {
            panic!(
                "contract §4a: the newest entry's summary token \"ZuluNewest\" \
                 must render in the audit list.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        let (_ox, oldest_y) = driver.find("AlphaOldest").unwrap_or_else(|| {
            panic!(
                "contract §4a: the oldest entry's summary token \"AlphaOldest\" \
                 must render in the audit list.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        assert!(
            newest_y < oldest_y,
            "contract §4a: audit list must be newest-first — the highest-ts \
             entry (ZuluNewest, y = {newest_y}) must sit ABOVE the lowest-ts \
             entry (AlphaOldest, y = {oldest_y}).\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3 (count line): the sidebar shows a decimal integer
    /// immediately followed by `" entries"`. The exact integer is left to the
    /// implementor — see the TODO below on why the value isn't asserted — so
    /// this pins the required `" entries"` label rather than a specific count.
    #[test]
    fn sidebar_count_line() {
        let mut driver = populated_audit_driver();
        nav_to_audit(&mut driver);
        // TODO(test-author): contract §3 says the count is "a decimal integer
        // immediately followed by ' entries'" and the §5 amendment pins
        // AuditPage to the §6 wire shape — which has NO total-count field
        // (only `entries` / `next_cursor` / `has_more`). The populated mock
        // shows "42 entries" over 6 visible rows, so the integer is ambiguous
        // (page length 6 vs. some server total 42, which §6 doesn't carry).
        // The exact number is therefore NOT asserted; only the required label.
        assert!(
            driver.screen_contains(" entries"),
            "contract §3: the Audit sidebar must render a count line ending in \
             \" entries\" (e.g. \"6 entries\").\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4c: pressing **Enter** on the selected entry opens an inline
    /// detail pane in the main content area showing the full entry fields. All
    /// six required labels from §4c must appear.
    ///
    /// The default selection is entry 0 (the newest), per the detail mock
    /// (`mocks/audit-panel-detail.screen`, "Entry 0 selected").
    #[test]
    fn entry_detail_pane_on_enter() {
        let mut driver = populated_audit_driver();
        nav_to_audit(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        let screen = driver.screen();
        for needle in [
            "Entry Detail",
            "ts:",
            "category:",
            "event_type:",
            "actor:",
            "summary:",
            "details:",
        ] {
            assert!(
                screen.contains(needle),
                "contract §4c: the entry-detail pane (opened with Enter) must \
                 render the field label {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    /// Contract §4c: **Esc** closes the detail pane and returns to the
    /// list-only view. After closing, the `"Entry Detail"` header must be gone
    /// and the list content (a seeded category row) must be visible again.
    #[test]
    fn entry_detail_esc_closes() {
        let mut driver = populated_audit_driver();
        nav_to_audit(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        assert!(
            driver.screen_contains("Entry Detail"),
            "precondition: Enter must open the detail pane before Esc can close \
             it.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver.press(Key::Named(NamedKey::Escape));
        driver.render();
        assert!(
            !driver.screen_contains("Entry Detail"),
            "contract §4c: Esc must close the detail pane — the \"Entry Detail\" \
             header must no longer be rendered.\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains("dispatch"),
            "contract §4c: after Esc the list-only view must be shown again \
             (audit rows visible).\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §7 (detail mode): while the detail pane is open the status bar
    /// shows `"Esc=close detail"`.
    #[test]
    fn detail_mode_status_hint() {
        let mut driver = populated_audit_driver();
        nav_to_audit(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        assert!(
            driver.screen_contains("Esc=close detail"),
            "contract §7: with the detail pane open, the status bar must show \
             \"Esc=close detail\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ───────────────────────────────────────────────────────────────────────
    // STILL DEFERRED — blocked on a seam the contract does NOT (yet) pin.
    //
    // TODO(test-author): the §3 **recent badge** ("<N> recent", N > 0) is
    // driven by `/board`'s `audit_recent_count` (contract §3/§6), which lands
    // on `BoardData` — whose fields are `pub(crate)`. An *external* acceptance
    // crate can only build `BoardData::default()` (audit_recent_count == 0), so
    // the badge-present (N > 0) case can't be seeded through the pinned seams:
    // `make_app_with_audit_json(data, audit_json)` seeds only the /audit page,
    // and `data: BoardData` has no public field to set the recent count. The
    // §3 badge-ABSENT case (0 recent / omitted) is the default and is implied
    // by the populated tests above not requiring a badge; the badge-PRESENT
    // case is left unauthored rather than guessed. If a future contract round
    // pins a seam for it (e.g. `make_app_with_audit_json` also taking a recent
    // count, or a `pub` audit_recent_count on BoardData), add:
    //   * sidebar_recent_badge  — screen_contains("7 recent")            (§3)
    //
    // The exact **count integer** (§3) is likewise unpinned — see the
    // `sidebar_count_line` in-body TODO: §6's wire shape carries no total, so
    // whether the line reads the page length or a server total is ambiguous;
    // only the " entries" label is asserted.
    // ───────────────────────────────────────────────────────────────────────
}
