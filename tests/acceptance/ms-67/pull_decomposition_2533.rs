// Sealed acceptance slice for **issue #2533** — "TUI: pull an approved work
// item into a briefed decomposition session" — milestone ms-67 (tracking
// issue #2530, "TUI portal bridge: project↔repo mapping + briefed
// decomposition").
//
// Authored independently from `tests/acceptance/ms-67/contract.md` (Gate A)
// and the mocks it indexes, with **zero** worker/implementation context: no
// work branch, PR or commit for #2533 was read. Every assertion below is
// derived from contract §4 (§4a–§4d) plus the two mocks §7 indexes for this
// issue (`mocks/approved-items-context-menu.screen`,
// `mocks/approved-items-chat-opened.screen`), and the §5 wire shape the
// fixture is seeded through.
//
// Drives the whole app through the real `event → handle → render` path via
// quadraui's `TuiDriver` against ratatui's headless `TestBackend`, on the
// 120×40 grid every ms-67 mock declares (contract §0):
// `driver_with_shell(app, CoordApp::shell_config(), 120, 40)`.
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It compiles only under `--features test-support`.
// It is SEALED: the worker implementing #2533 may run it
// (`coord acceptance run --issue 2533`) but may not read or edit it.
//
// ── Scope ─────────────────────────────────────────────────────────────────
// Contract §4 only. §2 (#2531, the `coordinator.yml` project↔repo mapping —
// config-only, no screen surface) and §3 (#2532, the panel itself: sidebar
// aggregates, the row list, the empty state, `Enter`=detail) are other
// issues' slices and are deliberately untouched here. #2532's slice already
// lives in `tests/acceptance/ms-67/approved_items_2532.rs`; this file adds a
// second, independent module beside it. The one clause #2532's slice
// explicitly handed over — the `right-click=menu` half of the list-mode
// status bar (`mocks/approved-items-populated.screen` row 39), which
// advertises *this* issue's action — is asserted here.
//
// ── Harness facts / contract gaps this slice designs around ───────────────
//
// 1. **Seeding.** Contract §3a pins that this panel has "no fetch of its
//    own" — its rows ride the existing `/board` poll — and §5 pins the
//    payload key (`approved_submissions`) and the per-row field names. The
//    fixture below therefore seeds through the existing public seam
//    `coord_tui::fixtures::make_app_with_board_json`, whose input *is* the
//    `/board` payload. Both §4a fixtures the contract names are seeded from
//    one payload: `sub_2f6a1c` (mapped → `natal-chart`, the enabled case)
//    and `sub_77b0e4` (`repos: []`, the "— no mapping —" disabled case §4a
//    explicitly nominates as "the fixture a test-author drives to exercise
//    it").
//    TODO(test-author): contract §6.9 flags §5's field names as the
//    contract's own *proposal*, not read off coord-portal's real schema. If
//    Gate A is amended to different names, `APPROVED_JSON` below is the
//    single place that changes.
//
// 2. **No x-offset assertions.** Contract §1 is explicit: nothing in §§3–5
//    is machine-rendered ground truth, so only text presence/order is
//    pinned, never a fixed column. Every assertion below is a
//    `screen_contains` substring or a *relative* row comparison.
//
// 3. **Activity-bar lookup.** `TuiDriver::find` returns the first row
//    containing the needle, top to bottom, and `✓` is a glyph this app
//    already paints in the main content area (`icon_for_action`'s
//    `mark-refined` / `approve-gate-a` / `ready`). A bare `find("✓")` could
//    therefore latch onto content rather than the panel icon.
//    `activity_icon_y()` below scans **column 1 only** (contract §0: col 0 =
//    accent, col 1 = activity-bar icon, col 2 = `│`), so it cannot
//    mis-target. Reaching the panel at all is #2532's surface, not #2533's —
//    a failure in `nav_to_approved` means the panel this action hangs off
//    has not landed yet (the two issues' dependency order, contract §6.10),
//    not that the action is wrong.
//
// 4. **The `⇢` action icon is NOT asserted.** §4a pins `⇢` (U+21E2) as this
//    action's `icon_for_action` glyph, but
//    `mocks/approved-items-context-menu.screen` — the one mock that renders
//    this menu — draws the item as a bare label with **no** icon glyph in
//    the box. Asserting `⇢` on the grid would therefore contradict the mock
//    the contract itself indexes for this clause.
//    TODO(test-author): contract §4a/§6.3 pin the icon at the *action-table*
//    level; whether it is painted into the context-menu row is unpinned and
//    the mock says no. Left to an in-crate unit test over
//    `icon_for_action("pull-into-decomposition-session")`.
//
// 5. **"Disabled" is asserted behaviourally, not visually.** §4a's pinned
//    fact is `ContextMenuItem.disabled == true` — a struct field, invisible
//    to a symbols-only `screen()` (no style is carried in the grid).
//    `pull_item_is_inert_on_an_unmapped_row` therefore asserts the
//    observable consequence the contract states in the same breath —
//    "clicking it is a no-op" — with an explicit, currently-RED precondition
//    (the item must be *present* on the unmapped row's menu) so it can never
//    pass vacuously against an app that simply has no such menu.
//
// 6. **§4d is only partly reachable from this crate, and only the reachable
//    half is authored.** The contract splits §4d into two moments: a toast
//    that fires "**immediately on dispatch**" (its own Testable bullet:
//    "within one tick"), and — "**once the polled assignment appears**" — a
//    redirect to Board's `[Chat]` sub-tab with a bound `ChatController`. The
//    first is authored below. The second is **deliberately left
//    unauthored**: it is gated on a `type="decomposition-chat"` assignment
//    row *arriving from a later poll*, and this external integration-test
//    crate has no public seam that can make one appear after construction
//    (`make_app_with_board_json` seeds once, at build time; the bind path's
//    match key — which repo/label a `decomposition-chat` row is matched on —
//    is pinned nowhere in the contract). Writing those assertions anyway
//    would produce permanently-red tests that block the gate for a fully
//    conformant implementation — the same call ms-38's #1123 slice made for
//    its own unreachable clauses, and for the same reason. See the TODO
//    block at the bottom of this file and the authoring summary.
//
// 7. **The 2026-08-22 amendment (record the portal link) is not covered
//    here.** #2533's briefing amendment requires the dispatched session to
//    call `coord portal link` and to treat a failure as a step failure. That
//    is behaviour *inside* the `claude -p` session and its CLI surface — it
//    paints nothing onto a `TestBackend` grid, and Gate A's contract (dated
//    2026-08-21) predates the amendment and says nothing about it. It is
//    unauthorable by this driver; see the TODO block at the bottom.

