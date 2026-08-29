// Sealed acceptance slice for **issue #2532** — "TUI: 'Approved work items'
// panel from the portal bridge" — milestone ms-67 (tracking issue #2530,
// "TUI portal bridge: project↔repo mapping + briefed decomposition").
//
// Authored independently from `tests/acceptance/ms-67/contract.md` (Gate A)
// and the mocks it indexes, with **zero** worker/implementation context: no
// work branch, PR or commit for #2532 was read. Every assertion below is
// derived from contract §3 (§3a–§3e) plus §5's wire shape and the three mocks
// that section indexes (`mocks/approved-items-empty.screen`,
// `mocks/approved-items-populated.screen`, `mocks/approved-items-detail.screen`)
// alone.
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-67 mock declares (contract §0):
// `driver_with_shell(app, CoordApp::shell_config(), 120, 40)`.
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2532 may run it
// (`coord acceptance run --issue 2532`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §3 only (plus the §5 wire shape §3 reads through). §2 (#2531, the
// `coordinator.yml` project↔repo mapping — config-only, no screen surface)
// and §4 (#2533, the "Pull into decomposition session" context-menu item,
// its dispatch and the Board/[Chat] redirect) are other issues' slices and
// are deliberately untouched here — including the right-click menu the
// populated mock's own status line advertises.
//
// ── Harness facts / contract gaps this slice designs around ───────────────
//
// 1. **Seeding.** Contract §3a pins that this panel has "no fetch of its
//    own" — its rows ride the existing `/board` poll — and §5 pins the
//    payload key (`approved_submissions`) and the per-row field names. The
//    fixture below therefore seeds through the existing public seam
//    `coord_tui::fixtures::make_app_with_board_json`, whose input *is* the
//    `/board` payload. Today `BoardPayload` has no `approved_submissions`
//    field, so serde ignores the key and the board comes back empty — which
//    is exactly why these tests are RED before #2532 lands.
//    TODO(test-author): contract §6.9 flags §5's field names
//    (`submission_id` / `client` / `project_id` / `project_label` /
//    `outcome` / `audience` / `done_definition` / `constraints` / `repos` /
//    `received_at`) as the contract's own *proposal*, not read off
//    coord-portal's real schema. If Gate A is amended to different names,
//    `APPROVED_JSON` below is the single place that changes.
//
// 2. **No x-offset assertions.** Contract §1 is explicit: nothing here is
//    machine-rendered ground truth, so only text presence/order is pinned,
//    never a fixed column. Every assertion below is a `screen_contains`
//    substring or a relative `find()` y-comparison — the §3c column widths
//    (12/22/16/32) are deliberately *not* asserted.
//
// 3. **Activity-bar lookup.** `TuiDriver::find` returns the *first* row
//    containing the needle, top to bottom, and `✓` is a glyph this app
//    already paints in the main content area (`icon_for_action`'s
//    `mark-refined` / `approve-gate-a` / `ready`, and a gate cell in
//    `render.rs`). A bare `find("✓")` could therefore latch onto content
//    rather than the panel icon. `activity_icon_y()` below scans **column 1
//    only** (contract §0: col 0 = accent, col 1 = activity-bar icon, col 2 =
//    `│`), so it can never mis-target.
//
// 4. **Default row selection for `Enter`.** §3e says "selecting a row and
//    pressing `Enter`", and `mocks/approved-items-detail.screen` shows the
//    *first* row (`sub_2f6a1c`) expanded with no prior navigation depicted.
//    The detail tests below therefore press `Enter` straight after
//    activating the panel, exactly as ms-33's Audit slice does for its own
//    "Entry 0 selected" mock.
//    TODO(test-author): the contract never states in words that row 0 is
//    selected by default; that reading comes from the mock alone.
//
// 5. **Row ordering.** §3c pins "oldest-first", mirroring
//    `list_submissions()`'s `ORDER BY first_seen_at ASC` — i.e. the *server*
//    already emits them in that order. The contract does not say whether the
//    TUI re-sorts a payload that arrives out of order, so `rows_render_
//    oldest_first` seeds an already-oldest-first payload and asserts only
//    that the older row renders above the newer one. That holds whether the
//    client sorts or simply preserves payload order; asserting the stronger
//    claim would invent a requirement Gate A never made.

