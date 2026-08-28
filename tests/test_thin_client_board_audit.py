"""#615/#906 regression guard: no *new* direct local-board call site may land
in ``coord/commands/*.py`` or the core coord modules below without either
(a) routing through the daemon first, or (b) being added to the matching
``ALLOWLIST`` below with a one-line justification.

## Background

``coord/commands/*.py`` (the post-#747 split of the old ``cli.py``) used to
have ~30 call sites that read/wrote the board via
``coord.state.build_board()`` / ``save_board()`` / ``load_board()``, which hit
the local SQLite DB directly.  On a thin client (no daemon-backed local DB)
those commands silently operated on an empty board (#584/#615).

Prior fixes (#590, #609, #611, #614, #747, #749, #779, #821, #905) migrated
every *reachable* call site to route through ``coord.board_service`` /
``daemon_reroute_target()`` / ``resolve_board_service()`` first.

**#906** widened the scan:

* ``BOARD_LOCAL_FUNCS`` now includes *every* non-routed board reader/writer in
  ``coord.state``: the original three (``build_board`` / ``save_board`` /
  ``load_board``) plus the newly-guarded helpers (``mark_notified``,
  ``save_plan``, ``load_dispatched``). ``get_issue_test_mode`` itself is now
  daemon-routed (mirrors ``get_test_plan``: routes to ``POST
  /issue-test-mode`` when ``board_service`` is configured, falls back to the
  private ``_get_issue_test_mode_local`` otherwise) after a review caught it
  being reachable from a thin client via ``coord resume`` -> ``reconcile()``
  — so it's no longer tracked as an unrouted local function here.

**#1493** removed ``mark_notified`` and ``load_dispatched`` from
``BOARD_FUNCS_EXTENDED`` for the same reason: both are now daemon-routed
(``mark_notified`` -> ``POST /notified``, ``load_dispatched`` -> the existing
``GET /board`` payload), not merely guarded. The #615 guard on these two had
only ever fired via callers that bypass the ``COORD_NOTIFY_ON_DAEMON``
whole-command reroute — namely ``coord.notify.post_orphaned_review_findings``
(reached directly by ``coord post-pending-reviews`` and the dashboard's
"post findings" action) and every ``load_dispatched()`` caller outside
``coord notify``'s own reroute (``coord log``/``wait``/``watch``, ``coord
status``, ``coord report-result``). Now that both functions branch on
``board_service`` internally, every existing call site (including the ones
below still shown as historical context in the allowlists) is safe
unconditionally, so they're no longer tracked here. ``save_plan`` remains
tracked — it stays merely guarded, covered only by the two call paths its own
docstring names.
* The **second test** (``test_no_unallowlisted_board_calls_in_core_modules``)
  scans the wider set of core modules beyond ``coord/commands/`` for the same
  ``BOARD_LOCAL_FUNCS`` *plus* raw ``get_connection()`` calls — the one
  escape hatch that bypasses all state-layer guards.

Both tests follow the same "fail loud on NEW additions" policy: if you add a
new call site, you must either route it through the daemon or add it to the
module-specific ``ALLOWLIST`` below with a one-line reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = REPO_ROOT / "coord" / "commands"
COORD_DIR = REPO_ROOT / "coord"

# ── #615-era board-persistence helpers ────────────────────────────────────────
BOARD_FUNCS_ORIGINAL = {"build_board", "save_board", "load_board"}

# ── #906 additions: non-routed board readers/writers added to state.py guards ─
# NOTE: get_issue_test_mode is NOT here — it's daemon-routed (like
# get_test_plan), not merely guarded, after the #906 review found it
# reachable from a thin client via `coord resume` -> reconcile().
# NOTE: mark_notified and load_dispatched are ALSO not here as of #1493 — both
# are now daemon-routed (POST /notified, GET /board respectively), for the
# same reason as get_issue_test_mode above.
BOARD_FUNCS_EXTENDED = {
    "save_plan",           # local plans write; guarded
}

# All board-local function names tracked by both tests.
BOARD_LOCAL_FUNCS = BOARD_FUNCS_ORIGINAL | BOARD_FUNCS_EXTENDED

# Raw DB escape hatch — tracked in the extended-modules test.
GET_CONNECTION = {"get_connection"}


def _find_calls(
    path: Path,
    canonical_names: set[str],
    *,
    source_modules: frozenset[str] = frozenset({"coord.state"}),
) -> set[tuple[str, str]]:
    """Return ``{(enclosing_function_name, canonical_call_name)}`` for every
    direct call to any name in *canonical_names* in *path*, resolved through
    any ``from <module> import X as Y`` alias where <module> is in
    *source_modules*.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # local (possibly aliased) name -> canonical function name.
    alias_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in source_modules:
            for alias in node.names:
                if alias.name in canonical_names:
                    alias_map[alias.asname or alias.name] = alias.name

    if not alias_map:
        return set()

    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def _enclosing_function(lineno: int) -> str:
        best = None
        for f in funcs:
            if f.lineno <= lineno <= (f.end_lineno or f.lineno):
                if best is None or f.lineno > best.lineno:
                    best = f
        return best.name if best else "<module>"

    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            canonical = alias_map.get(node.func.id)
            if canonical is not None:
                found.add((_enclosing_function(node.lineno), canonical))
    return found


