//! #1042 seam smoke test.
//!
//! Proves that, with the `test-support` feature enabled, an *external*
//! integration-test crate can build a [`coord_tui::CoordApp`] from
//! in-memory `BoardData` — no live daemon — exactly the way the in-crate
//! `#[cfg(test)]` suite does via `app::fixtures`. This is the harness seam
//! the oracle-loop's `tui-tuidriver` driver (docs/ORACLE_LOOP.md) assumes
//! exists at `tui/tests/acceptance.rs`.
//!
//! This is **not** the real Gate-A acceptance suite for #1039/#1040 — that
//! is independently authored later by #931's `test-author`, from #1041's
//! Gate-A contract. This file only proves the seam works.
//!
//! Run with:
//!   cargo test --test acceptance --features test-support
//! (the sealed-suite invocation adds `RUSTC_BOOTSTRAP=1 ... -- -Z
//! unstable-options --format json` for libtest JSON-lines output; see #1042
//! deliverable 4 / `coordinator.yml` `acceptance.drivers`.)
#![cfg(feature = "test-support")]

use coord_tui::fixtures::{make_test_app, BoardData};

#[test]
fn make_test_app_builds_from_board_data_with_no_live_daemon() {
    // Constructing the app — no daemon, no I/O, no panic — is the
    // assertion: it proves `app::fixtures` is reachable from an external
    // integration-test crate under the `test-support` feature.
    let app = make_test_app(BoardData::default());
    let _ = app;
}

// ── Sealed oracle suite (docs/ORACLE_LOOP.md) — DO NOT REMOVE ─────────────
// Wires each milestone's independently-authored acceptance slice under
// `tests/acceptance/ms-NN/` into this `--test acceptance` target so the
// configured `tui-tuidriver` driver command runs them. The slice files hold
// the assertions; this file only pastes them in at crate root. Paths are
// relative to this file (`tui/tests/`), so `../../` is the repo root.
include!("../../tests/acceptance/ms-33/audit_1039.rs");
include!("../../tests/acceptance/ms-38/plans_help_1124.rs");
include!("../../tests/acceptance/ms-38/plans_rightclick_1123.rs");
include!("../../tests/acceptance/ms-38/plans_detail_1122.rs");
include!("../../tests/acceptance/ms-65/board_tabs_2282.rs");
include!("../../tests/acceptance/ms-65/board_tabs_close_2283.rs");
include!("../../tests/acceptance/ms-65/pipeline_tabs_2284.rs");
include!("../../tests/acceptance/ms-65/detail_substate_2285.rs");
include!("../../tests/acceptance/ms-65/tabs_discoverability_2287.rs");
include!("../../tests/acceptance/ms-65/tabs_persistence_2286.rs");
include!("../../tests/acceptance/ms-65/board_split_2288.rs");
include!("../../tests/acceptance/ms-67/approved_items_2532.rs");
include!("../../tests/acceptance/ms-67/pull_decomposition_2533.rs");