mod approved_items_2532 {
    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::{Key, NamedKey};

    /// Two approved submissions in the §5 wire shape, oldest first (see
    /// harness note 5). Row 1 is the mapped row every §3c/§3e assertion uses
    /// (`sub_2f6a1c` → `natal-chart`); row 2 is the unmapped row §3c's
    /// `"— no mapping —"` placeholder and §3b's `"missing a repo mapping"`
    /// sidebar line key off (`repos: []`, i.e. `repos_for_project` returned
    /// empty per §2).
    ///
    /// Field values for row 1 are §5's own example payload verbatim, so the
    /// long `outcome` / `done_definition` / `constraints` strings the §3e
    /// detail pane must render untruncated are exactly the ones the contract
    /// itself drew the detail mock from.
    const APPROVED_JSON: &str = r#"{
      "approved_submissions": [
        {
          "submission_id": "sub_2f6a1c",
          "client": "Heuron Technologies",
          "project_id": "proj_9f2a",
          "project_label": "Portal redesign",
          "outcome": "Customers can self-serve a billing address change instead of emailing support.",
          "audience": "Existing subscription customers on the billing portal",
          "done_definition": "Customer edits and saves a new billing address from their account page, sees it reflected immediately, and gets a confirmation email.",
          "constraints": "Must reuse the existing Stripe customer object — no new payment fields.",
          "repos": ["natal-chart"],
          "received_at": "2026-08-18T09:14:00Z"
        },
        {
          "submission_id": "sub_77b0e4",
          "client": "Acme Ridge Logistics",
          "project_id": "proj_44de",
          "project_label": "Ops dashboard",
          "outcome": "Single dashboard for shift handover across three depots.",
          "audience": "Depot supervisors on the night shift",
          "done_definition": "A supervisor sees every open handover note for their depot on one screen.",
          "constraints": "Read-only for the first release.",
          "repos": [],
          "received_at": "2026-08-19T16:02:00Z"
        }
      ]
    }"#;

    /// The same payload with only the **mapped** row — the §3b fixture for
    /// "no row is missing a mapping, so the ⚠ line is not rendered".
    const APPROVED_JSON_ALL_MAPPED: &str = r#"{
      "approved_submissions": [
        {
          "submission_id": "sub_2f6a1c",
          "client": "Heuron Technologies",
          "project_id": "proj_9f2a",
          "project_label": "Portal redesign",
          "outcome": "Customers can self-serve a billing address change instead of emailing support.",
          "audience": "Existing subscription customers on the billing portal",
          "done_definition": "Customer edits and saves a new billing address from their account page, sees it reflected immediately, and gets a confirmation email.",
          "constraints": "Must reuse the existing Stripe customer object — no new payment fields.",
          "repos": ["natal-chart"],
          "received_at": "2026-08-18T09:14:00Z"
        }
      ]
    }"#;

    /// Zero approved submissions — §3d's empty state. Deliberately still a
    /// well-formed `/board` payload carrying the key, so the empty state is
    /// "the daemon said none", not "the fixture failed to parse".
    const APPROVED_JSON_EMPTY: &str = r#"{ "approved_submissions": [] }"#;

    fn driver_for(board_json: &str) -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_app_with_board_json(board_json);
        driver_with_shell(app, CoordApp::shell_config(), 120, 40)
    }

    /// The `y` of the activity-bar row whose **column 1** glyph is `icon`
    /// (contract §0). See harness note 3 for why this is not `find(icon)`.
    fn activity_icon_y<A: quadraui::AppLogic>(driver: &TuiDriver<A>, icon: char) -> Option<u16> {
        driver
            .screen()
            .lines()
            .enumerate()
            .find(|(_, row)| row.chars().nth(1) == Some(icon))
            .map(|(y, _)| y as u16)
    }

    /// Activate the "Approved work items" panel by clicking its `✓`
    /// activity-bar icon (contract §3a), then repaint. Fails loudly (RED) if
    /// the icon isn't registered yet — the pre-implementation failure mode
    /// for every assertion in this slice.
    fn nav_to_approved<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let y = activity_icon_y(driver, '✓').unwrap_or_else(|| {
            panic!(
                "contract §3a: the activity bar must render the '✓' \
                 \"Approved work items\" panel icon (widget id panel:approved, \
                 SidebarView::Approved) in column 1 so the panel can be \
                 activated — not found, i.e. the panel is not registered yet \
                 (#2532).\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        driver.click(1.5, y as f32 + 0.5);
        driver.render();
    }

    // ── §3a — activity-bar entry ──────────────────────────────────────────

    /// Contract §3a: a new `PanelDefinition` with icon `✓` is registered in
    /// the activity bar (columns 0–2, per §0's pinned geometry).
    #[test]
    fn activity_bar_shows_approved_panel_icon() {
        let driver = driver_for(APPROVED_JSON);
        assert!(
            activity_icon_y(&driver, '✓').is_some(),
            "contract §3a: the activity bar must render the '✓' panel icon for \
             \"Approved work items\" once panel:approved is registered in \
             shell_config().\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3a (*placement*, a distinct claim from mere presence): the
    /// new entry is appended **after** the existing `panel:queue` (`⇅`) entry
    /// and **before** the bottom-pinned `panel:settings` (`⚙`) — i.e. the
    /// 12th top-level icon. Verified purely from column-1 row indices; `⇅`
    /// and `⚙` are pre-existing anchors that already render today.
    #[test]
    fn activity_bar_places_approved_between_queue_and_settings() {
        let driver = driver_for(APPROVED_JSON);
        let queue_y = activity_icon_y(&driver, '⇅').unwrap_or_else(|| {
            panic!(
                "pre-existing anchor missing: the activity bar must render the \
                 '⇅' Queue icon.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        let settings_y = activity_icon_y(&driver, '⚙').unwrap_or_else(|| {
            panic!(
                "pre-existing anchor missing: the activity bar must render the \
                 bottom-pinned '⚙' Settings icon.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        let approved_y = activity_icon_y(&driver, '✓').unwrap_or_else(|| {
            panic!(
                "contract §3a: the '✓' Approved-work-items icon must be \
                 registered in the activity bar so its placement can be \
                 checked (#2532 not landed).\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        assert!(
            queue_y < approved_y,
            "contract §3a: '✓' must be appended AFTER (below) the '⇅' Queue \
             icon; got queue y = {queue_y}, approved y = {approved_y}\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            approved_y < settings_y,
            "contract §3a: '✓' must sit BEFORE (above) the bottom-pinned '⚙' \
             Settings icon; got approved y = {approved_y}, settings y = \
             {settings_y}\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ── §3b — sidebar aggregate reading ───────────────────────────────────

    /// Contract §3b: `screen_contains("APPROVED WORK ITEMS")` is true
    /// whenever this panel is active, regardless of row count. Asserted here
    /// on the populated fixture; `empty_state_sidebar_shows_zero_count`
    /// covers the zero-row half of "regardless".
    #[test]
    fn sidebar_shows_panel_title() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        assert!(
            driver.screen_contains("APPROVED WORK ITEMS"),
            "contract §3a/§3b: the active panel's title must read \
             \"APPROVED WORK ITEMS\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3b: the sidebar's first aggregate line is
    /// `"<N> ready to pull"` — two approved submissions are seeded.
    #[test]
    fn sidebar_shows_ready_to_pull_count() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        assert!(
            driver.screen_contains("2 ready to pull"),
            "contract §3b: with 2 approved submissions the sidebar must read \
             \"2 ready to pull\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3b: `screen_contains("missing a repo mapping")` is true iff
    /// at least one visible row's mapped-repo list is empty. `sub_77b0e4`
    /// carries `repos: []`, so the line must render (with its count).
    #[test]
    fn sidebar_flags_rows_missing_a_repo_mapping() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        assert!(
            screen.contains("missing a repo mapping"),
            "contract §3b: one seeded row has no mapped repo, so the sidebar \
             must render the \"⚠ <M> missing a repo mapping\" line.\n\
             --- screen ---\n{screen}",
        );
        assert!(
            screen.contains("1 missing a repo mapping"),
            "contract §3b: exactly one of the two seeded rows is unmapped, so \
             the count on that line must be 1.\n--- screen ---\n{screen}",
        );
    }

    /// Contract §3b (the conditional half — "only rendered when M > 0",
    /// mirroring `queue_sidebar`'s own `if s.blocked > 0`): with every
    /// visible row mapped, the ⚠ line must be absent. The positive
    /// precondition (panel active, its one row rendered) is asserted too, so
    /// this can't pass vacuously.
    #[test]
    fn sidebar_omits_missing_mapping_line_when_every_row_is_mapped() {
        let mut driver = driver_for(APPROVED_JSON_ALL_MAPPED);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        assert!(
            screen.contains("1 ready to pull"),
            "precondition (§3b): the all-mapped fixture has exactly one \
             approved submission, so the sidebar must read \"1 ready to \
             pull\".\n--- screen ---\n{screen}",
        );
        assert!(
            !screen.contains("missing a repo mapping"),
            "contract §3b: every visible row has ≥1 mapped repo, so the \
             \"missing a repo mapping\" line must NOT be rendered.\n\
             --- screen ---\n{screen}",
        );
    }

    // ── §3c — main panel row list ─────────────────────────────────────────

    /// Contract §3c: with ≥1 approved submission the list carries a header
    /// row naming all four columns.
    #[test]
    fn main_panel_renders_the_column_header_row() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        for needle in ["Submission", "Client / Project", "Repo(s)", "Outcome"] {
            assert!(
                screen.contains(needle),
                "contract §3c: the populated list's header row must contain \
                 {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    /// Contract §3c: a mapped row renders its submission reference and the
    /// repo(s) resolved server-side by §2's `repos_for_project`.
    #[test]
    fn mapped_row_shows_submission_reference_and_repo() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        for needle in ["sub_2f6a1c", "natal-chart"] {
            assert!(
                screen.contains(needle),
                "contract §3c: the mapped row must render {needle:?} \
                 verbatim.\n--- screen ---\n{screen}",
            );
        }
    }

    /// Contract §3c: a row whose `repos` is empty renders the literal string
    /// `"— no mapping —"` in the Repo(s) column — "not a blank cell, so 'no
    /// mapping' and 'not yet loaded' are never visually indistinguishable".
    #[test]
    fn unmapped_row_shows_the_no_mapping_placeholder() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        assert!(
            screen.contains("sub_77b0e4"),
            "precondition (§3c): the unmapped submission must render as a \
             row.\n--- screen ---\n{screen}",
        );
        assert!(
            screen.contains("— no mapping —"),
            "contract §3c: a row whose project has no project_repos entry (or \
             an empty repos list) must render the literal \"— no mapping —\" \
             in the Repo(s) column.\n--- screen ---\n{screen}",
        );
    }

    /// Contract §3c: rows are **oldest-first**. The seeded payload is
    /// already in that order (see harness note 5), so `sub_2f6a1c`
    /// (received 2026-08-18) must render on an earlier row than `sub_77b0e4`
    /// (received 2026-08-19).
    #[test]
    fn rows_render_oldest_first() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        let (_ox, older_y) = driver.find("sub_2f6a1c").unwrap_or_else(|| {
            panic!(
                "contract §3c: the older submission must render in the \
                 list.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        let (_nx, newer_y) = driver.find("sub_77b0e4").unwrap_or_else(|| {
            panic!(
                "contract §3c: the newer submission must render in the \
                 list.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        assert!(
            older_y < newer_y,
            "contract §3c: the list is oldest-first — sub_2f6a1c (received \
             2026-08-18, y = {older_y}) must sit ABOVE sub_77b0e4 (received \
             2026-08-19, y = {newer_y}).\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3c: the Outcome column truncates (`…` past 31 chars) — the
    /// tail of the seeded outcome must NOT be on screen in list mode. §3e's
    /// detail pane is explicitly "the one place where the 32-column Outcome
    /// truncation does not apply", so the same token appearing there
    /// (`detail_shows_untruncated_field_values`) is what makes this pair
    /// meaningful.
    ///
    /// Asserts on a single whole word past the truncation point rather than
    /// on a column count (contract §1: no x-offset assertions).
    #[test]
    fn list_mode_truncates_the_outcome_column() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        assert!(
            screen.contains("sub_2f6a1c"),
            "precondition (§3c): the mapped row must render before its \
             truncation can be judged.\n--- screen ---\n{screen}",
        );
        assert!(
            !screen.contains("emailing"),
            "contract §3c: the Outcome column truncates past 31 chars, so the \
             tail of \"…instead of emailing support.\" must not be visible in \
             list mode.\n--- screen ---\n{screen}",
        );
    }

    // ── §3d — empty state ─────────────────────────────────────────────────

    /// Contract §3d: with zero approved submissions the main panel shows one
    /// line — `"No approved work items yet — check back after a customer
    /// signs off."`
    #[test]
    fn empty_state_message() {
        let mut driver = driver_for(APPROVED_JSON_EMPTY);
        nav_to_approved(&mut driver);
        assert!(
            driver.screen_contains("No approved work items yet"),
            "contract §3d: an empty Approved-work-items panel must render \
             \"No approved work items yet — check back after a customer signs \
             off.\"\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3d: the header row is *not* drawn over an empty list ("a
    /// header with nothing under it reads as broken"). `"Client / Project"`
    /// is the header-only string §3d itself nominates for this check.
    #[test]
    fn empty_state_has_no_column_header_row() {
        let mut driver = driver_for(APPROVED_JSON_EMPTY);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        assert!(
            screen.contains("No approved work items yet"),
            "precondition (§3d): the empty-state line must be rendered before \
             the header's absence means anything.\n--- screen ---\n{screen}",
        );
        assert!(
            !screen.contains("Client / Project"),
            "contract §3d: with zero rows there is no header row, so \
             \"Client / Project\" must not appear.\n--- screen ---\n{screen}",
        );
    }

    /// Contract §3b/§3d: the sidebar count line still renders in the empty
    /// state, reading `"0 ready to pull"`, and the conditional ⚠ line does
    /// not.
    #[test]
    fn empty_state_sidebar_shows_zero_count() {
        let mut driver = driver_for(APPROVED_JSON_EMPTY);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        assert!(
            screen.contains("APPROVED WORK ITEMS"),
            "contract §3b: the panel title renders regardless of row \
             count.\n--- screen ---\n{screen}",
        );
        assert!(
            screen.contains("0 ready to pull"),
            "contract §3d: the empty-state sidebar must read \"0 ready to \
             pull\".\n--- screen ---\n{screen}",
        );
        assert!(
            !screen.contains("missing a repo mapping"),
            "contract §3d: the zero-row empty state renders no \"missing a \
             repo mapping\" line.\n--- screen ---\n{screen}",
        );
    }

    /// Contract §3d: the status-bar hint in the empty state is
    /// `" no approved submissions  q=quit "`.
    #[test]
    fn empty_state_status_bar_hint() {
        let mut driver = driver_for(APPROVED_JSON_EMPTY);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        for needle in ["no approved submissions", "q=quit"] {
            assert!(
                screen.contains(needle),
                "contract §3d: the empty-state status bar must contain \
                 {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    /// List-mode status-bar hints, per `mocks/approved-items-populated.screen`
    /// (row 39): `" j/k=nav  Enter=detail  right-click=menu  q=quit "`. The
    /// `right-click=menu` half advertises #2533's action and is asserted
    /// there, not here; this slice pins the two hints that belong to §3c/§3e
    /// navigation plus the global `q=quit`.
    #[test]
    fn list_mode_status_bar_hints() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        let screen = driver.screen();
        for needle in ["j/k=nav", "Enter=detail", "q=quit"] {
            assert!(
                screen.contains(needle),
                "contract §3c/§3e (mocks/approved-items-populated.screen row \
                 39): the list-mode status bar must contain {needle:?}.\n\
                 --- screen ---\n{screen}",
            );
        }
    }

    // ── §3e — row detail (Enter) ──────────────────────────────────────────

    /// Contract §3e: `Enter` on the selected row opens a detail region below
    /// the list, headed by Audit's `"── <Title> ──"` divider convention —
    /// here `"── Submission Detail ──"`. (Default selection = row 0, per
    /// harness note 4.)
    #[test]
    fn enter_opens_the_submission_detail() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        assert!(
            driver.screen_contains("── Submission Detail ──"),
            "contract §3e: Enter on a selected row must open a detail region \
             headed \"── Submission Detail ──\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3e: the detail shows exactly the four submission fields
    /// #2533's briefing consumes, each behind its own label.
    #[test]
    fn detail_shows_the_four_briefing_field_labels() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        let screen = driver.screen();
        for needle in ["outcome:", "audience:", "done:", "constraints:"] {
            assert!(
                screen.contains(needle),
                "contract §3e: the open detail region must render the field \
                 label {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    /// Contract §3e: the detail region also carries the identity and routing
    /// context the mock draws — `submission:`, `client:`, `project:`,
    /// `repos:`, `received:` (`mocks/approved-items-detail.screen`).
    #[test]
    fn detail_shows_identity_and_routing_context() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        let screen = driver.screen();
        for needle in ["submission:", "client:", "project:", "repos:", "received:"] {
            assert!(
                screen.contains(needle),
                "contract §3e (mocks/approved-items-detail.screen): the detail \
                 region must render the field label {needle:?}.\n\
                 --- screen ---\n{screen}",
            );
        }
        assert!(
            screen.contains("Heuron Technologies"),
            "contract §3e: `client:` shows the full client name, not the \
             list's 22-column truncation.\n--- screen ---\n{screen}",
        );
        assert!(
            screen.contains("proj_9f2a"),
            "contract §3e: `project:` shows the project id \
             (\"proj_9f2a (Portal redesign)\").\n--- screen ---\n{screen}",
        );
    }

    /// Contract §3e: "the visible text after each label matches that row's
    /// **full (untruncated)** field — the one place where §3c's 32-column
    /// Outcome truncation does not apply."
    ///
    /// Asserted with one distinctive whole word drawn from the tail of each
    /// long field. §6.2 flags the multi-line value-wrapping convention as
    /// new/unpinned, so this deliberately does not assert a whole sentence
    /// (any wrap would split it) — only that the tail words made it onto the
    /// screen at all.
    #[test]
    fn detail_shows_untruncated_field_values() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        let screen = driver.screen();
        for (field, needle) in [
            ("outcome", "emailing"),
            ("audience", "subscription"),
            ("done", "confirmation"),
            ("constraints", "Stripe"),
        ] {
            assert!(
                screen.contains(needle),
                "contract §3e: the {field} value must be shown in full — the \
                 word {needle:?} from it is missing, so the value was \
                 truncated.\n--- screen ---\n{screen}",
            );
        }
    }

    /// Contract §3e: the status-bar hint while the detail is open mirrors
    /// Audit's exactly — `"Esc=close detail"`.
    #[test]
    fn detail_mode_status_bar_hint() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        assert!(
            driver.screen_contains("Esc=close detail"),
            "contract §3e: with the detail region open the status bar must \
             show \"Esc=close detail\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §3e: `Esc` closes the detail region and the list above is
    /// unaffected — "detail is additive, not a replace".
    ///
    /// TODO(test-author): §3e says selection and scroll are unchanged across
    /// the close, but a symbols-only `screen()` cannot distinguish the
    /// selected row from an unselected one (no style in the grid), so this
    /// asserts the observable half: the divider is gone and both rows are
    /// still rendered, in their original order.
    #[test]
    fn esc_closes_the_detail_and_leaves_the_list_intact() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        driver.press(Key::Named(NamedKey::Enter));
        driver.render();
        assert!(
            driver.screen_contains("── Submission Detail ──"),
            "precondition (§3e): Enter must open the detail region before Esc \
             can close it.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver.press(Key::Named(NamedKey::Escape));
        driver.render();
        let screen = driver.screen();
        assert!(
            !screen.contains("── Submission Detail ──"),
            "contract §3e: Esc must close the detail region — its divider must \
             no longer be rendered.\n--- screen ---\n{screen}",
        );
        for needle in ["sub_2f6a1c", "sub_77b0e4", "Client / Project"] {
            assert!(
                screen.contains(needle),
                "contract §3e: closing the detail leaves the list above \
                 unaffected — {needle:?} must still be rendered.\n\
                 --- screen ---\n{screen}",
            );
        }
    }
}