# ══════════════════════════════════════════════════════════════════════════════
# Test 1: coord/commands/*.py — scans BOARD_LOCAL_FUNCS only
# ══════════════════════════════════════════════════════════════════════════════

# Every direct BOARD_LOCAL_FUNCS call left in coord/commands/*.py, and why
# it's safe.  Keyed by filename (relative to coord/commands/).
COMMANDS_ALLOWLIST: dict[str, set[tuple[str, str]]] = {
    # #584-routed: `reconcile_merges` and `merge` only reach these calls after
    # `daemon_reroute_target()` returns None (i.e. we ARE the daemon, or no
    # daemon is configured) — see the early `if _svc is not None: ...; return`
    # guard above each of these bodies. `_dispatch_conflict_fixes` (#1474
    # review: extracted so the `--only` surgical path can share the #241
    # conflict-fix dispatch the whole-queue path already had) is only ever
    # called from within `merge()`'s two bodies, both past that same guard —
    # the load_board/save_board pair just moved from `merge`'s own frame into
    # this helper's, not into a new unguarded call site.
    # `_explain_missing_only_entry` (#1695: tells "a gate blocked enqueue"
    # apart from "the identifier did not resolve" when `--only` finds no
    # queue entry) is likewise only ever called from inside `merge()`'s
    # `--only` branch, which sits below that same `if _merge_svc is not None:
    # ...; return` guard — a thin client's `--only` is forwarded to the
    # daemon's /merge and never reaches this frame. It is a read-only
    # diagnostic: load_board only, no save_board, and it swallows its own
    # exceptions so a board problem degrades the message rather than
    # replacing the real error with a traceback.
    "merge.py": {
        ("reconcile_merges", "build_board"),
        ("reconcile_merges", "save_board"),
        ("merge", "load_board"),
        ("_dispatch_conflict_fixes", "load_board"),
        ("_dispatch_conflict_fixes", "save_board"),
        ("_explain_missing_only_entry", "load_board"),
        # #1769-routed: `_apply_revalidation` is only reached from inside the
        # `merge` command body, which already returned early via
        # `daemon_reroute_target("COORD_MERGE_ON_DAEMON")` on a thin client —
        # so this re-read of the board (needed because the revalidation just
        # wrote fresh Test verdicts the in-memory board predates) only ever
        # runs on the daemon host / a standalone dev environment, exactly like
        # `merge`'s own `load_board` above.
        ("_apply_revalidation", "load_board"),
        # #2143-routed: `_reload_board_after_wait` is likewise only called from
        # the `merge` command body (both the whole-queue and `--only` paths),
        # i.e. after the same `daemon_reroute_target("COORD_MERGE_ON_DAEMON")`
        # early-return — so this post-wait re-read (the board snapshot can go
        # stale across a minutes-long CI-settle poll) only ever runs on the
        # daemon host / a standalone dev environment.
        ("_reload_board_after_wait", "load_board"),
        # #2510-routed: `_dispatch_ci_fixes` is the CI-failure sibling of
        # `_dispatch_conflict_fixes` above — it dispatches a bounded ci-fix
        # worker (or escalates to HUMAN_REQUIRED) for a CONFIRMED
        # `checks_failed` event. Like its conflict-fix twin it is only ever
        # called from inside `merge()`'s two bodies (the whole-queue path and
        # the `--only` surgical path), both of which sit below the same
        # `daemon_reroute_target("COORD_MERGE_ON_DAEMON")` early-return — so a
        # thin client's `coord merge` is forwarded to the daemon's /merge and
        # never reaches this frame. The load_board/save_board pair is the same
        # guarded pair `_dispatch_conflict_fixes` already had, not a new
        # unguarded call site.
        ("_dispatch_ci_fixes", "load_board"),
        ("_dispatch_ci_fixes", "save_board"),
    },
    # #2182-guarded: `_fetch_live_ci_gate` re-derives a `parked` entry's merge
    # gate live on the drive-queue tick (instead of waiting out the 45-minute
    # PARK_STALE_SECONDS ceiling), and needs a board to hand
    # `merge_queue.entry_gate_status`. The call is behind an explicit
    # `if resolve_board_service() is not None: return {}` early return —
    # the inverted form of dispatch_workers.py's `if svc is None:` guard
    # below — so a thin client returns before importing or calling it, and
    # only the daemon host (`coord drive-queue tick` runs there and nowhere
    # else, #1870) ever reaches the local read. Read-only: no save_board, and
    # the whole block is wrapped in a fail-soft `except Exception: return {}`
    # that degrades to the pre-#2182 ceiling rather than to a wedge.
    # #2230-guarded: `_fetch_live_blocked_gate` is the same shape for the same
    # reason, one queue state over — it re-derives a `blocked` entry's merge
    # gate live so a gate that has since cleared can resume the entry, instead
    # of leaving `blocked` terminal (quadraui#309 sat there ~11h on a merge
    # that read READY). Identical guard, identical placement: the
    # `if resolve_board_service() is not None: return {}` early return sits
    # ABOVE the `from coord.state import load_board` import, so a thin client
    # returns before the module-level read is even imported, let alone called.
    # Also read-only and wrapped in the same fail-soft `except Exception:
    # return {}`, which degrades to `plan_tick`'s board-only fallback.
    # #2276-guarded: `drive_queue_diagnose` (the read-only queue
    # diagnostician) sits behind an explicit `if is_remote(): raise
    # click.ClickException(...)` early-exit placed ABOVE the
    # `from coord.state import build_board` import — everything the command
    # reads (Phase-0 block log, queue rows, the board handed to GhLiveProbe)
    # lives on the daemon host, and its probes need `gh` (#1483), so a thin
    # client fails loud instead of confidently diagnosing an empty queue.
    # Read-only with respect to the board: build_board only, no save_board —
    # its single write is a `diagnosis` record in the Phase-0 block log.
    # #2350-guarded: `_fetch_merge_only_ready` is the third member of the
    # `_fetch_live_*` family above, with the identical guard in the identical
    # place — it confirms, for the handful of entries this tick's live gate
    # re-check just found clear, that the board ALSO already records Test
    # `passed` and Review `approve` (so Merge was the only gate ever still
    # shut, and the tick can `coord merge --only` directly instead of spending
    # a relaunch). The `if resolve_board_service() is not None: return {}`
    # early return sits ABOVE the `from coord.state import load_board` import,
    # so a thin client returns before the local read is even imported; only the
    # daemon host, where `coord drive-queue tick` runs and nowhere else
    # (#1870), reaches it. Read-only: load_board only, no save_board, and the
    # whole block is wrapped in a fail-soft `except Exception: return {}` whose
    # empty result degrades to exactly the pre-#2350 `resumed` relaunch path.
    # #2535-guarded: `_run_auto_revalidate_checks_stale` is the same shape and
    # the same guard once more — it hands a board to
    # `merge_queue.ci_revalidation_candidates` so the tick can fire the bounded
    # CI re-run for a merge-queue entry blocked solely on stale-but-green
    # checks against an already-approved review (PR #2534 sat there on nothing
    # but 210 unrelated merges having landed since its CI last ran). The
    # `if resolve_board_service() is not None: return` early return sits ABOVE
    # the `from coord.state import load_board` import, so a thin client returns
    # before the local read is even imported, let alone called; only the daemon
    # host, where `coord drive-queue tick` runs and nowhere else (#1870),
    # reaches it. Read-only with respect to the board: load_board only, no
    # save_board (its one write is to the separate merge-queue table via
    # `merge_queue.save_queue`, which the extended scan below already covers),
    # and the whole block is wrapped in a fail-soft `except Exception: return`
    # that degrades to exactly the pre-#2535 "wait for an operator" behaviour.
    "drive_queue.py": {
        ("_fetch_live_ci_gate", "load_board"),
        ("_fetch_live_blocked_gate", "load_board"),
        ("_fetch_merge_only_ready", "load_board"),
        ("_run_auto_revalidate_checks_stale", "load_board"),
        ("drive_queue_diagnose", "build_board"),
    },
    # #1337: `coord test` no longer calls save_board at all — the verdict is
    # recorded via the single-row `record_test_verdict` on both paths (it
    # self-routes to the daemon when board_service is set), so test_gate.py
    # has no direct BOARD_LOCAL_FUNCS call sites left.
    # #590-routed: build_board is in the `else:` branch of `svc =
    # resolve_board_service(); if svc is not None: ...daemon path... else:
    # ...local path...`.  On a thin client `svc` is not None, so the call is
    # not reached; `report_result` routes to the daemon's board payload.
    # (`report_result` also calls `load_dispatched` — no longer tracked here
    # since #1493 made it daemon-routed; see BOARD_FUNCS_EXTENDED above.)
    "review.py": {("report_result", "build_board")},
    # #762-routed: `diagnose`'s body already routed via `daemon_reroute_target`
    # above; this build_board() is the deliberate host-local read for the
    # already-routed body — see the "NOTE: deliberately NO save_board here"
    # comment a few lines below the call.
    # (`status` also calls `load_dispatched` — no longer tracked here since
    # #1493 made it daemon-routed; see BOARD_FUNCS_EXTENDED above.)
    "status.py": {("diagnose", "build_board")},
    # #1657-routed: `gates`'s body already routed via `daemon_reroute_target`
    # above (mirrors `diagnose` immediately above) — this build_board() is
    # the deliberate host-local read for the already-routed body.
    "gates.py": {("gates", "build_board")},
    # (`sessions.py`'s `log`/`wait`/`watch` used to appear here for
    # `load_dispatched` — no longer tracked since #1493 made it daemon-routed;
    # see BOARD_FUNCS_EXTENDED above. sessions.py has no other
    # BOARD_LOCAL_FUNCS call sites, so it no longer needs an entry here.)
    # #590/#749: informational-only local peek, gated behind
    # `if not is_remote():` — used only to print "no saved board" vs
    # "rebuilding" before the real (daemon-aware) `read_board()` call.
    "lifecycle.py": {("resume", "load_board")},
    # #590/#749: each of the five human-attended `--interactive` dispatch
    # flavors only calls the local build_board/save_board pair behind an
    # explicit `if svc is None:` guard — `record_dispatched_assignment()`
    # already routed the assignment row to the daemon when one is configured,
    # so this is the "no daemon configured / standalone dev" path.
    "dispatch_workers.py": {
        ("_dispatch_review_of", "build_board"),
        ("_dispatch_review_of", "save_board"),
        ("_dispatch_smoke_of", "build_board"),
        ("_dispatch_smoke_of", "save_board"),
        ("_dispatch_fix_of", "build_board"),
        ("_dispatch_fix_of", "save_board"),
        ("_dispatch_rework_of", "build_board"),
        ("_dispatch_rework_of", "save_board"),
        ("_dispatch_merge_of", "build_board"),
        ("_dispatch_merge_of", "save_board"),
    },
}


