"""#2532 (ms-67 contract §2/§5): the "Approved work items" panel's DATA SOURCE.

The TUI half of #2532 (the panel itself) is covered by the sealed ms-67
acceptance slice, which seeds a synthetic `/board` JSON fixture — so by
construction it cannot tell whether a real `coord serve` daemon ever emits
that key. That is exactly the gap these tests close, black-box, through the
same `GET /board` endpoint a thin client uses:

1. `portal.project_repos` parses + validates at LOAD (contract §2), and
   `repos_for_project` never raises on an unmapped project.
2. "approved" is the LATEST sign-off verdict, not "was ever approved".
3. `GET /board` actually carries `approved_submissions`, oldest-first, with
   every non-defaulted wire field `ApprovedSubmission` (tui/src/app/types.rs)
   requires, `repos` resolved server-side, and an unmapped project rendered
   as `[]` rather than dropped.
4. A submission whose `last_status` has moved to `planned` / `in-progress` /
   `quality-check` / `shipped` drops off the panel even though its sign-off
   verdict is still `approved` (#2660) — it has already been pulled.
   Everything else (pre-decomposition statuses, operator-set interrupts, an
   empty/unset status, anything unrecognised) stays on the list.
5. #2661: a submission that has NEVER had a signoff event of any kind also
   reaches the panel, tagged `signoff_status == "new"`, as long as its
   `last_status` is still `""`/`describing` — but NOT once a design round
   is underway (`in-design`/`awaiting-signoff`) or an operator has parked
   it (`needs-input`/`on-hold`), and NOT once it has ANY signoff history
   (a `changes_requested` submission stays off the list exactly as before
   #2661). An `"approved"` row is now also explicitly tagged
   `signoff_status == "approved"` so a client can tell the two states apart.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.config import (
    ConfigError,
    PortalConfig,
    PortalProjectRepo,
    _parse_portal,
    load as load_config,
)
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.serve_app import build_app
from tests.backends import set_board_meta

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
  - name: shared
    github: acme/shared

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api, shared]

portal:
  project_repos:
    - project_id: proj_9f2a
      repos: [api, shared]
"""


# ── 1. contract §2 — the project↔repo mapping ────────────────────────────────


class TestProjectReposConfig:
    def test_absent_block_maps_nothing_and_never_raises(self) -> None:
        cfg = _parse_portal(None)
        assert cfg.project_repos == []
        assert cfg.repos_for_project("proj_9f2a") == []

    def test_a_mapping_parses_and_resolves(self) -> None:
        cfg = _parse_portal(
            {"project_repos": [{"project_id": "proj_9f2a", "repos": ["api"]}]},
            {"api", "shared"},
        )
        assert cfg.project_repos == [
            PortalProjectRepo(project_id="proj_9f2a", repos=["api"])
        ]
        assert cfg.repos_for_project("proj_9f2a") == ["api"]

    def test_an_unmapped_project_is_not_an_error(self) -> None:
        """A brand-new portal project the operator has not routed yet is a
        valid, common state — the panel renders "no mapping", it does not
        fail."""
        cfg = _parse_portal(
            {"project_repos": [{"project_id": "proj_9f2a", "repos": ["api"]}]},
            {"api"},
        )
        assert cfg.repos_for_project("proj_nope") == []
        assert cfg.repos_for_project("") == []

    def test_returned_list_cannot_mutate_the_config(self) -> None:
        cfg = _parse_portal(
            {"project_repos": [{"project_id": "p", "repos": ["api"]}]}, {"api"}
        )
        cfg.repos_for_project("p").append("shared")
        assert cfg.repos_for_project("p") == ["api"]

    def test_duplicate_project_id_is_refused_at_load(self) -> None:
        with pytest.raises(ConfigError, match="duplicate project_id"):
            _parse_portal(
                {
                    "project_repos": [
                        {"project_id": "p", "repos": ["api"]},
                        {"project_id": "p", "repos": ["shared"]},
                    ]
                },
                {"api", "shared"},
            )

    def test_unknown_repo_name_is_refused_at_load(self) -> None:
        with pytest.raises(ConfigError, match="unknown repos"):
            _parse_portal(
                {"project_repos": [{"project_id": "p", "repos": ["nope"]}]},
                {"api"},
            )

    def test_empty_repos_list_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="non-empty list of repo names"):
            _parse_portal({"project_repos": [{"project_id": "p", "repos": []}]}, {"api"})

    def test_blank_project_id_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="non-empty string"):
            _parse_portal(
                {"project_repos": [{"project_id": "  ", "repos": ["api"]}]}, {"api"}
            )

    def test_unknown_key_inside_an_entry_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="unknown portal.project_repos"):
            _parse_portal(
                {"project_repos": [{"project_id": "p", "repos": ["api"], "rpeos": []}]},
                {"api"},
            )

    def test_not_a_list_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be a list of mappings"):
            _parse_portal({"project_repos": {"project_id": "p"}}, {"api"})

    def test_project_repos_is_a_known_portal_option(self) -> None:
        """Regression guard: `_parse_portal`'s unknown-option check derives
        its allowlist from `fields(PortalConfig)`, so a mapping declared in a
        real coordinator.yml must not trip it."""
        cfg = _parse_portal({"project_repos": []}, set())
        assert cfg == PortalConfig()