mod pull_decomposition_2533 {
    use coord_tui::fixtures::make_app_with_board_json;
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};

    /// The §4a menu item's label, verbatim from contract §4a and
    /// `mocks/approved-items-context-menu.screen`.
    const PULL_ITEM: &str = "Pull into decomposition session";

    /// Two approved submissions in the §5 wire shape. `sub_2f6a1c` is the
    /// **mapped** row (`repos: ["natal-chart"]`) every "enabled" assertion
    /// drives; `sub_77b0e4` is the **unmapped** row (`repos: []`, i.e. §2's
    /// `repos_for_project` returned empty) that §4a names as the fixture for
    /// the disabled case. Row 1's field values are §5's own example payload
    /// verbatim.
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
    /// activity-bar icon (contract §3a), then repaint.
    ///
    /// This is #2532's surface, not #2533's (harness note 3): a panic here
    /// means the panel this action hangs off does not exist yet.
    fn nav_to_approved<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let y = activity_icon_y(driver, '✓').unwrap_or_else(|| {
            panic!(
                "contract §3a (#2532, this issue's dependency): the activity \
                 bar must render the '✓' \"Approved work items\" panel icon in \
                 column 1 so the panel carrying this action can be \
                 activated — not found.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        driver.click(1.5, y as f32 + 0.5);
        driver.render();
        assert!(
            driver.screen_contains("APPROVED WORK ITEMS"),
            "contract §3a/§3b (#2532, this issue's dependency): clicking the \
             '✓' activity-bar icon must activate the Approved-work-items \
             panel, whose title reads \"APPROVED WORK ITEMS\".\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Right-click the list row rendered for `submission_id`, then repaint —
    /// the real `event → handle → open_context_menu → render` chain, via
    /// `TuiDriver::right_click` (a genuine `UiEvent::MouseDown` with
    /// `MouseButton::Right`), not a direct call into any menu-building
    /// function.
    fn right_click_row<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>, submission_id: &str) {
        let (x, y) = driver.find(submission_id).unwrap_or_else(|| {
            panic!(
                "precondition (contract §3c, #2532): the approved-work-items \
                 list must render a row for {submission_id:?} before it can be \
                 right-clicked.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        driver.right_click(x, y);
        driver.render();
    }

    /// Panel active, with a right-click delivered to the given row.
    fn approved_with_row_right_click(submission_id: &str) -> TuiDriver<impl quadraui::AppLogic> {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        right_click_row(&mut driver, submission_id);
        driver
    }

    /// Click the §4a menu item (wherever it rendered) and repaint.
    fn click_pull_item<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let (x, y) = driver.find(PULL_ITEM).unwrap_or_else(|| {
            panic!(
                "contract §4a: the row context menu must contain the \
                 {PULL_ITEM:?} item before it can be activated.\n\
                 --- screen ---\n{}",
                driver.screen(),
            )
        });
        driver.click(x, y);
        driver.render();
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4a — the context-menu item
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §4a + `mocks/approved-items-populated.screen` row 39: the
    /// list-mode status bar advertises the right-click menu this issue adds
    /// (`right-click=menu`). #2532's slice deliberately left this half of the
    /// hint to #2533, since it advertises *this* action.
    #[test]
    fn list_status_bar_advertises_the_right_click_menu() {
        let mut driver = driver_for(APPROVED_JSON);
        nav_to_approved(&mut driver);
        assert!(
            driver.screen_contains("right-click=menu"),
            "contract §4a (mocks/approved-items-populated.screen row 39): the \
             Approved-work-items list status bar must advertise \
             \"right-click=menu\", the discoverability hint for this issue's \
             row action.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4a: right-clicking a row with ≥1 mapped repo opens a menu
    /// containing the item `"Pull into decomposition session"`.
    ///
    /// The label is a full phrase that appears nowhere else on any
    /// Approved-work-items screen, so it is RED while no such menu exists.
    #[test]
    fn right_click_mapped_row_offers_pull_into_decomposition_session() {
        let driver = approved_with_row_right_click("sub_2f6a1c");
        assert!(
            driver.screen_contains(PULL_ITEM),
            "contract §4a: right-clicking the mapped row (sub_2f6a1c → \
             natal-chart) must open a context menu containing the item \
             {PULL_ITEM:?} (action id pull-into-decomposition-session).\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4a: the new item is **top of the menu** ("one new item, top
    /// of the menu, mirroring the 'one primary verb, plain label'
    /// convention"), exactly as `mocks/approved-items-context-menu.screen`
    /// draws it — the item row sits directly beneath the menu box's top
    /// border, with no other item above it.
    ///
    /// Asserted structurally (contract §1: no fixed x-offsets): the row
    /// immediately above the item must be a box border — a horizontal
    /// box-drawing rule with no letters on it — rather than another menu
    /// entry. Corner-glyph variants (`┌ ╭ ┏ ╒`) are all accepted; only
    /// "there is no *item* above this one" is pinned.
    #[test]
    fn pull_item_is_the_top_menu_item() {
        let driver = approved_with_row_right_click("sub_2f6a1c");
        let screen = driver.screen();
        let item_y = screen
            .lines()
            .position(|line| line.contains(PULL_ITEM))
            .unwrap_or_else(|| {
                panic!(
                    "contract §4a: the mapped row's context menu must contain \
                     {PULL_ITEM:?} before its placement can be judged.\n\
                     --- screen ---\n{screen}",
                )
            });
        assert!(
            item_y > 0,
            "contract §4a: the menu item cannot be on row 0 — the menu's top \
             border must be above it.\n--- screen ---\n{screen}",
        );
        let above = screen.lines().nth(item_y - 1).unwrap_or_default();
        assert!(
            above.contains('─') || above.contains('━'),
            "contract §4a (mocks/approved-items-context-menu.screen): \
             {PULL_ITEM:?} must be the TOP item of the menu — the row above it \
             (row {}) must be the menu box's top border, but it is {above:?}.\n\
             --- screen ---\n{screen}",
            item_y - 1,
        );
        assert!(
            !above.chars().any(|c| c.is_alphabetic()),
            "contract §4a: {PULL_ITEM:?} must be the TOP item of the menu — no \
             other menu entry may render above it, but row {} carries text: \
             {above:?}.\n--- screen ---\n{screen}",
            item_y - 1,
        );
    }

    /// Contract §4a (the disabled case, half one): right-clicking a row whose
    /// `Repo(s)` column reads `"— no mapping —"` still shows the item —
    /// "present, greyed, inert", not hidden. A hidden item would leave the
    /// operator with no clue why the row can't be pulled.
    #[test]
    fn right_click_unmapped_row_still_shows_the_item() {
        let driver = approved_with_row_right_click("sub_77b0e4");
        let screen = driver.screen();
        assert!(
            screen.contains("— no mapping —"),
            "precondition (contract §3c, #2532): sub_77b0e4 has no mapped \
             repo, so its Repo(s) cell must read \"— no mapping —\" — that is \
             what makes it §4a's disabled fixture.\n--- screen ---\n{screen}",
        );
        assert!(
            screen.contains(PULL_ITEM),
            "contract §4a: on a row with no repo mapping the item is \
             \"present, greyed, inert\" — {PULL_ITEM:?} must still be RENDERED \
             in the menu, not omitted.\n--- screen ---\n{screen}",
        );
    }

    /// Contract §4a (the disabled case, half two): "clicking it is a no-op".
    /// Since a symbols-only grid carries no style, `ContextMenuItem.disabled
    /// == true` is asserted through its stated observable consequence — no
    /// dispatch happens (harness note 5).
    ///
    /// The precondition assert makes this non-vacuous: it fails RED today,
    /// because there is no menu at all, rather than silently "passing"
    /// because nothing happened.
    #[test]
    fn pull_item_is_inert_on_an_unmapped_row() {
        let mut driver = approved_with_row_right_click("sub_77b0e4");
        assert!(
            driver.screen_contains(PULL_ITEM),
            "precondition (contract §4a): the item must be present on the \
             unmapped row's menu before 'clicking it is a no-op' can mean \
             anything.\n--- screen ---\n{}",
            driver.screen(),
        );
        click_pull_item(&mut driver);
        let screen = driver.screen();
        assert!(
            !screen.contains("chat ready — type to start"),
            "contract §4a: the item is DISABLED for a submission with no \
             mapped repo — clicking it must be a no-op, so no \
             \"chat ready — type to start\" toast may fire.\n\
             --- screen ---\n{screen}",
        );
        assert!(
            !screen.contains("Decomposition chat →"),
            "contract §4a/§4d: clicking the disabled item must not bind a \
             decomposition ChatController.\n--- screen ---\n{screen}",
        );
        assert!(
            screen.contains("APPROVED WORK ITEMS"),
            "contract §4a: clicking the disabled item is a no-op — the \
             operator stays on the Approved-work-items panel, with no redirect \
             to Board's Chat tab.\n--- screen ---\n{screen}",
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // §4d — what the TUI shows once the action fires
    // ═══════════════════════════════════════════════════════════════════════

    /// Contract §4d, Testable bullet 1: "Triggering §4a's action →
    /// `screen_contains("chat ready — type to start")` is true (the toast)
    /// within one tick."
    ///
    /// §4d pins this toast as firing "**immediately on dispatch**" — before,
    /// and independently of, the polled-assignment bind that produces the
    /// Board/[Chat] redirect (harness note 6). One extra `render()` after the
    /// click supplies the "within one tick" slack.
    #[test]
    fn pull_action_fires_the_chat_ready_toast() {
        let mut driver = approved_with_row_right_click("sub_2f6a1c");
        click_pull_item(&mut driver);
        driver.render();
        assert!(
            driver.screen_contains("chat ready — type to start"),
            "contract §4d: triggering \"{PULL_ITEM}\" on the mapped row must \
             fire a toast reading \"…: chat ready — type to start.\" \
             immediately on dispatch (within one tick).\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4d + `mocks/approved-items-chat-opened.screen`: the toast is
    /// titled `"Decomposition chat"` and its body names the **submission**
    /// (`"sub_2f6a1c: chat ready — type to start."`) — the exact
    /// `new-issue-chat` body with the submission id substituted for the repo
    /// name.
    ///
    /// The needle is the *joined* `"sub_2f6a1c: chat ready"` precisely
    /// because `sub_2f6a1c` alone is already on screen in the list behind the
    /// toast; only the joined form proves the toast body carries it.
    #[test]
    fn pull_action_toast_is_titled_and_names_the_submission() {
        let mut driver = approved_with_row_right_click("sub_2f6a1c");
        click_pull_item(&mut driver);
        driver.render();
        let screen = driver.screen();
        assert!(
            screen.contains("Decomposition chat"),
            "contract §4d (mocks/approved-items-chat-opened.screen): the toast \
             fired by this action must be titled \"Decomposition chat\".\n\
             --- screen ---\n{screen}",
        );
        assert!(
            screen.contains("sub_2f6a1c: chat ready"),
            "contract §4d: the toast body is \"{{submission_id}}: chat ready — \
             type to start.\" — it must name the submission the operator \
             pulled (sub_2f6a1c), not a repo.\n--- screen ---\n{screen}",
        );
    }

    // ═══════════════════════════════════════════════════════════════════════
    // Deliberately UNAUTHORED clauses — do not read as "covered"
    // ═══════════════════════════════════════════════════════════════════════
    //
    // TODO(test-author): §4d's **post-bind** half — the redirect to Board +
    // its `[Chat]` sub-tab, the `ChatController` status line
    // `"  Decomposition chat → sub_2f6a1c  (Ctrl+S/Alt+Enter = send · Esc =
    // close)"`, and the "transcript area contains no turn text" claim — is
    // NOT authored here. The contract gates all three on "once the polled
    // assignment appears", i.e. on a `type="decomposition-chat"` assignment
    // row arriving from a *later* `/board` poll. This external
    // integration-test crate has no public fixture seam that can inject an
    // assignment after app construction (`make_app_with_board_json` seeds
    // once, at build time), and the contract pins neither that seam nor the
    // key such a row is matched on. Written anyway, they would be
    // permanently red against a fully conformant implementation and would
    // block the gate — the same call ms-38's #1123 slice made, for the same
    // reason. Closing this needs a contract amendment adding a public
    // "assignment appears on a later poll" fixture seam, not a guess here.
    //
    // TODO(test-author): §4c's dispatch surface — the new CLI
    // `coord portal decompose-chat <submission_id> [--machine NAME]`, the
    // `type="decomposition-chat"` string, the briefing's four submission
    // fields + mapped repos + `coordinator.yml` topology context (§4b), the
    // tool posture (`coord issue create` / `coord drive-queue add`
    // permitted), and §4c's multi-repo machine-selection refusal (itself
    // flagged as an open question in §6.7) — is invisible to a `TestBackend`
    // grid. It belongs in a `cli-pytest` slice or in coord-side unit tests,
    // not in this driver.
    //
    // TODO(test-author): the briefing's **2026-08-22 amendment** — the
    // session must call `coord portal link` for the milestone/issues it
    // creates, must treat a link failure as a step failure, and must cover
    // the one-off-issue (non-milestone) case — is not covered by any
    // assertion here, and is not covered by Gate A either: `contract.md` is
    // dated 2026-08-21 and predates the amendment, so nothing in §4 mentions
    // `portal_store.link_milestone` / `get_link_by_submission`. It is also
    // structurally out of reach of `tui-tuidriver` (it is behaviour of the
    // dispatched `claude -p` session and of coord's CLI, and paints nothing).
    // This needs a contract amendment plus a `cli-pytest` slice; see the
    // authoring summary.
}