def test_no_unallowlisted_direct_board_calls_in_commands() -> None:
    """Guard: no new BOARD_LOCAL_FUNCS call site may land in coord/commands/*.py
    without routing through the daemon or being added to COMMANDS_ALLOWLIST."""
    actual: dict[str, set[tuple[str, str]]] = {}
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        calls = _find_calls(path, BOARD_LOCAL_FUNCS)
        if calls:
            actual[path.name] = calls

    expected = {k: v for k, v in COMMANDS_ALLOWLIST.items() if v}

    assert actual == expected, (
        "coord/commands/*.py's direct BOARD_LOCAL_FUNCS call sites changed "
        "since this test's COMMANDS_ALLOWLIST was written.\n\n"
        "If you ADDED a new direct call site: it must be routed through the "
        "daemon first (mirror `coord merge`'s daemon_reroute_target() / "
        "board_service.route_write() pattern, #615/#906) — do not call "
        "coord.state.build_board/save_board/load_board/save_plan "
        "unconditionally from a CLI command. "
        "If it's already safely guarded (e.g. behind an `if svc is None:` / "
        "`if not is_remote():` check, or only reached after a daemon-routing "
        "early-return), add it to COMMANDS_ALLOWLIST with a one-line reason.\n"
        "If you REMOVED or renamed one: delete/update its COMMANDS_ALLOWLIST entry.\n\n"
        f"expected: {expected}\n"
        f"actual:   {actual}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 2: extended core modules — scans BOARD_LOCAL_FUNCS + get_connection
# ══════════════════════════════════════════════════════════════════════════════

# Modules that can reach thin-client code paths and should never add new
# unguarded local-board reads/writes.
EXTENDED_MODULE_PATHS: list[Path] = [
    COORD_DIR / "notify.py",
    COORD_DIR / "reconcile.py",
    COORD_DIR / "issue_store.py",
    COORD_DIR / "auto_loop.py",
    COORD_DIR / "merge_queue.py",
    COORD_DIR / "interactive.py",
    COORD_DIR / "dispatch.py",
]

# Combined set tracked for extended modules.
EXTENDED_TRACKED = BOARD_LOCAL_FUNCS | GET_CONNECTION

# All source modules from which the tracked names can be imported.
EXTENDED_SOURCE_MODULES = frozenset({"coord.state", "coord.db"})

# Per-file allowlist for the extended module scan.
EXTENDED_ALLOWLIST: dict[str, set[tuple[str, str]]] = {
    # coord/notify.py — all board-local calls covered by the
    # COORD_NOTIFY_ON_DAEMON whole-command reroute (#906): on a thin client
    # `coord notify` POSTs to /notify and the daemon runs the whole function
    # against the canonical DB.
    # (notify.py's many load_dispatched()/mark_notified() call sites —
    # detect_transitions/detect_stuck/detect_needs_attention, post_transition/
    # post_stuck/post_needs_attention/post_orphaned_review_findings/
    # post_stalled_pipeline — used to be listed here individually. Both
    # functions are no longer tracked as of #1493: they're daemon-routed
    # (POST /notified, GET /board) rather than merely guarded, so every call
    # site — including post_orphaned_review_findings, which is the ONE
    # notify.py caller reachable OUTSIDE the COORD_NOTIFY_ON_DAEMON reroute
    # via `coord post-pending-reviews` and the dashboard's "post findings"
    # action — is now unconditionally safe. See BOARD_FUNCS_EXTENDED above.)
    "notify.py": {
        # save_plan: called from _try_parse_and_post_plan (inside
        # post_transition) → daemon via COORD_NOTIFY_ON_DAEMON.
        ("_try_parse_and_post_plan", "save_plan"),
        # _persist_review_verdict: raw get_connection() backstop that writes
        # review_verdict directly.  Pre-dates update_assignment_review_findings
        # routing; safe because notify.run() is daemon-rerouted (#906).
        # TODO: migrate to update_assignment_review_findings (no raw DB call).
        ("_persist_review_verdict", "get_connection"),
    },
    # coord/reconcile.py — all board-local calls run from daemon-only paths:
    #   - reconcile_completed_assignments → only called from serve_app
    #     _passive_tick (daemon tick); never from a thin-client CLI command.
    #   - reconcile_board_merges → called from `coord reconcile-merges`
    #     (COORD_RECONCILE_ON_DAEMON rerouted) or daemon tick.
    "reconcile.py": {
        # build_board in reconcile_completed_assignments: daemon tick only.
        ("reconcile_completed_assignments", "build_board"),
        # build_board in reconcile_late_agent_reports (#2547): same daemon-
        # tick-only posture as reconcile_completed_assignments above — called
        # from serve_app._passive_tick / _tick_loop right after it, never
        # from a thin-client CLI command.
        ("reconcile_late_agent_reports", "build_board"),
        # save_plan in _capture_plan_best_effort: daemon tick only.
        ("_capture_plan_best_effort", "save_plan"),
        # NOTE: reconcile() calls get_issue_test_mode(), but that function is
        # now daemon-routed itself (not in BOARD_LOCAL_FUNCS) after the #906
        # review found reconcile() runs from the thin-client-reachable
        # `coord resume`, not just the daemon tick — so no entry is needed here.
    },
    # coord/merge_queue.py — board-local and DB calls; merge queue is a
    # separate concern (its own table); all callers are daemon-side or behind
    # COORD_MERGE_ON_DAEMON reroute.
    "merge_queue.py": {
        # build_board in enqueue_approved_work: called from daemon tick or from
        # `coord merge` (COORD_MERGE_ON_DAEMON rerouted).
        ("enqueue_approved_work", "build_board"),
        # Raw get_connection calls for the merge-queue table (_mq_* rows), not
        # the board/assignments tables — separate storage concern.  All callers
        # (plan/save_queue/load_queue/drop_entry) run on the daemon side.
        ("load_queue", "get_connection"),
        ("save_queue", "get_connection"),
        ("drop_entry", "get_connection"),
        ("_load_milestones_for_queue", "get_connection"),
        # #1107 Part 3: unions merge_queue with merge_queue_archive so the
        # "already merged" dedup in enqueue_approved_work/staging_items still
        # works after housekeeping.sweep() archives a row. Same daemon-side
        # merge-queue-table seam as the others above.
        ("merged_issue_keys", "get_connection"),
    },
    # coord/issue_store.py — raw get_connection calls for the issue-store
    # seam; these write notifications/results into the DB and are called from
    # the agent's completion posting path, NOT from thin-client CLI commands.
    # The seam routes through /result and /completion endpoints on thin clients
    # (#590); the _local suffix functions are only reached on the daemon.
    "issue_store.py": {
        ("_update_local_state", "get_connection"),
        ("_assignment_type_local", "get_connection"),
        ("_record_notification", "get_connection"),
        # #990: the verdict write was factored out of _post_result_local into
        # these two named helpers (retry + readback-verify so a silent no-op
        # under SQLite lock contention can't masquerade as success) — same
        # local-DB-only seam, no new daemon-bypass.
        ("_read_review_verdict_local", "get_connection"),
        ("_persist_review_verdict", "get_connection"),
        # #886 Phase 2: Milestone Outcome Audit structured verdict — same
        # local-DB-only seam as the #990 pair above (retry + readback-verify
        # write, plus the read helpers that support it and the diff).
        ("get_audit_runs_for_epic", "get_connection"),
        ("_read_audit_run_local", "get_connection"),
        ("_persist_audit_result", "get_connection"),
        # #1956: verdict provenance (verdict_source/verdict_source_reason) —
        # same local-DB-only seam as the #990 pair above; written from
        # _post_result_local right after _persist_review_verdict, never from
        # a thin-client CLI path directly.
        ("_read_verdict_source_local", "get_connection"),
        ("_persist_verdict_source", "get_connection"),
    },
    # coord/interactive.py — raw get_connection calls for session/assignment
    # management (reading status, marking stale rows terminal).  These are
    # intrinsically local — they run against the local agent's own DB — so
    # routing them through the daemon would be wrong.
    "interactive.py": {
        ("_assignment_status", "get_connection"),
        ("reap_stale_interactive_sessions", "get_connection"),
        ("_mark_stale_reap_in_db", "get_connection"),
    },
}


def test_no_unallowlisted_board_calls_in_core_modules() -> None:
    """Guard: no new BOARD_LOCAL_FUNCS or raw get_connection() call site may
    land in the core coord modules without routing or an EXTENDED_ALLOWLIST
    entry with a justification."""
    actual: dict[str, set[tuple[str, str]]] = {}
    for path in EXTENDED_MODULE_PATHS:
        if not path.exists():
            continue
        calls = _find_calls(path, EXTENDED_TRACKED, source_modules=EXTENDED_SOURCE_MODULES)
        if calls:
            actual[path.name] = calls

    expected = {k: v for k, v in EXTENDED_ALLOWLIST.items() if v}

    assert actual == expected, (
        "Core coord modules' direct BOARD_LOCAL_FUNCS / get_connection() "
        "call sites changed since EXTENDED_ALLOWLIST was written.\n\n"
        "If you ADDED a new direct call: it must be routed through the daemon "
        "first (via board_service.route_write() or a whole-command reroute like "
        "COORD_NOTIFY_ON_DAEMON), OR added to EXTENDED_ALLOWLIST with a one-line "
        "justification explaining why it's safe.\n"
        "If you REMOVED or renamed one: delete/update its EXTENDED_ALLOWLIST entry.\n\n"
        f"expected: {expected}\n"
        f"actual:   {actual}"
    )