# ── 2. "approved" is the LATEST verdict ──────────────────────────────────────


def _signoff(event_id: str, submission_id: str, verdict: str) -> dict:
    """A sign-off event in the `type`-suffix shape the portal is observed to
    send (`coord.portal_sync._signoff_verdict` accepts two shapes; the nested
    one is exercised below)."""
    return {
        "id": event_id,
        "submission_id": submission_id,
        "type": f"signoff.{verdict}",
    }


class TestApprovedSubmissionIds:
    def test_an_approved_signoff_lists_the_submission(self, coord_db) -> None:
        from coord import portal_store
        from coord.approved_work import approved_submission_ids

        portal_store.record_events([_signoff("e1", "sub_a", "approved")], now=100.0)
        assert approved_submission_ids() == {"sub_a"}

    def test_the_nested_verdict_shape_is_read_too(self, coord_db) -> None:
        from coord import portal_store
        from coord.approved_work import approved_submission_ids

        portal_store.record_events(
            [
                {
                    "id": "e1",
                    "submission_id": "sub_a",
                    "type": "signoff",
                    "data": {"verdict": "approved"},
                }
            ],
            now=100.0,
        )
        assert approved_submission_ids() == {"sub_a"}

    def test_changes_requested_is_not_approved(self, coord_db) -> None:
        from coord import portal_store
        from coord.approved_work import approved_submission_ids

        portal_store.record_events(
            [_signoff("e1", "sub_a", "changes_requested")], now=100.0
        )
        assert approved_submission_ids() == set()

    def test_a_walked_back_approval_drops_off_the_list(self, coord_db) -> None:
        """The whole point of folding oldest-first: a client who approved and
        then changed their mind must not still read as ready to pull."""
        from coord import portal_store
        from coord.approved_work import approved_submission_ids

        portal_store.record_events([_signoff("e1", "sub_a", "approved")], now=100.0)
        portal_store.record_events(
            [_signoff("e2", "sub_a", "changes_requested")], now=200.0
        )
        assert approved_submission_ids() == set()

    def test_a_re_approval_after_changes_requested_counts(self, coord_db) -> None:
        from coord import portal_store
        from coord.approved_work import approved_submission_ids

        portal_store.record_events(
            [_signoff("e1", "sub_a", "changes_requested")], now=100.0
        )
        portal_store.record_events([_signoff("e2", "sub_a", "approved")], now=200.0)
        assert approved_submission_ids() == {"sub_a"}

    def test_non_signoff_events_are_ignored(self, coord_db) -> None:
        from coord import portal_store
        from coord.approved_work import approved_submission_ids

        portal_store.record_events(
            [{"id": "e1", "submission_id": "sub_a", "type": "submission.created"}],
            now=100.0,
        )
        assert approved_submission_ids() == set()


# ── 3. the wire shape, black-box through GET /board ──────────────────────────


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe file-backed coord.db override for TestClient tests
    (mirrors tests/test_fleet_health_snapshot.py's fixture of the same name —
    the autouse `coord_db` fixture's thread-bound :memory: conn is unusable
    from the ASGI worker thread TestClient runs handlers on)."""
    import coord.db as db_mod

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db_mod.override_connection(conn)
    yield conn
    db_mod.close()


@pytest.fixture
def detail_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    set_board_meta(conn, "round_number", "0")
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def portal_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _seed_submission(
    submission_id: str, *, project_id: str, first_seen_at: float, **facts
) -> None:
    from coord import portal_store

    portal_store.mirror_customer_facts(
        submission_id, {"project_id": project_id, **facts}, now=first_seen_at
    )
    portal_store.record_events(
        [_signoff(f"ev-{submission_id}", submission_id, "approved")],
        now=first_seen_at,
    )


def _set_last_status(submission_id: str, status: str, *, now: float) -> None:
    """Confirm *status* as the submission's `last_status`, bypassing the
    real push round-trip (`coord.portal_sync`'s `SUBMISSION_STATUSES`/
    `ANNOUNCING_STATUSES` machinery, which needs a fake bridge client and,
    for `quality-check`, a prior confirmed `preview` row). This module only
    reads the confirmed column — `portal_store.enqueue` + `mark_applied` is
    the direct way to land a value in it for a test."""
    from coord import portal_store

    row = portal_store.enqueue(submission_id, "status", {"status": status}, now=now)
    portal_store.mark_applied(row, now=now)


def _board(detail_db: Path, config_path: Path) -> dict:
    app = build_app(SqliteStore(detail_db), load_config(config_path))
    with TestClient(app) as cli:
        return cli.get("/board").json()


class TestBoardWiring:
    def test_board_carries_the_key_even_with_nothing_approved(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """The key is always present, so a client can tell "daemon knows
        about this and there is nothing" from "daemon predates #2532"."""
        assert _board(detail_db, portal_config_path)["approved_submissions"] == []

    def test_an_approved_submission_reaches_the_board_with_resolved_repos(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        _seed_submission(
            "sub_2f6a1c",
            project_id="proj_9f2a",
            first_seen_at=1755500040.0,
            client="Heuron Technologies",
            project_label="Portal redesign",
            outcome="Customers can self-serve a billing address change.",
            audience="Existing subscription customers",
            done_definition="Customer edits and saves a new billing address.",
            constraints="Must reuse the existing Stripe customer object.",
        )

        (row,) = _board(detail_db, portal_config_path)["approved_submissions"]

        assert row["submission_id"] == "sub_2f6a1c"
        assert row["client"] == "Heuron Technologies"
        assert row["project_id"] == "proj_9f2a"
        assert row["project_label"] == "Portal redesign"
        assert row["outcome"].startswith("Customers can self-serve")
        assert row["audience"] == "Existing subscription customers"
        assert row["done_definition"].startswith("Customer edits and saves")
        assert row["constraints"].startswith("Must reuse the existing Stripe")
        # Resolved server-side from portal.project_repos — the TUI never
        # reads coordinator.yml.
        assert row["repos"] == ["api", "shared"]
        # ISO-8601 Z, the shape tui/src/app/data.rs parses.
        assert row["received_at"] == "2025-08-18T06:54:00Z"
        # #2661: a signed-off row is explicitly tagged, so a client can tell
        # it apart from a never-touched "new" row.
        assert row["signoff_status"] == "approved"

    def test_the_real_coord_portal_146_identity_shape_resolves_end_to_end(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """coord-portal#146's confirmed `submission.created` payload sends
        `client_id`/`project_id` only — opaque ids, no display name/label of
        any kind (see docs/CUSTOMER_PORTAL.md). This seeds exactly that
        shape (not the pre-#146 `client=`/`project_label=` guess the other
        test above still checks as a fallback) and proves
        `portal.project_repos` resolves the real id to a coord repo, wired
        all the way through `GET /board`."""
        _seed_submission(
            "sub_real_shape",
            project_id="proj_9f2a",
            first_seen_at=1755500040.0,
            client_id="cli_9f2a",
            outcome="Customers can self-serve a billing address change.",
        )

        (row,) = _board(detail_db, portal_config_path)["approved_submissions"]

        assert row["submission_id"] == "sub_real_shape"
        # The confirmed wire key `client_id` is read, not just the old guess.
        assert row["client"] == "cli_9f2a"
        assert row["project_id"] == "proj_9f2a"
        # coord-portal never sends a label — this stays the empty-string
        # supported state, not an error.
        assert row["project_label"] == ""
        assert row["repos"] == ["api", "shared"]

    def test_every_non_defaulted_wire_field_is_always_present(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """`ApprovedSubmission` (tui/src/app/types.rs) deserializes these
        WITHOUT `#[serde(default)]`, so a missing key is a hard serde error
        that blanks the whole board — not a blank cell."""
        _seed_submission("sub_bare", project_id="proj_9f2a", first_seen_at=1.0)

        (row,) = _board(detail_db, portal_config_path)["approved_submissions"]
        for key in (
            "submission_id",
            "client",
            "project_id",
            "project_label",
            "outcome",
            "audience",
            "done_definition",
            "constraints",
            "repos",
            "received_at",
        ):
            assert key in row, key
        assert row["client"] == ""
        assert row["outcome"] == ""
        # #2661: new wire field, always present alongside the rest.
        assert row["signoff_status"] == "approved"

    def test_an_unmapped_project_is_listed_with_no_repos(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """Never suppressed: an operator who cannot see the unmapped
        submission cannot know to map it (contract §3c's "— no mapping —")."""
        _seed_submission("sub_unmapped", project_id="proj_new", first_seen_at=1.0)

        (row,) = _board(detail_db, portal_config_path)["approved_submissions"]
        assert row["submission_id"] == "sub_unmapped"
        assert row["repos"] == []

    def test_rows_are_oldest_first(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        _seed_submission("sub_new", project_id="proj_9f2a", first_seen_at=2000.0)
        _seed_submission("sub_old", project_id="proj_9f2a", first_seen_at=1000.0)

        rows = _board(detail_db, portal_config_path)["approved_submissions"]
        assert [r["submission_id"] for r in rows] == ["sub_old", "sub_new"]

    def test_a_submission_without_an_approved_signoff_is_absent(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        from coord import portal_store

        portal_store.mirror_customer_facts(
            "sub_pending", {"project_id": "proj_9f2a"}, now=1.0
        )
        portal_store.record_events(
            [_signoff("e1", "sub_pending", "changes_requested")], now=1.0
        )
        assert _board(detail_db, portal_config_path)["approved_submissions"] == []

    def test_a_broken_portal_read_never_blanks_the_board(
        self, detail_db, portal_config_path, rw_db, monkeypatch
    ) -> None:
        """Fail-open, same posture as `merge_plan`/`merge_staging`: this is an
        advisory panel and must never 503 the board every other panel rides
        on."""
        import coord.approved_work as aw

        monkeypatch.setattr(
            aw,
            "approved_submission_ids",
            lambda: (_ for _ in ()).throw(RuntimeError("portal DB is on fire")),
        )
        board = _board(detail_db, portal_config_path)
        assert board["approved_submissions"] == []
        assert "issues" in board  # the rest of the board is intact


# ── 4. a pulled submission drops off even though it is still "approved" ─────
# (#2660: the panel used to be an append-only "was approved" log)


class TestPulledSubmissionsDropOff:
    @pytest.mark.parametrize(
        "status", ["planned", "in-progress", "quality-check", "shipped"]
    )
    def test_a_submission_pulled_past_decomposition_drops_off(
        self, detail_db, portal_config_path, rw_db, status
    ) -> None:
        """The whole point of the panel is a FIFO backlog of work NOT yet
        pulled — an approved sign-off whose confirmed status has moved past
        `awaiting-signoff` has already been decomposed and dispatched."""
        _seed_submission("sub_pulled", project_id="proj_9f2a", first_seen_at=1.0)
        _set_last_status("sub_pulled", status, now=2.0)

        assert _board(detail_db, portal_config_path)["approved_submissions"] == []

    @pytest.mark.parametrize(
        "status",
        [
            "describing",
            "in-design",
            "awaiting-signoff",
            "needs-input",
            "on-hold",
            "some-future-status-this-module-has-never-seen",
        ],
    )
    def test_a_submission_not_yet_pulled_stays_on_the_list(
        self, detail_db, portal_config_path, rw_db, status
    ) -> None:
        """Pre-decomposition statuses, operator-set interrupts (needs-input /
        on-hold can land before decomposition just as easily as after), and
        any status this module does not recognise all stay — "never suppress
        a row you cannot explain" applies to `last_status` exactly as it
        already does to an unmapped project."""
        _seed_submission("sub_waiting", project_id="proj_9f2a", first_seen_at=1.0)
        _set_last_status("sub_waiting", status, now=2.0)

        rows = _board(detail_db, portal_config_path)["approved_submissions"]
        assert [r["submission_id"] for r in rows] == ["sub_waiting"]

    def test_an_unset_last_status_stays_on_the_list(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """The common case: a freshly-approved submission whose status has
        never been pushed at all (`last_status == ""`, the schema default)
        must not be mistaken for "pulled"."""
        _seed_submission("sub_fresh", project_id="proj_9f2a", first_seen_at=1.0)

        rows = _board(detail_db, portal_config_path)["approved_submissions"]
        assert [r["submission_id"] for r in rows] == ["sub_fresh"]

    def test_only_the_pulled_submissions_drop_leaving_the_rest(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """Mirrors the issue's own evidence table: two shipped submissions
        that should read "0 ready to pull" alongside one still-unpulled
        submission that should stay — the fix must be surgical, not blanket."""
        _seed_submission("sub_shipped_a", project_id="proj_9f2a", first_seen_at=1.0)
        _set_last_status("sub_shipped_a", "shipped", now=1.5)
        _seed_submission("sub_shipped_b", project_id="proj_9f2a", first_seen_at=2.0)
        _set_last_status("sub_shipped_b", "shipped", now=2.5)
        _seed_submission("sub_still_waiting", project_id="proj_9f2a", first_seen_at=3.0)

        rows = _board(detail_db, portal_config_path)["approved_submissions"]
        assert [r["submission_id"] for r in rows] == ["sub_still_waiting"]


# ── 5. #2661 — a request nobody has acted on reaches the panel too ──────────


def _seed_unsignoffed(
    submission_id: str, *, project_id: str, first_seen_at: float, **facts
) -> None:
    """A submission with intake done and NO signoff event of any kind —
    the #2661 "nobody has acted on this yet" case. Deliberately does not
    call `record_events` at all (unlike `_seed_submission`), so
    `_fold_signoff_verdicts()` never sees this submission id."""
    from coord import portal_store

    portal_store.mirror_customer_facts(
        submission_id, {"project_id": project_id, **facts}, now=first_seen_at
    )


class TestNewUnactionedSubmissions:
    def test_a_never_touched_submission_with_no_status_pushed_is_listed_as_new(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """The common real-world case: `last_status` is still `""` (the
        schema default — nothing has ever confirmed-pushed a status for
        this submission) and no signoff event exists at all."""
        _seed_unsignoffed(
            "sub_brand_new", project_id="proj_9f2a", first_seen_at=1.0
        )

        (row,) = _board(detail_db, portal_config_path)["approved_submissions"]
        assert row["submission_id"] == "sub_brand_new"
        assert row["signoff_status"] == "new"

    def test_a_never_touched_submission_at_describing_is_listed_as_new(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """`last_status == "describing"` is treated the same as `""` — see
        the module docstring for why nothing today ever writes the literal
        string, but the rule accepts it anyway."""
        _seed_unsignoffed(
            "sub_describing", project_id="proj_9f2a", first_seen_at=1.0
        )
        _set_last_status("sub_describing", "describing", now=2.0)

        (row,) = _board(detail_db, portal_config_path)["approved_submissions"]
        assert row["submission_id"] == "sub_describing"
        assert row["signoff_status"] == "new"

    @pytest.mark.parametrize(
        "status",
        ["in-design", "awaiting-signoff", "needs-input", "on-hold"],
    )
    def test_a_never_touched_submission_past_describing_is_not_listed(
        self, detail_db, portal_config_path, rw_db, status
    ) -> None:
        """Once `last_status` has moved past intake — a design round is
        underway, or an operator has parked it with a question/hold — an
        operator (or the process) has already acted, so this is no longer
        "nobody has acted on this" even though it was never signed off."""
        _seed_unsignoffed(
            "sub_in_flight", project_id="proj_9f2a", first_seen_at=1.0
        )
        _set_last_status("sub_in_flight", status, now=2.0)

        assert _board(detail_db, portal_config_path)["approved_submissions"] == []

    def test_a_changes_requested_submission_never_reads_as_new(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """A submission that already went through a design round and came
        back `changes_requested` has been looked at — it must not be
        mistaken for "brand new" just because `last_status` happens to
        still be `""`/`describing` (#2661 must not resurrect the pre-#2660
        "was ever touched" reading for a different field)."""
        from coord import portal_store

        _seed_unsignoffed(
            "sub_changes_requested", project_id="proj_9f2a", first_seen_at=1.0
        )
        portal_store.record_events(
            [_signoff("e1", "sub_changes_requested", "changes_requested")],
            now=1.0,
        )

        assert _board(detail_db, portal_config_path)["approved_submissions"] == []

    def test_new_and_approved_rows_interleave_oldest_first(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """The panel is one FIFO backlog across both kinds of row, not two
        separate lists — an old "new" row must outrank a younger "approved"
        one, and vice versa."""
        _seed_unsignoffed(
            "sub_new_oldest", project_id="proj_9f2a", first_seen_at=1.0
        )
        _seed_submission("sub_approved_middle", project_id="proj_9f2a", first_seen_at=2.0)
        _seed_unsignoffed(
            "sub_new_newest", project_id="proj_9f2a", first_seen_at=3.0
        )

        rows = _board(detail_db, portal_config_path)["approved_submissions"]
        assert [r["submission_id"] for r in rows] == [
            "sub_new_oldest",
            "sub_approved_middle",
            "sub_new_newest",
        ]
        assert [r["signoff_status"] for r in rows] == ["new", "approved", "new"]

    def test_a_new_row_still_carries_every_required_wire_field(
        self, detail_db, portal_config_path, rw_db
    ) -> None:
        """Same wire contract as an approved row (#2532 point 3) — a "new"
        row must not be a stripped-down shape a client has to special-case."""
        _seed_unsignoffed("sub_bare_new", project_id="proj_9f2a", first_seen_at=1.0)

        (row,) = _board(detail_db, portal_config_path)["approved_submissions"]
        for key in (
            "submission_id",
            "client",
            "project_id",
            "project_label",
            "outcome",
            "audience",
            "done_definition",
            "constraints",
            "repos",
            "received_at",
            "signoff_status",
        ):
            assert key in row, key
        assert row["repos"] == ["api", "shared"]
