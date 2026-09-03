"""#2507: milestone <-> portal submission_id linkage.

Covers the domain layer (`coord.portal_store.PortalLink` + its wrapper
functions) and the underlying board_meta persistence in `coord.state`, the
same split `tests/test_gate_a.py`'s `TestPersistence` covers for
`GateAApproval` / `save_gate_a_approval` — this is the analogous seam, one
level over in the portal bridge.
"""

from __future__ import annotations

import pytest


class TestPortalLinkFromDict:
    def test_round_trips_through_to_dict(self) -> None:
        from coord.portal_store import PortalLink

        link = PortalLink(
            repo_name="acme-portal",
            milestone_number=3,
            submission_id="sub_abc123",
            linked_at=1000.0,
            actor="john",
        )
        again = PortalLink.from_dict(link.to_dict())
        assert again == link

    def test_rejects_not_a_dict(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(None) is None
        assert PortalLink.from_dict("sub_abc123") is None

    def test_rejects_missing_repo_name(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {"milestone_number": 3, "submission_id": "sub_1"}
        ) is None

    def test_rejects_missing_milestone_number(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {"repo_name": "acme-portal", "submission_id": "sub_1"}
        ) is None

    def test_rejects_missing_submission_id(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {"repo_name": "acme-portal", "milestone_number": 3}
        ) is None

    def test_rejects_a_newer_schema(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {
                "repo_name": "acme-portal",
                "milestone_number": 3,
                "submission_id": "sub_1",
                "schema": 999,
            }
        ) is None

    def test_tolerates_a_stringy_milestone_number(self) -> None:
        from coord.portal_store import PortalLink

        link = PortalLink.from_dict(
            {
                "repo_name": "acme-portal",
                "milestone_number": "3",
                "submission_id": "sub_1",
            }
        )
        assert link is not None
        assert link.milestone_number == 3


class TestPortalLinkIssueScoped:
    """#2665: a one-off issue decomposition (no milestone) links via
    ``issue_number`` instead — exactly one of ``milestone_number`` /
    ``issue_number`` is ever set."""

    def test_round_trips_through_to_dict(self) -> None:
        from coord.portal_store import PortalLink

        link = PortalLink(
            repo_name="acme-portal",
            issue_number=42,
            submission_id="sub_abc123",
            linked_at=1000.0,
            actor="john",
        )
        again = PortalLink.from_dict(link.to_dict())
        assert again == link
        assert again.milestone_number is None

    def test_rejects_both_milestone_number_and_issue_number(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {
                "repo_name": "acme-portal",
                "milestone_number": 3,
                "issue_number": 42,
                "submission_id": "sub_1",
            }
        ) is None

    def test_rejects_neither_milestone_number_nor_issue_number(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {"repo_name": "acme-portal", "submission_id": "sub_1"}
        ) is None

    def test_an_old_milestone_only_row_still_decodes_unchanged(self) -> None:
        """The dict-shape widening must not disturb a record written before
        #2665, which never had an ``issue_number`` key at all."""
        from coord.portal_store import PortalLink

        link = PortalLink.from_dict(
            {
                "repo_name": "acme-portal",
                "milestone_number": 3,
                "submission_id": "sub_1",
            }
        )
        assert link is not None
        assert link.milestone_number == 3
        assert link.issue_number is None

    def test_target_desc(self) -> None:
        from coord.portal_store import PortalLink

        ms_link = PortalLink(repo_name="r", milestone_number=3, submission_id="s")
        issue_link = PortalLink(repo_name="r", issue_number=42, submission_id="s")
        assert ms_link.target_desc == "ms-3"
        assert issue_link.target_desc == "issue #42"


class TestLinkMilestone:
    def test_link_then_get(self, coord_db) -> None:
        from coord.portal_store import get_milestone_link, link_milestone

        link_milestone(
            repo_name="acme-portal",
            milestone_number=3,
            submission_id="sub_abc123",
            actor="john",
            now=1000.0,
        )
        found = get_milestone_link(repo_name="acme-portal", milestone_number=3)
        assert found is not None
        assert found.submission_id == "sub_abc123"
        assert found.actor == "john"
        assert found.linked_at == 1000.0

    def test_get_returns_none_when_unlinked(self, coord_db) -> None:
        from coord.portal_store import get_milestone_link

        assert get_milestone_link(repo_name="acme-portal", milestone_number=3) is None

    def test_relink_overwrites_not_appends(self, coord_db) -> None:
        from coord.portal_store import (
            get_milestone_link,
            link_milestone,
            list_milestone_links,
        )

        link_milestone(
            repo_name="acme-portal", milestone_number=3, submission_id="sub_typo"
        )
        link_milestone(
            repo_name="acme-portal", milestone_number=3, submission_id="sub_fixed"
        )
        links = [
            link
            for link in list_milestone_links()
            if link.repo_name == "acme-portal" and link.milestone_number == 3
        ]
        assert len(links) == 1
        assert links[0].submission_id == "sub_fixed"
        assert (
            get_milestone_link(repo_name="acme-portal", milestone_number=3).submission_id
            == "sub_fixed"
        )

    def test_different_milestones_coexist(self, coord_db) -> None:
        from coord.portal_store import get_milestone_link, link_milestone

        link_milestone(
            repo_name="acme-portal", milestone_number=3, submission_id="sub_ms3"
        )
        link_milestone(
            repo_name="acme-portal", milestone_number=9, submission_id="sub_ms9"
        )
        assert (
            get_milestone_link(repo_name="acme-portal", milestone_number=3).submission_id
            == "sub_ms3"
        )
        assert (
            get_milestone_link(repo_name="acme-portal", milestone_number=9).submission_id
            == "sub_ms9"
        )

    def test_different_repos_do_not_collide(self, coord_db) -> None:
        from coord.portal_store import get_milestone_link, link_milestone

        link_milestone(repo_name="repo-a", milestone_number=3, submission_id="sub_a")
        link_milestone(repo_name="repo-b", milestone_number=3, submission_id="sub_b")
        assert (
            get_milestone_link(repo_name="repo-a", milestone_number=3).submission_id
            == "sub_a"
        )
        assert (
            get_milestone_link(repo_name="repo-b", milestone_number=3).submission_id
            == "sub_b"
        )


class TestLinkIssue:
    """#2665: the one-off-issue counterpart to ``TestLinkMilestone`` — a
    decomposition that produced a single milestone-less issue."""

    def test_link_then_get(self, coord_db) -> None:
        from coord.portal_store import get_issue_link, link_issue

        link_issue(
            repo_name="acme-portal",
            issue_number=42,
            submission_id="sub_abc123",
            actor="john",
            now=1000.0,
        )
        found = get_issue_link(repo_name="acme-portal", issue_number=42)
        assert found is not None
        assert found.submission_id == "sub_abc123"
        assert found.actor == "john"
        assert found.linked_at == 1000.0
        assert found.milestone_number is None

    def test_get_returns_none_when_unlinked(self, coord_db) -> None:
        from coord.portal_store import get_issue_link

        assert get_issue_link(repo_name="acme-portal", issue_number=42) is None

    def test_relink_overwrites_not_appends(self, coord_db) -> None:
        from coord.portal_store import get_issue_link, link_issue, list_milestone_links

        link_issue(repo_name="acme-portal", issue_number=42, submission_id="sub_typo")
        link_issue(repo_name="acme-portal", issue_number=42, submission_id="sub_fixed")
        links = [
            link
            for link in list_milestone_links()
            if link.repo_name == "acme-portal" and link.issue_number == 42
        ]
        assert len(links) == 1
        assert links[0].submission_id == "sub_fixed"
        assert (
            get_issue_link(repo_name="acme-portal", issue_number=42).submission_id
            == "sub_fixed"
        )

    def test_a_milestone_link_and_a_same_numbered_issue_link_coexist(
        self, coord_db
    ) -> None:
        """ms-3 and issue #3 are different keys — no cross-talk (#2665)."""
        from coord.portal_store import get_issue_link, get_milestone_link, link_issue, link_milestone

        link_milestone(repo_name="acme-portal", milestone_number=3, submission_id="sub_ms3")
        link_issue(repo_name="acme-portal", issue_number=3, submission_id="sub_issue3")
        assert (
            get_milestone_link(repo_name="acme-portal", milestone_number=3).submission_id
            == "sub_ms3"
        )
        assert (
            get_issue_link(repo_name="acme-portal", issue_number=3).submission_id
            == "sub_issue3"
        )


class TestGetLinkBySubmission:
    """The reverse lookup #2509's verdict consumer needs: an inbound event
    carries only `submission_id` and must resolve back to `(repo,
    milestone)` — or, since #2665, `(repo, issue)`."""

    def test_finds_the_link_by_submission_id(self, coord_db) -> None:
        from coord.portal_store import get_link_by_submission, link_milestone

        link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id="sub_xyz"
        )
        found = get_link_by_submission("sub_xyz")
        assert found is not None
        assert found.repo_name == "acme-portal"
        assert found.milestone_number == 5

    def test_finds_an_issue_scoped_link_by_submission_id(self, coord_db) -> None:
        from coord.portal_store import get_link_by_submission, link_issue

        link_issue(repo_name="acme-portal", issue_number=42, submission_id="sub_xyz")
        found = get_link_by_submission("sub_xyz")
        assert found is not None
        assert found.repo_name == "acme-portal"
        assert found.issue_number == 42
        assert found.milestone_number is None

    def test_unknown_submission_id_is_a_clean_none(self, coord_db) -> None:
        from coord.portal_store import get_link_by_submission

        assert get_link_by_submission("nope") is None

    def test_does_not_confuse_two_different_submissions(self, coord_db) -> None:
        from coord.portal_store import get_link_by_submission, link_milestone

        link_milestone(repo_name="repo-a", milestone_number=1, submission_id="sub_a")
        link_milestone(repo_name="repo-b", milestone_number=2, submission_id="sub_b")
        assert get_link_by_submission("sub_a").repo_name == "repo-a"
        assert get_link_by_submission("sub_b").repo_name == "repo-b"


class TestEventsForSubmission:
    """#2659: the read half of the `coord portal remirror` backfill — the
    full, undamaged event history a clobbered mirror is reconstructed from.
    """

    def test_empty_when_no_events_pulled(self, coord_db) -> None:
        from coord.portal_store import events_for_submission

        assert events_for_submission("sub_1") == []

    def test_empty_submission_id_is_a_clean_empty_list(self, coord_db) -> None:
        from coord.portal_store import events_for_submission

        assert events_for_submission("") == []

    def test_returns_oldest_first(self, coord_db) -> None:
        from coord.portal_store import events_for_submission, record_events

        record_events(
            [
                {"id": "e2", "submission_id": "sub_1", "type": "b", "at": "t2"},
                {"id": "e1", "submission_id": "sub_1", "type": "a", "at": "t1"},
            ]
        )
        events = events_for_submission("sub_1")
        # #2723: both rows share the same `received_at` stamp (one
        # `record_events` call = one `time.time()` read for the whole page),
        # so this exercises the tiebreak. Real pulls arrive on separate ticks
        # and so get distinct, ordering `received_at`s — the tiebreak only
        # ever matters for same-page rows like these two. Pre-#2723 this
        # broke on SQLite's implicit `rowid` (insertion order: "e2", "e1");
        # Postgres has no `rowid`, so the tiebreak is now `event_id` (the
        # table's own PK) — a real ordering change, called out in #2723.
        assert [e.event_id for e in events] == ["e1", "e2"]

    def test_only_returns_the_named_submission(self, coord_db) -> None:
        from coord.portal_store import events_for_submission, record_events

        record_events(
            [
                {"id": "e1", "submission_id": "sub_1", "type": "a"},
                {"id": "e2", "submission_id": "sub_2", "type": "a"},
            ]
        )
        events = events_for_submission("sub_1")
        assert len(events) == 1
        assert events[0].event_id == "e1"


class TestAllEventSubmissionIds:
    def test_empty_by_default(self, coord_db) -> None:
        from coord.portal_store import all_event_submission_ids

        assert all_event_submission_ids() == []

    def test_deduplicates_and_sorts(self, coord_db) -> None:
        from coord.portal_store import all_event_submission_ids, record_events

        record_events(
            [
                {"id": "e1", "submission_id": "sub_b", "type": "a"},
                {"id": "e2", "submission_id": "sub_a", "type": "a"},
                {"id": "e3", "submission_id": "sub_b", "type": "a"},
            ]
        )
        assert all_event_submission_ids() == ["sub_a", "sub_b"]


class TestReplaceCustomerJson:
    """#2659: the write half — rebuild from empty, not merge, which is the
    whole point of the backfill (a merge would leave a stale `"payload"` key
    sitting next to the freshly-derived facts)."""

    def test_creates_the_submission_row_if_missing(self, coord_db) -> None:
        from coord.portal_store import get_submission, replace_customer_json

        replace_customer_json("sub_1", {"outcome": "x"})
        record = get_submission("sub_1")
        assert record is not None
        assert record.customer == {"outcome": "x"}

    def test_overwrites_rather_than_merges(self, coord_db) -> None:
        from coord.portal_store import (
            get_submission,
            mirror_customer_facts,
            replace_customer_json,
        )

        mirror_customer_facts("sub_1", {"payload": {"verdict": "approved"}})
        replace_customer_json("sub_1", {"outcome": "x", "verdict": "approved"})
        record = get_submission("sub_1")
        assert record is not None
        assert record.customer == {"outcome": "x", "verdict": "approved"}
        assert "payload" not in record.customer

    def test_empty_submission_id_is_a_no_op(self, coord_db) -> None:
        from coord.portal_store import list_submissions, replace_customer_json

        replace_customer_json("", {"outcome": "x"})
        assert list_submissions() == []


class TestStatePersistenceDirect:
    """The board_meta seam itself — `coord.state`'s half, exercised the same
    way `TestPersistence` in `tests/test_gate_a.py` exercises the sibling
    `gate_a_approvals` seam."""

    def test_record_without_a_key_is_rejected(self, coord_db) -> None:
        from coord.state import _save_portal_link_local

        with pytest.raises(ValueError):
            _save_portal_link_local({"submission_id": "sub_1"})

    def test_record_with_neither_milestone_nor_issue_is_rejected(self, coord_db) -> None:
        """#2665: repo_name alone is no longer enough — a record must pick a
        target."""
        from coord.state import _save_portal_link_local

        with pytest.raises(ValueError):
            _save_portal_link_local({"repo_name": "acme-portal", "submission_id": "sub_1"})

    def test_record_with_both_milestone_and_issue_is_rejected(self, coord_db) -> None:
        from coord.state import _save_portal_link_local

        with pytest.raises(ValueError):
            _save_portal_link_local(
                {
                    "repo_name": "acme-portal",
                    "milestone_number": 3,
                    "issue_number": 42,
                    "submission_id": "sub_1",
                }
            )

    def test_list_is_empty_by_default(self, coord_db) -> None:
        from coord.state import list_portal_links

        assert list_portal_links() == []

    def test_get_requires_exactly_one_of_milestone_or_issue(self, coord_db) -> None:
        from coord.state import get_portal_link

        with pytest.raises(ValueError):
            get_portal_link(repo_name="acme-portal")
        with pytest.raises(ValueError):
            get_portal_link(repo_name="acme-portal", milestone_number=3, issue_number=42)

    def test_write_routes_to_the_daemon_when_configured(self, monkeypatch) -> None:
        """#2751: a `type="decomposition-chat"` session dispatched to a thin
        client must not write its `coord portal link` into a local DB nobody
        reads — same posture as `save_gate_a_approval`
        (`tests/test_gate_a.py::TestPersistence::
        test_write_routes_to_the_daemon_when_configured`)."""
        from coord import client as cc
        from coord import state

        monkeypatch.setattr(
            cc,
            "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        captured: dict = {}
        monkeypatch.setattr(
            cc,
            "post_record",
            lambda svc, path, payload, **kw: captured.update(
                path=path, payload=payload
            )
            or {"ok": True},
        )
        record = {
            "repo_name": "acme-portal",
            "milestone_number": 3,
            "issue_number": None,
            "submission_id": "sub_abc123",
        }
        state.save_portal_link(record)
        assert captured["path"] == "/portal-link"
        assert captured["payload"] == {"record": record}
        assert state.list_portal_links() == []  # routed → no local write

    def test_read_routes_to_the_daemon_when_configured(self, monkeypatch) -> None:
        from coord import client as cc
        from coord import state

        monkeypatch.setattr(
            cc,
            "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        captured: dict = {}

        def _fake_get(url, *, params, headers, timeout):
            captured.update(url=url, params=params)

            class _Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "link": {
                            "repo_name": "acme-portal",
                            "milestone_number": 3,
                            "submission_id": "sub_abc123",
                        }
                    }

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", _fake_get)
        link = state.get_portal_link(repo_name="acme-portal", milestone_number=3)
        assert captured["url"] == "http://d:7435/portal-link"
        assert captured["params"] == {"repo_name": "acme-portal", "milestone_number": 3}
        assert link["submission_id"] == "sub_abc123"

    def test_unreachable_daemon_reads_as_not_linked(self, monkeypatch) -> None:
        """Fails soft, mirroring `fetch_gate_a_approval`: "couldn't ask"
        collapses to "not linked", which is exactly what the CLI already
        reports for a genuinely unlinked milestone/issue."""
        import httpx

        from coord import client as cc

        def _boom(*a, **k):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(cc.httpx, "get", _boom)
        assert (
            cc.fetch_portal_link(
                cc.ServiceConfig("http://d:7435"), "acme-portal", milestone_number=3
            )
            is None
        )


def test_daemon_portal_link_endpoints(tmp_path) -> None:
    """#2751: POST/GET `/portal-link` — the thin-client seam a
    `type="decomposition-chat"` session needs to run its mandatory
    `coord portal link` step from any machine, mirroring
    `tests/test_gate_a.py::test_daemon_gate_a_approval_endpoints`."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db, state
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-link.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    ms_record = {
        "repo_name": "api",
        "milestone_number": 3,
        "issue_number": None,
        "submission_id": "sub_abc123",
        "linked_at": 0.0,
        "actor": "tester",
        "schema": 1,
    }
    issue_record = {
        "repo_name": "api",
        "milestone_number": None,
        "issue_number": 42,
        "submission_id": "sub_def456",
        "linked_at": 0.0,
        "actor": "tester",
        "schema": 1,
    }
    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing = cli.get(
            "/portal-link", params={"repo_name": "api", "milestone_number": 3}
        )
        ok_ms = cli.post("/portal-link", json={"record": ms_record})
        ok_issue = cli.post("/portal-link", json={"record": issue_record})
        bad = cli.post("/portal-link", json={"record": "not-an-object"})
        found_ms = cli.get(
            "/portal-link", params={"repo_name": "api", "milestone_number": 3}
        )
        found_issue = cli.get(
            "/portal-link", params={"repo_name": "api", "issue_number": 42}
        )
        other = cli.get(
            "/portal-link", params={"repo_name": "api", "milestone_number": 99}
        )
        bad_params_neither = cli.get("/portal-link", params={"repo_name": "api"})
        bad_params_both = cli.get(
            "/portal-link",
            params={"repo_name": "api", "milestone_number": 3, "issue_number": 42},
        )

    assert missing.json() == {"link": None}
    assert ok_ms.status_code == 200
    assert ok_issue.status_code == 200
    assert bad.status_code == 400
    assert found_ms.json()["link"] == ms_record
    assert found_issue.json()["link"] == issue_record
    assert other.json() == {"link": None}
    assert bad_params_neither.status_code == 400
    assert bad_params_both.status_code == 400
    assert (
        state._get_portal_link_local(
            repo_name="api", milestone_number=3, issue_number=None
        )
        == ms_record
    )


def test_daemon_portal_decision_endpoint(tmp_path) -> None:
    """#2749 (IL-3): POST `/portal-decision` — the Decisions layer's one
    agent-writable seam, mirroring `test_daemon_portal_link_endpoints`
    above (#2751) — an agent session proposing/confirming/rejecting a
    decision can land on any machine, not just the daemon host."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.portal_store import decisions_for_submission
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-decision.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing_submission = cli.post("/portal-decision", json={"action": "propose"})
        proposed = cli.post(
            "/portal-decision",
            json={
                "action": "propose",
                "submission_id": "sub_1",
                "text": "Ship offline-first v1",
                "actor": "agent-1",
            },
        )
        unknown_action = cli.post(
            "/portal-decision",
            json={"action": "nope", "submission_id": "sub_1"},
        )
        seq = proposed.json()["entry"]["seq"]
        confirmed = cli.post(
            "/portal-decision",
            json={
                "action": "confirm",
                "submission_id": "sub_1",
                "seq": seq,
                "actor": "operator",
            },
        )
        rejected_no_reason = cli.post(
            "/portal-decision",
            json={"action": "reject", "submission_id": "sub_1", "seq": seq, "reason": ""},
        )

    assert missing_submission.status_code == 400
    assert proposed.status_code == 200
    assert proposed.json()["entry"]["state"] == "proposed"
    assert proposed.json()["entry"]["text"] == "Ship offline-first v1"
    assert unknown_action.status_code == 400
    assert confirmed.status_code == 200
    assert confirmed.json()["entry"]["state"] == "confirmed"
    assert rejected_no_reason.status_code == 400

    [stored] = decisions_for_submission("sub_1")
    assert stored.state == "confirmed"
    assert stored.text == "Ship offline-first v1"


def test_daemon_portal_ledger_endpoint(tmp_path) -> None:
    """#2749 (IL-3): GET `/portal-ledger` — the read half of the "any
    machine can be briefed" requirement, mirroring the two endpoint tests
    above."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db, portal_store
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-ledger.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    row = portal_store.enqueue("sub_1", "question", {"question": "Offline-first?"})
    portal_store.mark_applied(row)

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing_param = cli.get("/portal-ledger")
        found = cli.get("/portal-ledger", params={"submission_id": "sub_1"})
        empty = cli.get("/portal-ledger", params={"submission_id": "sub_nope"})

    assert missing_param.status_code == 400
    assert found.status_code == 200
    payload = found.json()["payload"]
    assert payload["submission_id"] == "sub_1"
    [qa] = payload["qa"]
    assert qa["question"] == "Offline-first?"
    assert qa["answers"] == []
    assert empty.json()["payload"]["qa"] == []


class TestEventsAfterVerdictWatermarkPagination:
    """#2723 (Phase C slice 5/7 of #1948): the seam migration replaced the
    `(received_at, rowid)` tiebreak `events_after_verdict_watermark` pages on
    with `(received_at, event_id)` — SQLite's implicit `rowid` has no
    Postgres equivalent, but `portal_events.event_id` is the table's own
    primary key, so it is unique per row and `(received_at, event_id)` stays
    a TOTAL order. That is exactly the property a watermark scan needs to
    neither skip nor repeat a row while paging: every row is visited exactly
    once as the cursor strictly advances through a fixed total order,
    regardless of what the tiebreak column actually contains.

    These tests drive `events_after_verdict_watermark` the way
    `coord.portal_sync._consume_verdicts` does — repeatedly, feeding each
    page's last `(received_at, event_id)` back in as the next call's cursor —
    and check the UNION of every page for exactly the accounting properties
    that matter: nothing missing, nothing seen twice.
    """

    def _drain(self, *, limit: int) -> list[str]:
        """Page from the PERSISTED watermark (`get_verdict_watermark`) to the
        end, the same cursor `coord.portal_sync._consume_verdicts` uses —
        and leave the watermark advanced at the end, so a second `_drain`
        call is a genuine "what's new since last time" check."""
        from coord.portal_store import (
            events_after_verdict_watermark,
            get_verdict_watermark,
            set_verdict_watermark,
        )

        received_at, event_id = get_verdict_watermark()
        seen: list[str] = []
        for _ in range(1000):  # generous cap; a real bug would loop forever
            page = events_after_verdict_watermark(received_at, event_id, limit=limit)
            if not page:
                break
            for eid, event in page:
                seen.append(eid)
                received_at, event_id = event.received_at, eid
        set_verdict_watermark(received_at, event_id)
        return seen

    def test_all_rows_share_one_received_at_no_skip_no_repeat(self, coord_db) -> None:
        """The realistic worst case: one `record_events` page, so EVERY row
        ties on `received_at` and the scan lives entirely off the tiebreak.
        """
        from coord.portal_store import record_events

        event_ids = [f"e{i:03d}" for i in range(37)]
        record_events(
            [{"id": eid, "submission_id": "sub_1", "type": "noise"} for eid in event_ids],
            now=100.0,
        )

        seen = self._drain(limit=5)  # small page, forces many round trips

        assert sorted(seen) == sorted(event_ids)  # nothing skipped
        assert len(seen) == len(set(seen))  # nothing repeated
        # And the traversal order is the declared total order.
        assert seen == sorted(event_ids)

    def test_mixed_ties_and_distinct_timestamps_no_skip_no_repeat(self, coord_db) -> None:
        """Several `record_events` pages (distinct `received_at`s, mirroring
        separate pull ticks), each internally tied — the scan must stitch
        pages together without dropping or re-visiting a row at the
        boundary."""
        from coord.portal_store import record_events

        batches = [
            (100.0, ["b0", "a1", "c2"]),
            (100.0, ["z9", "y8"]),  # SAME received_at as the batch above
            (200.0, ["m5", "m1", "m3"]),
            (50.0, ["early"]),  # earlier than everything already inserted
        ]
        all_ids: list[str] = []
        for received_at, ids in batches:
            record_events(
                [{"id": eid, "submission_id": "sub_1", "type": "noise"} for eid in ids],
                now=received_at,
            )
            all_ids.extend(ids)

        seen = self._drain(limit=2)  # smaller than every batch

        assert sorted(seen) == sorted(all_ids)
        assert len(seen) == len(set(seen))

    def test_a_second_drain_from_the_advanced_watermark_returns_nothing(
        self, coord_db
    ) -> None:
        """Once the watermark has caught up, re-scanning from where it
        stopped must not re-surface anything already visited."""
        from coord.portal_store import record_events

        record_events(
            [
                {"id": "e1", "submission_id": "sub_1", "type": "noise"},
                {"id": "e2", "submission_id": "sub_1", "type": "noise"},
            ],
            now=100.0,
        )
        first = self._drain(limit=100)
        assert sorted(first) == ["e1", "e2"]

        second = self._drain(limit=100)
        assert second == []  # nothing new since the watermark already caught up


class TestVerdictWatermarkEventIdIsAlwaysText:
    """#2723 review fix: `verdict_watermark_rowid` used to be declared
    `INTEGER` in `coord.db`'s schema even though this slice repointed it at
    `portal_events.event_id` — a TEXT primary key that is frequently
    non-numeric (`_synthetic_event_id` mints `sha256:<hash>` ids for any
    event the portal didn't give an id). An `INTEGER` column silently
    coerces a numeric-looking string to SQLite's `integer` storage class on
    write (manifest typing), masking the bug locally while Postgres would
    reject a `sha256:...` id outright. Pin that the round trip preserves the
    exact string for both a hash-style id and a numeric-looking one, and
    that `get_verdict_watermark`'s return type is always `str`.
    """

    def test_synthetic_sha256_style_event_id_round_trips_as_str(self, coord_db) -> None:
        from coord.portal_store import get_verdict_watermark, set_verdict_watermark

        synthetic_id = "sha256:" + "ab" * 16
        set_verdict_watermark(100.0, synthetic_id)

        received_at, event_id = get_verdict_watermark()

        assert received_at == 100.0
        assert event_id == synthetic_id
        assert isinstance(event_id, str)

    def test_numeric_looking_event_id_round_trips_as_str_not_int(self, coord_db) -> None:
        """A portal-provided numeric id (valid per `_event_id_of`'s
        docstring) must not come back as an `int` — the column is TEXT, and
        `get_verdict_watermark` is typed `-> tuple[float, str]`."""
        from coord.portal_store import get_verdict_watermark, set_verdict_watermark

        set_verdict_watermark(200.0, "42")

        received_at, event_id = get_verdict_watermark()

        assert received_at == 200.0
        assert event_id == "42"
        assert isinstance(event_id, str)


class TestEnqueueSeamMigration:
    """#2723: `enqueue()`'s `INSERT INTO portal_outbox` used to read back its
    new row's id via `cursor.lastrowid`; it now goes through
    `coord.sql.insert_returning_id` (the seam's portable lastrowid/RETURNING
    helper). Pin that the returned `OutboxRow.id` is real and actually
    resolves to the stored row, not just a non-crashing call."""

    def test_returned_id_round_trips_to_the_stored_row(self, coord_db) -> None:
        from coord.portal_store import outbox_for_submission, enqueue

        row = enqueue("sub_1", "status", {"status": "in_progress"})
        assert isinstance(row.id, int)
        assert row.id > 0

        [stored] = outbox_for_submission("sub_1")
        assert stored.id == row.id
        assert stored.fields == {"status": "in_progress"}

    def test_successive_enqueues_get_distinct_increasing_ids(self, coord_db) -> None:
        from coord.portal_store import enqueue

        first = enqueue("sub_1", "status", {"status": "a"})
        second = enqueue("sub_1", "status", {"status": "b"})
        assert second.id > first.id


# ── #2749 (IL-3): the running-context ledger ─────────────────────────────────


class TestLedger:
    def test_append_and_read_back(self, coord_db) -> None:
        from coord.portal_store import (
            LEDGER_KIND_QUESTION_ANSWERED,
            append_ledger_entry,
            ledger_for_submission,
        )

        entry = append_ledger_entry(
            "sub_1",
            LEDGER_KIND_QUESTION_ANSWERED,
            question_revision=1,
            text="Yes, offline-first.",
            actor="jane",
            source_event_id="e1",
            payload={"raw": True},
        )
        assert entry.seq == 1
        [stored] = ledger_for_submission("sub_1")
        assert stored == entry
        assert stored.text == "Yes, offline-first."
        assert stored.payload == {"raw": True}

    def test_seq_is_monotonic_per_submission(self, coord_db) -> None:
        from coord.portal_store import append_ledger_entry, ledger_for_submission

        append_ledger_entry("sub_1", "question_pushed", text="Q1")
        append_ledger_entry("sub_1", "question_pushed", text="Q2")
        append_ledger_entry("sub_2", "question_pushed", text="other submission")

        seqs = [e.seq for e in ledger_for_submission("sub_1")]
        assert seqs == [1, 2]
        assert [e.seq for e in ledger_for_submission("sub_2")] == [1]

    def test_repeat_append_with_same_source_event_id_is_a_no_op(self, coord_db) -> None:
        """#2749: idempotency for a consumer retrying after a crash between
        the ledger append and `mark_event_handled` — a second append for the
        same (submission_id, kind, source_event_id) must return the
        ALREADY-stored row, not duplicate it."""
        from coord.portal_store import append_ledger_entry, ledger_for_submission

        first = append_ledger_entry(
            "sub_1", "question_answered", text="Yes.", source_event_id="e1"
        )
        second = append_ledger_entry(
            "sub_1", "question_answered", text="Yes.", source_event_id="e1"
        )
        assert first == second
        assert len(ledger_for_submission("sub_1")) == 1

    def test_no_source_event_id_never_dedupes(self, coord_db) -> None:
        """Rows with no `source_event_id` (e.g. `question_pushed`, derived
        from an outbox transition, not a pulled event) coexist freely — SQL's
        NULL != NULL means the `UNIQUE(submission_id, kind, source_event_id)`
        constraint never fires for them."""
        from coord.portal_store import append_ledger_entry, ledger_for_submission

        append_ledger_entry("sub_1", "question_pushed", text="Q1")
        append_ledger_entry("sub_1", "question_pushed", text="Q2")
        assert len(ledger_for_submission("sub_1")) == 2

    def test_mark_applied_on_a_question_row_ledgers_it(self, coord_db) -> None:
        """The ledger's own record of "a question as it was ACTUALLY
        pushed" — written from `mark_applied`'s `kind == "question"` branch,
        not at enqueue time (see that function's #2749 comment)."""
        from coord.portal_store import (
            LEDGER_KIND_QUESTION_PUSHED,
            enqueue,
            ledger_for_submission,
            mark_applied,
        )

        row = enqueue("sub_1", "question", {"question": "Offline-first?"})
        assert ledger_for_submission("sub_1") == []  # not yet — only enqueued

        mark_applied(row)
        [entry] = ledger_for_submission("sub_1")
        assert entry.kind == LEDGER_KIND_QUESTION_PUSHED
        assert entry.question_revision == row.revision
        assert entry.text == "Offline-first?"


# ── #2749 (IL-3): decisions ──────────────────────────────────────────────────


class TestDecisions:
    def test_propose_then_confirm(self, coord_db) -> None:
        from coord.portal_store import (
            DECISION_CONFIRMED,
            confirm_decision,
            propose_decision,
        )

        proposed = propose_decision("sub_1", "Ship offline-first v1", actor="agent-1")
        assert proposed.state == "proposed"
        assert proposed.is_current

        confirmed = confirm_decision("sub_1", proposed.seq, actor="operator")
        assert confirmed.state == DECISION_CONFIRMED
        assert confirmed.text == "Ship offline-first v1"  # text never rewritten
        assert confirmed.is_current

    def test_reject_requires_a_reason(self, coord_db) -> None:
        from coord.portal_store import propose_decision, reject_decision

        proposed = propose_decision("sub_1", "Native app")
        with pytest.raises(ValueError, match="reason"):
            reject_decision("sub_1", proposed.seq, "")
        with pytest.raises(ValueError, match="reason"):
            reject_decision("sub_1", proposed.seq, "   ")

        rejected = reject_decision("sub_1", proposed.seq, "customer wants web-only")
        assert rejected.state == "rejected"
        assert rejected.reason == "customer wants web-only"
        assert not rejected.is_current

    def test_supersede_keeps_the_old_row_and_points_at_the_new_one(self, coord_db) -> None:
        from coord.portal_store import propose_decision, supersede_decision

        first = propose_decision("sub_1", "Use Postgres")
        second = propose_decision("sub_1", "Use SQLite")
        superseded = supersede_decision("sub_1", first.seq, by_seq=second.seq)

        assert superseded.state == "superseded"
        assert superseded.superseded_by_seq == second.seq
        assert superseded.text == "Use Postgres"  # never rewritten
        assert not superseded.is_current

    def test_transitioning_an_unknown_seq_raises(self, coord_db) -> None:
        from coord.portal_store import confirm_decision

        with pytest.raises(ValueError, match="no decision"):
            confirm_decision("sub_1", 999)

    def test_decisions_for_submission_is_seq_ordered_and_scoped(self, coord_db) -> None:
        from coord.portal_store import decisions_for_submission, propose_decision

        propose_decision("sub_1", "first")
        propose_decision("sub_1", "second")
        propose_decision("sub_2", "unrelated")

        texts = [d.text for d in decisions_for_submission("sub_1")]
        assert texts == ["first", "second"]

    def test_propose_routes_to_the_daemon_when_board_service_is_set(
        self, coord_db, monkeypatch
    ) -> None:
        """#2749: the one write path in this module that IS daemon-routed —
        an agent session can land on any machine, not just the daemon host,
        same reason `coord portal link` routes (#2751)."""
        import coord.client as cc
        from coord.portal_store import propose_decision

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        calls = []

        def fake_post_record(svc, path, payload, **kw):
            calls.append((path, payload))
            return {
                "entry": {
                    "id": 1, "submission_id": payload["submission_id"], "seq": 1,
                    "text": payload["text"], "state": "proposed", "reason": "",
                    "superseded_by_seq": None, "actor": payload.get("actor", ""),
                    "recorded_at": 100.0, "updated_at": 100.0,
                }
            }

        monkeypatch.setattr(cc, "post_record", fake_post_record)

        result = propose_decision("sub_1", "Routed decision", actor="agent-x")
        assert calls == [
            (
                "/portal-decision",
                {
                    "action": "propose",
                    "submission_id": "sub_1",
                    "text": "Routed decision",
                    "actor": "agent-x",
                },
            )
        ]
        assert result.text == "Routed decision"
        assert result.actor == "agent-x"


# ── #2749 (IL-3): narrative ───────────────────────────────────────────────────


class TestNarrative:
    def test_set_then_get(self, coord_db) -> None:
        from coord.portal_store import get_narrative, set_narrative

        set_narrative("sub_1", "The customer wants an offline-first mobile app.", actor="agent-1")
        entry = get_narrative("sub_1")
        assert entry.text == "The customer wants an offline-first mobile app."
        assert entry.actor == "agent-1"

    def test_missing_narrative_is_none(self, coord_db) -> None:
        from coord.portal_store import get_narrative

        assert get_narrative("nope") is None

    def test_set_overwrites_wholesale_not_appends(self, coord_db) -> None:
        from coord.portal_store import get_narrative, set_narrative

        set_narrative("sub_1", "first draft")
        set_narrative("sub_1", "second draft, replacing the first")
        entry = get_narrative("sub_1")
        assert entry.text == "second draft, replacing the first"


# ── #2749 (IL-3): the question consumer's private watermark ─────────────────


class TestQuestionWatermark:
    def test_default_is_zero_sentinel(self, coord_db) -> None:
        from coord.portal_store import get_question_watermark

        assert get_question_watermark() == (0.0, "")

    def test_set_then_get_round_trips(self, coord_db) -> None:
        from coord.portal_store import get_question_watermark, set_question_watermark

        set_question_watermark(200.0, "evt-42")
        assert get_question_watermark() == (200.0, "evt-42")

    def test_independent_of_the_verdict_watermark(self, coord_db) -> None:
        from coord.portal_store import (
            get_question_watermark,
            get_verdict_watermark,
            set_question_watermark,
            set_verdict_watermark,
        )

        set_verdict_watermark(50.0, "verdict-evt")
        set_question_watermark(75.0, "question-evt")
        assert get_verdict_watermark() == (50.0, "verdict-evt")
        assert get_question_watermark() == (75.0, "question-evt")


# ── #2903 (phase 1 of #2902): the draft gate ────────────────────────────────


def _draft_question(sub: str = "sub_d1", text: str = "which shade of blue?"):
    from coord import portal_store

    return portal_store.enqueue(
        sub, "question", {"question": text}, state=portal_store.STATE_DRAFT
    )


def _draft_design(sub: str = "sub_d1", outcome: str = "a booking form"):
    from coord import portal_store

    return portal_store.enqueue(
        sub,
        "design_round",
        {
            "design_round": {
                "round": 1,
                "outcome_definition": outcome,
                "decomposition": ["a", "b"],
                "bundle_key": "r2://bundles/1",
            }
        },
        state=portal_store.STATE_DRAFT,
    )


class TestDraftState:
    def test_a_draft_row_is_never_returned_by_pending_outbox(self, coord_db) -> None:
        from coord import portal_store

        row = _draft_question()
        assert row.state == portal_store.STATE_DRAFT
        assert portal_store.pending_outbox() == []
        assert [r.seq for r in portal_store.draft_outbox()] == [row.seq]

    def test_a_draft_still_allocates_seq_and_revision(self, coord_db) -> None:
        """It holds its place in per-submission FIFO — rows behind it block."""
        from coord import portal_store

        first = _draft_question()
        second = portal_store.enqueue("sub_d1", "status", {"status": "in-design"})
        assert (first.seq, first.revision) == (1, 1)
        assert (second.seq, second.revision) == (2, 2)

    def test_enqueue_still_defaults_to_pending(self, coord_db) -> None:
        from coord import portal_store

        row = portal_store.enqueue("sub_d1", "status", {"status": "in-design"})
        assert row.state == portal_store.STATE_PENDING
        assert [r.seq for r in portal_store.pending_outbox()] == [1]

    def test_enqueue_refuses_a_state_that_is_not_draft_or_pending(self, coord_db) -> None:
        from coord import portal_store

        with pytest.raises(ValueError, match="can only be enqueued"):
            portal_store.enqueue(
                "sub_d1", "status", {"status": "shipped"}, state=portal_store.STATE_APPLIED
            )

    def test_draft_outbox_can_be_scoped_to_one_submission(self, coord_db) -> None:
        from coord import portal_store

        _draft_question("sub_a")
        _draft_question("sub_b")
        assert [r.submission_id for r in portal_store.draft_outbox("sub_a")] == ["sub_a"]
        assert len(portal_store.draft_outbox()) == 2


class TestEditDraft:
    def test_rewrites_the_field_and_leaves_the_row_draft(self, coord_db) -> None:
        from coord import portal_store

        _draft_question()
        updated = portal_store.edit_draft(
            "sub_d1", 1, "question", "which blue?", actor="john"
        )
        assert updated.fields["question"] == "which blue?"
        assert updated.state == portal_store.STATE_DRAFT

    def test_ledgers_both_the_agents_text_and_the_operators(self, coord_db) -> None:
        from coord import portal_store

        _draft_question(text="Whomst shall we ask about the blue?")
        portal_store.edit_draft("sub_d1", 1, "question", "which blue?", actor="john")

        entries = [
            e
            for e in portal_store.ledger_for_submission("sub_d1")
            if e.kind == portal_store.LEDGER_KIND_DRAFT_EDITED
        ]
        assert len(entries) == 1
        assert entries[0].text == "which blue?"
        assert entries[0].payload["agent_text"] == "Whomst shall we ask about the blue?"
        assert entries[0].payload["operator_text"] == "which blue?"
        assert entries[0].actor == "john"

    def test_a_second_edit_still_records_the_agents_original(self, coord_db) -> None:
        """Not the operator's first rewrite — the agent's words have to survive."""
        from coord import portal_store

        _draft_question(text="AGENT WORDS")
        portal_store.edit_draft("sub_d1", 1, "question", "first pass")
        portal_store.edit_draft("sub_d1", 1, "question", "second pass")

        entries = [
            e
            for e in portal_store.ledger_for_submission("sub_d1")
            if e.kind == portal_store.LEDGER_KIND_DRAFT_EDITED
        ]
        assert [e.payload["agent_text"] for e in entries] == ["AGENT WORDS", "AGENT WORDS"]
        assert entries[-1].payload["previous_text"] == "first pass"

    def test_edits_a_nested_design_round_field(self, coord_db) -> None:
        from coord import portal_store

        _draft_design()
        updated = portal_store.edit_draft(
            "sub_d1", 1, "design_round.outcome_definition", "a booking form, in plain words"
        )
        assert updated.fields["design_round"]["outcome_definition"] == (
            "a booking form, in plain words"
        )
        # ...and nothing else in the payload moved.
        assert updated.fields["design_round"]["bundle_key"] == "r2://bundles/1"
        assert updated.fields["design_round"]["decomposition"] == ["a", "b"]

    def test_refuses_a_non_editable_field(self, coord_db) -> None:
        from coord import portal_store

        _draft_design()
        with pytest.raises(portal_store.DraftGateError, match="not editable"):
            portal_store.edit_draft(
                "sub_d1", 1, "design_round.bundle_key", "r2://somewhere/else"
            )

    def test_refuses_a_row_that_is_not_a_draft(self, coord_db) -> None:
        from coord import portal_store

        portal_store.enqueue("sub_d1", "question", {"question": "already out"})
        with pytest.raises(portal_store.DraftGateError, match="not draft"):
            portal_store.edit_draft("sub_d1", 1, "question", "too late")

    def test_refuses_an_unknown_row(self, coord_db) -> None:
        from coord import portal_store

        with pytest.raises(portal_store.DraftGateError, match="no outbox row"):
            portal_store.edit_draft("sub_d1", 9, "question", "nope")

    def test_refuses_empty_text(self, coord_db) -> None:
        from coord import portal_store

        _draft_question()
        with pytest.raises(portal_store.DraftGateError, match="non-empty"):
            portal_store.edit_draft("sub_d1", 1, "question", "   ")


class TestApproveDraft:
    def test_flips_to_pending_keeping_seq_and_revision(self, coord_db) -> None:
        from coord import portal_store

        before = _draft_question()
        row = portal_store.approve_draft("sub_d1", 1, actor="john")
        assert row.state == portal_store.STATE_PENDING
        assert (row.seq, row.revision) == (before.seq, before.revision)
        assert [r.seq for r in portal_store.pending_outbox()] == [1]
        assert portal_store.draft_outbox() == []

    def test_appends_a_ledger_entry(self, coord_db) -> None:
        from coord import portal_store

        _draft_question(text="which blue?")
        portal_store.approve_draft("sub_d1", 1, actor="john")
        kinds = [e.kind for e in portal_store.ledger_for_submission("sub_d1")]
        assert portal_store.LEDGER_KIND_DRAFT_APPROVED in kinds

    def test_refuses_a_second_approval(self, coord_db) -> None:
        from coord import portal_store

        _draft_question()
        portal_store.approve_draft("sub_d1", 1)
        with pytest.raises(portal_store.DraftGateError, match="not draft"):
            portal_store.approve_draft("sub_d1", 1)


class TestRejectDraft:
    def test_flips_to_the_existing_terminal_rejected_state(self, coord_db) -> None:
        from coord import portal_store

        _draft_question()
        row, also = portal_store.reject_draft("sub_d1", 1, "off-brand")
        assert row.state == portal_store.STATE_REJECTED
        assert row.reason == "off-brand"
        assert also == []

    def test_a_reason_is_mandatory(self, coord_db) -> None:
        from coord import portal_store

        _draft_question()
        with pytest.raises(portal_store.DraftGateError, match="must carry a reason"):
            portal_store.reject_draft("sub_d1", 1, "   ")

    def test_ledgers_the_reason_and_the_agents_text(self, coord_db) -> None:
        from coord import portal_store

        _draft_question(text="which blue?")
        portal_store.reject_draft("sub_d1", 1, "we already know", actor="john")
        entry = [
            e
            for e in portal_store.ledger_for_submission("sub_d1")
            if e.kind == portal_store.LEDGER_KIND_DRAFT_REJECTED
        ][0]
        assert entry.text == "we already know"
        assert entry.payload["agent_text"] == "which blue?"

    def test_also_rejects_what_announces_it(self, coord_db) -> None:
        """#2903: `ordering_block_reason` treats a rejected prerequisite as
        never-applied, so a bare reject would hold the announcement forever."""
        from coord import portal_store

        _draft_question()
        announcement = portal_store.enqueue(
            "sub_d1",
            "status",
            {"status": "needs-input"},
            announces="needs-input",
            requires_kind="question",
        )
        row, also = portal_store.reject_draft("sub_d1", 1, "not asking that")

        assert row.state == portal_store.STATE_REJECTED
        assert [r.seq for r in also] == [announcement.seq]
        assert also[0].state == portal_store.STATE_REJECTED
        assert "seq 1" in also[0].reason
        assert portal_store.pending_outbox() == []

    def test_no_cascade_refuses_and_names_the_row_to_reject_first(self, coord_db) -> None:
        from coord import portal_store

        _draft_question()
        portal_store.enqueue(
            "sub_d1",
            "status",
            {"status": "needs-input"},
            announces="needs-input",
            requires_kind="question",
        )
        with pytest.raises(portal_store.DraftGateError, match="seq=2"):
            portal_store.reject_draft(
                "sub_d1", 1, "not asking that", cascade=False
            )
        # Nothing moved.
        assert portal_store.draft_outbox()[0].seq == 1

    def test_does_not_touch_an_announcement_that_rides_on_a_later_row(
        self, coord_db
    ) -> None:
        """A second question's announcement depends on the SECOND question —
        rejecting the first must leave it alone, exactly as the guard reads it."""
        from coord import portal_store

        _draft_question(text="q1")
        portal_store.enqueue("sub_d1", "question", {"question": "q2"})
        announcement = portal_store.enqueue(
            "sub_d1",
            "status",
            {"status": "needs-input"},
            announces="needs-input",
            requires_kind="question",
        )
        _row, also = portal_store.reject_draft("sub_d1", 1, "superseded by q2")
        assert also == []
        assert portal_store.get_outbox_row("sub_d1", announcement.seq).state == (
            portal_store.STATE_PENDING
        )

    def test_does_not_re_reject_an_already_applied_announcement(self, coord_db) -> None:
        from coord import portal_store

        _draft_question()
        applied = portal_store.enqueue(
            "sub_d1",
            "status",
            {"status": "needs-input"},
            announces="needs-input",
            requires_kind="question",
        )
        portal_store.mark_applied(applied)
        _row, also = portal_store.reject_draft("sub_d1", 1, "too late but still")
        assert also == []


class TestDraftFieldValue:
    def test_reads_a_dotted_path(self, coord_db) -> None:
        from coord import portal_store

        row = _draft_design(outcome="a booking form")
        assert portal_store.draft_field_value(row, "design_round.outcome_definition") == (
            "a booking form"
        )

    def test_missing_path_reads_as_none(self, coord_db) -> None:
        from coord import portal_store

        row = _draft_question()
        assert portal_store.draft_field_value(row, "design_round.nope") is None


# ── #2867: operator notes — the ledger's operator-context layer ──────────────


class TestOperatorNotes:
    def test_append_stores_verbatim_and_attributes_the_human(self, coord_db) -> None:
        from coord.portal_store import (
            LEDGER_KIND_OPERATOR_NOTE,
            append_operator_note,
            ledger_for_submission,
            seed_revision,
        )

        seed_revision("sub_1", 1)
        text = "Spoke to her — it's just the two of them; calendar is a nice-to-have."
        entry = append_operator_note("sub_1", text, actor="jane")

        assert entry.kind == LEDGER_KIND_OPERATOR_NOTE
        assert entry.text == text
        assert entry.actor == "operator:jane"
        [stored] = ledger_for_submission("sub_1")
        assert stored == entry

    def test_unknown_submission_is_rejected(self, coord_db) -> None:
        from coord.portal_store import append_operator_note, ledger_for_submission

        with pytest.raises(ValueError, match="unknown submission"):
            append_operator_note("sub_nope", "background")
        assert ledger_for_submission("sub_nope") == []

    def test_empty_text_is_rejected(self, coord_db) -> None:
        from coord.portal_store import append_operator_note, seed_revision

        seed_revision("sub_1", 1)
        with pytest.raises(ValueError, match="non-empty"):
            append_operator_note("sub_1", "   ")

    def test_actor_is_never_double_prefixed(self, coord_db) -> None:
        """A note routed through the daemon arrives with an ALREADY-prefixed
        actor; re-normalizing must not yield `operator:operator:jane`."""
        from coord.portal_store import operator_actor

        assert operator_actor("jane") == "operator:jane"
        assert operator_actor("operator:jane") == "operator:jane"
        assert operator_actor("") == "operator"
        assert operator_actor("   ") == "operator"

    def test_notes_interleave_with_other_kinds_in_seq_order(self, coord_db) -> None:
        from coord.portal_store import (
            append_ledger_entry,
            append_operator_note,
            ledger_for_submission,
            operator_notes_for_submission,
            seed_revision,
        )

        seed_revision("sub_1", 1)
        append_ledger_entry("sub_1", "question_pushed", question_revision=1, text="Q1")
        append_operator_note("sub_1", "first note", actor="jane")
        append_ledger_entry("sub_1", "question_pushed", question_revision=2, text="Q2")
        append_operator_note("sub_1", "second note", actor="jane")

        assert [e.seq for e in ledger_for_submission("sub_1")] == [1, 2, 3, 4]
        assert [(n.seq, n.text) for n in operator_notes_for_submission("sub_1")] == [
            (2, "first note"),
            (4, "second note"),
        ]

    def test_render_ledger_payload_exposes_notes_and_keeps_them_out_of_qa(
        self, coord_db
    ) -> None:
        """#2867: notes are ledger-class — they appear under their own key,
        never as an answer, never as a decision, never in the narrative."""
        from coord.portal_store import (
            append_ledger_entry,
            append_operator_note,
            render_ledger_payload,
            seed_revision,
        )

        seed_revision("sub_1", 1)
        append_ledger_entry("sub_1", "question_pushed", question_revision=1, text="Q1")
        append_operator_note("sub_1", "Household of two.", actor="jane")

        payload = render_ledger_payload("sub_1")
        assert payload["operator_notes"] == [
            {
                "seq": 2,
                "text": "Household of two.",
                "actor": "operator:jane",
                "recorded_at": payload["operator_notes"][0]["recorded_at"],
            }
        ]
        assert payload["qa"] == [
            {
                "question_revision": 1,
                "question": "Q1",
                "answers": [],
                "confirmations": [],
            }
        ]
        assert payload["unpaired_answers"] == []
        assert payload["decisions"] == []
        assert payload["archived_decisions"] == []
        assert payload["narrative"] == ""

    def test_append_routes_to_the_daemon_when_board_service_is_set(
        self, coord_db, monkeypatch
    ) -> None:
        import coord.client as cc
        from coord.portal_store import append_operator_note, ledger_for_submission

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        calls = []

        def fake_post_record(svc, path, payload, **kw):
            calls.append((path, payload))
            return {
                "entry": {
                    "id": 1,
                    "submission_id": payload["submission_id"],
                    "seq": 3,
                    "kind": "operator_note",
                    "question_revision": None,
                    "text": payload["text"],
                    "actor": "operator:jane",
                    "source_event_id": None,
                    "payload_json": "{}",
                    "recorded_at": 100.0,
                }
            }

        monkeypatch.setattr(cc, "post_record", fake_post_record)

        result = append_operator_note("sub_1", "Routed note", actor="jane")
        assert calls == [
            (
                "/portal-note",
                {"submission_id": "sub_1", "text": "Routed note", "actor": "jane"},
            )
        ]
        assert result.seq == 3
        assert result.text == "Routed note"
        assert result.actor == "operator:jane"
        # ...and nothing was written into this (thin client's) own local DB.
        assert ledger_for_submission("sub_1") == []


def test_daemon_portal_note_endpoint(tmp_path) -> None:
    """#2867: POST `/portal-note` — the operator-context layer's daemon seam,
    mirroring `test_daemon_portal_decision_endpoint` above. The operator may
    be sitting at any machine in the fleet; the write must land in the
    daemon's real `portal_ledger`, and an unknown submission must come back
    as a clear 400 rather than silently creating one."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.portal_store import operator_notes_for_submission, seed_revision
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-note.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)
    seed_revision("sub_1", 1)

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing_submission = cli.post("/portal-note", json={"text": "hi"})
        unknown_submission = cli.post(
            "/portal-note", json={"submission_id": "sub_nope", "text": "hi"}
        )
        empty_text = cli.post(
            "/portal-note", json={"submission_id": "sub_1", "text": "   "}
        )
        noted = cli.post(
            "/portal-note",
            json={
                "submission_id": "sub_1",
                "text": "Household of two; no logins needed.",
                "actor": "jane",
            },
        )

    assert missing_submission.status_code == 400
    assert unknown_submission.status_code == 400
    assert "unknown submission" in unknown_submission.json()["error"]
    assert empty_text.status_code == 400
    assert noted.status_code == 200
    entry = noted.json()["entry"]
    assert entry["kind"] == "operator_note"
    assert entry["text"] == "Household of two; no logins needed."
    assert entry["actor"] == "operator:jane"
    # `payload_json` is a JSON *string*, because the client feeds this dict
    # straight back into `_ledger_from_row`, which parses it as one.
    assert entry["payload_json"] == "{}"

    [stored] = operator_notes_for_submission("sub_1")
    assert stored.text == "Household of two; no logins needed."
    assert stored.actor == "operator:jane"


# ── #2986: `coord portal answer` — an out-of-band answer's ledger seam ──────


class TestAnswerQuestion:
    def test_answers_the_current_open_question(self, coord_db) -> None:
        from coord.portal_store import (
            LEDGER_KIND_QUESTION_ANSWERED,
            answer_question,
            enqueue,
            ledger_for_submission,
            mark_applied,
        )

        row = enqueue("sub_1", "question", {"question": "Offline-first?"})
        mark_applied(row)

        entry = answer_question("sub_1", "Yes, offline-first.", actor="jane")

        assert entry.kind == LEDGER_KIND_QUESTION_ANSWERED
        assert entry.question_revision == row.revision
        assert entry.text == "Yes, offline-first."
        assert entry.actor == "operator:jane"
        assert entry.payload == {"relayed": True, "source": "verbal"}
        [stored] = [
            e for e in ledger_for_submission("sub_1")
            if e.kind == LEDGER_KIND_QUESTION_ANSWERED
        ]
        assert stored == entry

    def test_source_defaults_to_verbal_and_validates_choice(self, coord_db) -> None:
        from coord.portal_store import answer_question, enqueue, mark_applied

        row = enqueue("sub_1", "question", {"question": "SQLite or Postgres?"})
        mark_applied(row)

        with pytest.raises(ValueError, match="unknown --source"):
            answer_question("sub_1", "SQLite.", source="carrier-pigeon")

        entry = answer_question("sub_1", "SQLite.", source="PHONE")
        assert entry.payload["source"] == "phone"

    def test_unknown_submission_is_rejected(self, coord_db) -> None:
        from coord.portal_store import answer_question, ledger_for_submission

        with pytest.raises(ValueError, match="unknown submission"):
            answer_question("sub_nope", "Yes.")
        assert ledger_for_submission("sub_nope") == []

    def test_empty_text_is_rejected(self, coord_db) -> None:
        from coord.portal_store import answer_question, enqueue, mark_applied

        row = enqueue("sub_1", "question", {"question": "Offline-first?"})
        mark_applied(row)
        with pytest.raises(ValueError, match="non-empty"):
            answer_question("sub_1", "   ")

    def test_no_open_question_is_a_clear_error(self, coord_db) -> None:
        from coord.portal_store import answer_question, seed_revision

        seed_revision("sub_1", 1)
        with pytest.raises(ValueError, match="no open question"):
            answer_question("sub_1", "Yes.")

    def test_revision_backfills_an_older_already_reasked_question(
        self, coord_db
    ) -> None:
        """SUB-1EA1D3's fixture case (#2986): Q[11] asked two things, the
        client answered verbally, and the operator re-asked the rest as
        Q[13] — so by the time the operator backfills Q[11]'s answer, Q[13]
        is the CURRENT open question. `--revision 11` must land on Q[11]
        regardless."""
        from coord.portal_store import (
            answer_question,
            enqueue,
            mark_applied,
            render_ledger_payload,
        )

        q11 = enqueue(
            "sub_1", "question", {"question": "Who will use this, and how?"}
        )
        mark_applied(q11)
        q13 = enqueue("sub_1", "question", {"question": "And the rest?"})
        mark_applied(q13)

        entry = answer_question(
            "sub_1", "Household of two.", revision=q11.revision
        )
        assert entry.question_revision == q11.revision

        payload = render_ledger_payload("sub_1")
        by_revision = {qa["question_revision"]: qa for qa in payload["qa"]}
        assert by_revision[q11.revision]["answers"][0]["text"] == "Household of two."
        assert by_revision[q13.revision]["answers"] == []

    def test_relayed_answer_is_flagged_in_the_rendered_payload(
        self, coord_db
    ) -> None:
        from coord.portal_store import answer_question, enqueue, mark_applied, render_ledger_payload

        row = enqueue("sub_1", "question", {"question": "Offline-first?"})
        mark_applied(row)
        answer_question("sub_1", "Yes.", source="email", actor="jane")

        payload = render_ledger_payload("sub_1")
        [answer] = payload["qa"][0]["answers"]
        assert answer["relayed"] is True
        assert answer["source"] == "email"
        assert answer["actor"] == "operator:jane"

    def test_later_inbound_answer_is_additive_not_a_conflict(self, coord_db) -> None:
        """#2986 acceptance: a later inbound `question.answered` for the same
        revision must not erase or error against an earlier relayed answer —
        both coexist, in recorded order, under the same question."""
        from coord.portal_store import (
            LEDGER_KIND_QUESTION_ANSWERED,
            answer_question,
            append_ledger_entry,
            enqueue,
            mark_applied,
            render_ledger_payload,
        )

        row = enqueue("sub_1", "question", {"question": "Offline-first?"})
        mark_applied(row)
        answer_question("sub_1", "Relayed: yes.", source="phone", actor="jane")
        append_ledger_entry(
            "sub_1",
            LEDGER_KIND_QUESTION_ANSWERED,
            question_revision=row.revision,
            text="Yes, offline-first please.",
            actor="customer",
            source_event_id="evt-1",
        )

        payload = render_ledger_payload("sub_1")
        [qa] = payload["qa"]
        assert [a["text"] for a in qa["answers"]] == [
            "Relayed: yes.",
            "Yes, offline-first please.",
        ]
        assert qa["answers"][0]["relayed"] is True
        assert qa["answers"][1]["relayed"] is False

    def test_fold_status_after_answer_is_best_effort_with_no_config(
        self, coord_db
    ) -> None:
        """`config=None` (the CLI's own default when this process is a thin
        client, or simply not supplied) must never turn the fold nudge into
        a crash — it is a courtesy, not the recorded fact."""
        from coord.portal_store import answer_question, enqueue, mark_applied

        row = enqueue("sub_1", "question", {"question": "Offline-first?"})
        mark_applied(row)
        entry = answer_question("sub_1", "Yes.", config=None)
        assert entry.text == "Yes."

    def test_fold_status_after_answer_never_raises_on_a_broken_fold(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import portal_sync, state
        from coord.portal_store import answer_question, enqueue, mark_applied

        row = enqueue("sub_1", "question", {"question": "Offline-first?"})
        mark_applied(row)
        state._save_portal_link_local(
            {"repo_name": "api", "milestone_number": 3, "submission_id": "sub_1"}
        )

        class _FakeConfig:
            def repo(self, name):
                return object()

        def _boom(*a, **k):
            raise RuntimeError("simulated GitHub failure")

        monkeypatch.setattr(portal_sync, "fold_status_for_milestone", _boom)

        entry = answer_question("sub_1", "Yes.", config=_FakeConfig())
        assert entry.text == "Yes."

    def test_routes_to_the_daemon_when_board_service_is_set(
        self, coord_db, monkeypatch
    ) -> None:
        import coord.client as cc
        from coord.portal_store import answer_question, ledger_for_submission

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        calls = []

        def fake_post_record(svc, path, payload, **kw):
            calls.append((path, payload))
            return {
                "entry": {
                    "id": 1,
                    "submission_id": payload["submission_id"],
                    "seq": 3,
                    "kind": "question_answered",
                    "question_revision": 1,
                    "text": payload["text"],
                    "actor": "operator:jane",
                    "source_event_id": None,
                    "payload_json": '{"relayed": true, "source": "phone"}',
                    "recorded_at": 100.0,
                }
            }

        monkeypatch.setattr(cc, "post_record", fake_post_record)

        result = answer_question("sub_1", "Routed answer", source="phone", actor="jane")
        assert calls == [
            (
                "/portal-answer",
                {
                    "submission_id": "sub_1",
                    "text": "Routed answer",
                    "source": "phone",
                    "revision": None,
                    "actor": "jane",
                },
            )
        ]
        assert result.seq == 3
        assert result.question_revision == 1
        assert result.payload == {"relayed": True, "source": "phone"}
        # ...and nothing was written into this (thin client's) own local DB.
        assert ledger_for_submission("sub_1") == []


def test_daemon_portal_answer_endpoint(tmp_path) -> None:
    """#2986: POST `/portal-answer` — the relayed-answer daemon seam,
    mirroring `test_daemon_portal_note_endpoint` above."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.portal_store import enqueue, ledger_for_submission, mark_applied
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-answer.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    row = enqueue("sub_1", "question", {"question": "Offline-first?"})
    mark_applied(row)

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing_submission = cli.post("/portal-answer", json={"text": "hi"})
        unknown_submission = cli.post(
            "/portal-answer", json={"submission_id": "sub_nope", "text": "hi"}
        )
        empty_text = cli.post(
            "/portal-answer", json={"submission_id": "sub_1", "text": "   "}
        )
        bad_source = cli.post(
            "/portal-answer",
            json={"submission_id": "sub_1", "text": "Yes.", "source": "carrier-pigeon"},
        )
        answered = cli.post(
            "/portal-answer",
            json={
                "submission_id": "sub_1",
                "text": "Yes, offline-first.",
                "source": "phone",
                "actor": "jane",
            },
        )

    assert missing_submission.status_code == 400
    assert unknown_submission.status_code == 400
    assert "unknown submission" in unknown_submission.json()["error"]
    assert empty_text.status_code == 400
    assert bad_source.status_code == 400
    assert answered.status_code == 200
    entry = answered.json()["entry"]
    assert entry["kind"] == "question_answered"
    assert entry["question_revision"] == row.revision
    assert entry["text"] == "Yes, offline-first."
    assert entry["actor"] == "operator:jane"
    assert entry["payload_json"] == '{"relayed": true, "source": "phone"}'

    [stored] = [
        e for e in ledger_for_submission("sub_1") if e.kind == "question_answered"
    ]
    assert stored.text == "Yes, offline-first."


def test_daemon_portal_link_by_submission_endpoint(tmp_path) -> None:
    """#2995: GET `/portal-link-by-submission` — the reverse-lookup twin of
    `/portal-link` (test_daemon_portal_link_endpoints above), keyed on
    submission_id instead of (repo_name, milestone/issue). Backs
    `coord.portal_store.get_link_by_submission` on a thin client — the read
    `coord portal enqueue-status`'s #2996 "no link on file" warning needs
    once enqueue-status itself started routing through the daemon."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db, state
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-link-by-submission.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    state.save_portal_link(
        {
            "repo_name": "api",
            "milestone_number": 3,
            "issue_number": None,
            "submission_id": "sub_abc123",
            "linked_at": 0.0,
            "actor": "tester",
            "schema": 1,
        }
    )

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        found = cli.get(
            "/portal-link-by-submission", params={"submission_id": "sub_abc123"}
        )
        missing = cli.get(
            "/portal-link-by-submission", params={"submission_id": "sub_nope"}
        )
        no_param = cli.get("/portal-link-by-submission")

    assert found.status_code == 200
    assert found.json()["link"]["submission_id"] == "sub_abc123"
    assert missing.json() == {"link": None}
    assert no_param.status_code == 400


def test_daemon_portal_enqueue_status_endpoint(tmp_path) -> None:
    """#2995: POST `/portal-enqueue-status` — `coord portal enqueue-status`
    executed on the daemon, mirroring `test_daemon_portal_decision_endpoint`
    above. The claim check itself lives client-side
    (`coord.commands.portal._refuse_unless_claiming_machine`) — this
    endpoint only asserts the write it performs once a request arrives."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.portal_store import outbox_for_submission
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-enqueue-status.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing_submission_id = cli.post(
            "/portal-enqueue-status", json={"status": "in-design"}
        )
        missing_status = cli.post(
            "/portal-enqueue-status", json={"submission_id": "sub_1"}
        )
        unknown_status = cli.post(
            "/portal-enqueue-status",
            json={"submission_id": "sub_1", "status": "on-fire"},
        )
        # `quality-check` announces a `preview` — refused with nothing queued.
        no_preview = cli.post(
            "/portal-enqueue-status",
            json={"submission_id": "sub_1", "status": "quality-check"},
        )
        ok = cli.post(
            "/portal-enqueue-status",
            json={"submission_id": "sub_1", "status": "in-design"},
        )

    assert missing_submission_id.status_code == 400
    assert missing_status.status_code == 400
    assert unknown_status.status_code == 400
    assert "not in the pinned portal status vocabulary" in unknown_status.json()["error"]
    assert no_preview.status_code == 400
    assert ok.status_code == 200
    row = ok.json()["row"]
    assert row["submission_id"] == "sub_1"
    assert row["kind"] == "status"
    assert row["fields_json"] == '{"status": "in-design"}'

    [stored] = outbox_for_submission("sub_1")
    assert stored.kind == "status"
    assert stored.fields == {"status": "in-design"}


def test_daemon_portal_enqueue_question_endpoint_applies_both_rows_atomically(
    tmp_path,
) -> None:
    """#2995: POST `/portal-enqueue-question` — `coord portal
    enqueue-question` executed on the daemon. The design note this issue's
    acceptance bar calls out: the question row and its `needs-input`
    announcement (#2901) must both land from this ONE request, so a thin
    client can never observe (or a crash never leave behind) a question
    with no status row behind it."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.portal_store import outbox_for_submission
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-enqueue-question.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing_submission_id = cli.post(
            "/portal-enqueue-question", json={"question": "Offline-first?"}
        )
        empty_question = cli.post(
            "/portal-enqueue-question",
            json={"submission_id": "sub_1", "question": "   "},
        )
        ok = cli.post(
            "/portal-enqueue-question",
            json={"submission_id": "sub_1", "question": "Offline-first?"},
        )

    assert missing_submission_id.status_code == 400
    assert empty_question.status_code == 400
    assert ok.status_code == 200
    body = ok.json()
    question_row = body["question_row"]
    status_row = body["status_row"]
    assert question_row["kind"] == "question"
    assert question_row["fields_json"] == '{"question": "Offline-first?"}'
    assert status_row["kind"] == "status"
    assert status_row["fields_json"] == '{"status": "needs-input"}'
    # seq N (question) immediately followed by N+1 (its own announcement) —
    # both applied in this one request, never observably apart.
    assert status_row["seq"] == question_row["seq"] + 1

    stored = outbox_for_submission("sub_1")
    assert [r.kind for r in stored] == ["question", "status"]


# ── #2990: dashboard reads gating a browser client's relayed answer ────────


def test_daemon_portal_needs_input_endpoint(tmp_path) -> None:
    """#2990: GET `/portal-needs-input` — the daemon seam backing the
    dashboard's `GET /api/portal/needs-input` off the daemon host, mirroring
    `test_daemon_portal_ledger_endpoint` above."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.portal_store import enqueue, mark_applied
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-needs-input.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    status_row = enqueue("sub_1", "status", {"status": "needs-input"})
    mark_applied(status_row)
    question_row = enqueue("sub_1", "question", {"question": "Offline-first?"})
    mark_applied(question_row)

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        r = cli.get("/portal-needs-input")

    assert r.status_code == 200
    assert r.json() == {
        "submissions": [
            {
                "submission_id": "sub_1",
                "question_revision": question_row.revision,
                "question": "Offline-first?",
            }
        ]
    }


def test_daemon_portal_answer_preflight_endpoint(tmp_path) -> None:
    """#2990: GET `/portal-answer-preflight` — the daemon seam backing the
    dashboard's `POST /api/portal/answer` gating checks off the daemon
    host."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.portal_store import answer_question, enqueue, mark_applied
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-portal-answer-preflight.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    row = enqueue("sub_1", "question", {"question": "Offline-first?"})
    mark_applied(row)

    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing_param = cli.get("/portal-answer-preflight")
        unknown = cli.get(
            "/portal-answer-preflight", params={"submission_id": "sub_nope"}
        )
        before_answer = cli.get(
            "/portal-answer-preflight", params={"submission_id": "sub_1"}
        )

    assert missing_param.status_code == 400
    assert unknown.status_code == 404
    assert before_answer.status_code == 200
    preflight = before_answer.json()["preflight"]
    assert preflight["current_open_revision"] == row.revision
    assert preflight["relayed_answers"] == []

    answer_question("sub_1", "Yes, offline-first.", source="phone", actor="jane")

    with TestClient(app) as cli:
        after_answer = cli.get(
            "/portal-answer-preflight", params={"submission_id": "sub_1"}
        )

    assert after_answer.status_code == 200
    preflight = after_answer.json()["preflight"]
    # The question is now answered, so there is no open question left.
    assert preflight["current_open_revision"] is None
    [relayed] = preflight["relayed_answers"]
    assert relayed["question_revision"] == row.revision
    assert relayed["text"] == "Yes, offline-first."
    assert relayed["payload_json"] == '{"relayed": true, "source": "phone"}'


class TestNeedsInputAndAnswerPreflightRouteToTheDaemon:
    """#2990 fix round: `needs_input_submissions()` and `answer_preflight()`
    must resolve `board_service` and route to the daemon exactly like
    `answer_question`'s own `_route_answer` already does for the write —
    mirroring `TestOperatorNotes.test_append_routes_to_the_daemon_when_
    board_service_is_set` above. Before this fix, both read straight off the
    local DB unconditionally, which is silently wrong on a thin client
    (`coord/portal_store.py`'s own module docstring)."""

    def test_needs_input_submissions_routes_to_the_daemon_when_board_service_is_set(
        self, coord_db, monkeypatch
    ) -> None:
        import coord.client as cc
        from coord.portal_store import needs_input_submissions

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        calls = []

        def fake_get(url, **kw):
            calls.append((url, kw))

            class _Resp:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "submissions": [
                            {
                                "submission_id": "sub_1",
                                "question_revision": 1,
                                "question": "Offline-first?",
                            }
                        ]
                    }

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        result = needs_input_submissions()

        assert len(calls) == 1
        assert calls[0][0] == "http://daemon:7435/portal-needs-input"
        assert result == [
            {
                "submission_id": "sub_1",
                "question_revision": 1,
                "question": "Offline-first?",
            }
        ]

    def test_answer_preflight_routes_to_the_daemon_when_board_service_is_set(
        self, coord_db, monkeypatch
    ) -> None:
        import coord.client as cc
        from coord.portal_store import answer_preflight, ledger_for_submission

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        calls = []

        def fake_get(url, **kw):
            calls.append((url, kw))

            class _Resp:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "preflight": {
                            "current_open_revision": 2,
                            "relayed_answers": [],
                        }
                    }

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        result = answer_preflight("sub_1")

        assert len(calls) == 1
        assert calls[0][0] == "http://daemon:7435/portal-answer-preflight"
        assert calls[0][1]["params"] == {"submission_id": "sub_1"}
        assert result == {"current_open_revision": 2, "relayed_answers": []}
        # ...and nothing was read off this (thin client's) own local DB —
        # there is no local submission at all, so a local read would have
        # raised or returned None instead of the daemon's answer.
        assert ledger_for_submission("sub_1") == []

    def test_answer_preflight_returns_none_for_daemon_404(
        self, coord_db, monkeypatch
    ) -> None:
        import coord.client as cc
        from coord.portal_store import answer_preflight

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

        def fake_get(url, **kw):
            class _Resp:
                status_code = 404

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        assert answer_preflight("sub_nope") is None


# ── #3071: `coord journal` — one submission's run, in order ────────────────
#
# The join half of #3071 (the emission half lives in
# `tests/test_portal_sync.py`): `portal_ledger` + the applied outbox + the
# sign-off inbox + the business tier of the audit trail, folded into one
# ordered, timestamped narrative — and the CLI that renders it.
#
# The black-box bar (CLAUDE.md, "Testing"): `TestJournalCommand` below drives
# the real `coord journal` through Click's runner and asserts on rendered
# stdout, not just on the aggregator's return value.

JSUB = "sub-journal"


def _seed_applied(kind: str, fields: dict, *, now: float):
    """Queue a coord-owned fact and mark it applied, as a successful push
    does — the state the journal's outbox fold reads."""
    from coord import portal_store

    row = portal_store.enqueue(JSUB, kind, fields, now=now)
    portal_store.mark_applied(row, now=now)
    return row


def _seed_signoff(verdict: str = "approved", comments: str = "ship it", *, now: float):
    from coord import portal_store

    portal_store.record_events(
        [
            {
                "id": f"ev-{verdict}",
                "submission_id": JSUB,
                "type": f"signoff.{verdict}",
                "data": {"verdict": verdict, "comments": comments},
            }
        ],
        now=now,
    )


class TestJournalUrl:
    """`artifact` is contract-bound to "null or a URL" (#3071's `--json`
    bullet), so a bare R2 object key must not be smuggled into it."""

    def test_accepts_real_urls(self) -> None:
        from coord.portal_store import _journal_url

        assert _journal_url("https://pr-7.pages.dev") == "https://pr-7.pages.dev"
        assert _journal_url("r2://bundles/1") == "r2://bundles/1"

    def test_rejects_a_bare_object_key_and_junk(self) -> None:
        from coord.portal_store import _journal_url

        assert _journal_url("bundles/sub-001/r1.tar") is None
        assert _journal_url("") is None
        assert _journal_url(None) is None
        assert _journal_url(7) is None
        assert _journal_url("://nope") is None


class TestPreviewPublishedLedgerRow:
    """#3071: `enqueue-preview`'s APPLY path — the moment the customer can
    actually open it, not the moment it was queued."""

    def test_applying_a_preview_row_ledgers_it_with_the_url(self, coord_db) -> None:
        from coord import portal_store

        row = _seed_applied("preview", {"preview_url": "https://pr-7.pages.dev"}, now=10.0)

        [entry] = portal_store.ledger_for_submission(JSUB)
        assert entry.kind == portal_store.LEDGER_KIND_PREVIEW_PUBLISHED
        assert entry.text == "https://pr-7.pages.dev"
        assert entry.source_event_id == portal_store.outbox_source_key(row.id)

    def test_re_applying_the_same_row_does_not_duplicate(self, coord_db) -> None:
        from coord import portal_store

        row = portal_store.enqueue(
            JSUB, "preview", {"preview_url": "https://pr-7.pages.dev"}
        )
        portal_store.mark_applied(row)
        portal_store.mark_applied(row)

        assert len(portal_store.ledger_for_submission(JSUB)) == 1

    def test_a_status_row_is_not_mistaken_for_a_preview(self, coord_db) -> None:
        from coord import portal_store

        _seed_applied("status", {"status": "planned"}, now=10.0)
        assert portal_store.ledger_for_submission(JSUB) == []


class TestJournalIssueNumbers:
    def test_an_issue_scoped_link_names_exactly_its_own_issue(self, coord_db) -> None:
        from coord import portal_store

        portal_store.link_issue(
            repo_name="acme", issue_number=42, submission_id=JSUB
        )
        link = portal_store.get_link_by_submission(JSUB)
        assert portal_store.journal_issue_numbers(JSUB, link) == [42]

    def test_a_milestone_link_reads_the_design_round_decomposition(
        self, coord_db
    ) -> None:
        """A `PortalLink` carries only the milestone number, and resolving its
        members off GitHub is a live call this read must never make. The set
        coord already told the CUSTOMER about is the right one."""
        from coord import portal_store

        portal_store.link_milestone(
            repo_name="acme", milestone_number=4, submission_id=JSUB
        )
        _seed_applied(
            "design_round",
            {
                "design_round": {
                    "round": 1,
                    "bundle_key": "r2://bundles/4",
                    "decomposition": [
                        {"issue_number": 11}, {"issue_number": 9}, {"group": "x"},
                    ],
                }
            },
            now=10.0,
        )
        link = portal_store.get_link_by_submission(JSUB)
        assert portal_store.journal_issue_numbers(JSUB, link) == [9, 11]

    def test_no_link_and_no_decomposition_resolve_to_nothing(self, coord_db) -> None:
        from coord import portal_store

        assert portal_store.journal_issue_numbers(JSUB, None) == []
        portal_store.link_milestone(
            repo_name="acme", milestone_number=4, submission_id=JSUB
        )
        link = portal_store.get_link_by_submission(JSUB)
        assert portal_store.journal_issue_numbers(JSUB, link) == []


class TestRenderJournalPayload:
    def test_an_unlinked_submission_is_an_empty_timeline_not_an_error(
        self, coord_db
    ) -> None:
        """#3071's second acceptance bullet."""
        from coord import portal_store

        payload = portal_store.render_journal_payload("sub-never-seen")

        assert payload["entries"] == []
        assert payload["link"] is None
        assert any("no repo/milestone linked" in g for g in payload["gaps"])

    def test_every_entry_carries_the_pinned_keys(self, coord_db) -> None:
        """The `--json` contract a renderer builds against: `ts`, `kind`,
        `actor`, `text`, and an `artifact` that is null or a URL."""
        from coord import portal_store

        _seed_applied("preview", {"preview_url": "https://pr-7.pages.dev"}, now=30.0)
        _seed_applied(
            "design_round",
            {"design_round": {"round": 1, "bundle_key": "r2://bundles/4"}},
            now=20.0,
        )
        _seed_signoff(now=40.0)

        payload = portal_store.render_journal_payload(JSUB)

        assert payload["entries"]
        for entry in payload["entries"]:
            assert set(entry) >= {"ts", "kind", "actor", "text", "artifact"}
            assert isinstance(entry["ts"], float)
            assert isinstance(entry["kind"], str) and entry["kind"]
            assert isinstance(entry["actor"], str)
            assert isinstance(entry["text"], str)
            assert entry["artifact"] is None or "://" in entry["artifact"]

    def test_entries_are_ordered_oldest_first(self, coord_db) -> None:
        from coord import portal_store

        _seed_signoff(now=40.0)
        _seed_applied("preview", {"preview_url": "https://pr-7.pages.dev"}, now=30.0)
        _seed_applied(
            "design_round",
            {"design_round": {"round": 1, "bundle_key": "r2://bundles/4"}},
            now=20.0,
        )

        kinds = [e["kind"] for e in portal_store.render_journal_payload(JSUB)["entries"]]
        assert kinds == [
            portal_store.LEDGER_KIND_DESIGN_ROUND_PUBLISHED,
            portal_store.LEDGER_KIND_PREVIEW_PUBLISHED,
            portal_store.LEDGER_KIND_SIGNOFF_RECORDED,
        ]

    def test_a_design_round_published_before_this_shipped_still_renders(
        self, coord_db
    ) -> None:
        """The ledger has no UPDATE path and backfilling one would be a
        fiction, so a round pushed before #3071 is read off the applied
        outbox row that IS the durable record of it — bundle key included."""
        from coord import portal_store

        _seed_applied(
            "design_round",
            {"design_round": {"round": 2, "bundle_key": "r2://bundles/4"}},
            now=20.0,
        )

        [entry] = portal_store.render_journal_payload(JSUB)["entries"]
        assert entry["kind"] == portal_store.LEDGER_KIND_DESIGN_ROUND_PUBLISHED
        assert entry["source"] == portal_store.JOURNAL_SOURCE_OUTBOX
        assert entry["text"] == "design round R2 published (bundle r2://bundles/4)"
        assert entry["artifact"] == "r2://bundles/4"

    def test_a_realistic_bare_bundle_key_yields_no_artifact_but_is_not_lost(
        self, coord_db
    ) -> None:
        """Review finding on #3071: every seeded ``bundle_key`` elsewhere in
        this file already looks like a URL (``"r2://bundles/4"``), which only
        passes :func:`portal_store._journal_url` by accident of spelling —
        real data never does. :meth:`coord.portal_bridge.PortalBridgeClient.
        upload_bundle` returns a bare R2 object key, e.g.
        ``"bundles/sub-001/r2.tar"``, and nothing anywhere writes a
        ``bundle_url``. Against that realistic shape, ``artifact`` must be
        ``None`` — never a bare key smuggled past the "null or a URL"
        contract — while the raw key must still be readable in ``text`` and
        ``details`` so the operator watching the timeline isn't left with
        nothing to point at.
        """
        from coord import portal_store

        _seed_applied(
            "design_round",
            {"design_round": {"round": 2, "bundle_key": "bundles/sub-001/r2.tar"}},
            now=20.0,
        )

        [entry] = portal_store.render_journal_payload(JSUB)["entries"]
        assert entry["kind"] == portal_store.LEDGER_KIND_DESIGN_ROUND_PUBLISHED
        assert entry["artifact"] is None
        assert entry["text"] == (
            "design round R2 published (bundle bundles/sub-001/r2.tar)"
        )
        assert entry["details"]["bundle_key"] == "bundles/sub-001/r2.tar"

    def test_a_ledgered_design_round_is_not_also_shown_from_the_outbox(
        self, coord_db
    ) -> None:
        from coord import portal_store

        row = _seed_applied(
            "design_round",
            {"design_round": {"round": 1, "bundle_key": "r2://bundles/4"}},
            now=20.0,
        )
        portal_store.append_ledger_entry(
            JSUB,
            portal_store.LEDGER_KIND_DESIGN_ROUND_PUBLISHED,
            text="design round R1 published (bundle r2://bundles/4)",
            actor="coord",
            source_event_id=portal_store.outbox_source_key(row.id),
            payload={"bundle_key": "r2://bundles/4"},
            now=21.0,
        )

        entries = portal_store.render_journal_payload(JSUB)["entries"]
        rounds = [
            e for e in entries
            if e["kind"] == portal_store.LEDGER_KIND_DESIGN_ROUND_PUBLISHED
        ]
        assert len(rounds) == 1
        assert rounds[0]["source"] == portal_store.JOURNAL_SOURCE_LEDGER

    def test_a_ledgered_signoff_is_not_also_shown_from_the_inbox(
        self, coord_db
    ) -> None:
        from coord import portal_store

        _seed_signoff(now=40.0)
        portal_store.append_ledger_entry(
            JSUB,
            portal_store.LEDGER_KIND_SIGNOFF_RECORDED,
            text="sign-off: approved — ship it",
            actor="customer",
            source_event_id="ev-approved",
            payload={"verdict": "approved"},
            now=41.0,
        )

        entries = portal_store.render_journal_payload(JSUB)["entries"]
        signoffs = [
            e for e in entries
            if e["kind"] == portal_store.LEDGER_KIND_SIGNOFF_RECORDED
        ]
        assert len(signoffs) == 1
        assert signoffs[0]["source"] == portal_store.JOURNAL_SOURCE_LEDGER

    def test_a_pending_outbox_row_is_not_yet_a_published_artifact(
        self, coord_db
    ) -> None:
        from coord import portal_store

        portal_store.enqueue(
            JSUB, "preview", {"preview_url": "https://pr-7.pages.dev"}
        )
        assert portal_store.render_journal_payload(JSUB)["entries"] == []

    def test_business_tier_dispatch_and_merge_rows_are_folded_in(
        self, coord_db
    ) -> None:
        """`--tier business` is exactly the right filter and the split exists
        for this: operational-tier housekeeping is not what "what is
        happening with my project" means."""
        from coord import portal_store
        from coord.audit import record_audit

        portal_store.link_issue(
            repo_name="acme", issue_number=42, submission_id=JSUB
        )
        record_audit(
            tier="business", category="dispatch", event_type="dispatched",
            actor="drive", summary="Dispatched work to precision: acme#42",
            repo="acme", issue=42, ts=50.0,
        )
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="Merged: acme#42",
            repo="acme", issue=42, ts=60.0,
        )
        record_audit(
            tier="business", category="review", event_type="review_approved",
            actor="coordinator", summary="Review approved: acme#42",
            repo="acme", issue=42, ts=55.0,
        )
        record_audit(
            tier="operational", category="tick", event_type="dispatched",
            actor="daemon", summary="tick housekeeping",
            repo="acme", issue=42, ts=51.0,
        )

        entries = portal_store.render_journal_payload(JSUB)["entries"]
        assert [(e["kind"], e["actor"]) for e in entries] == [
            ("dispatched", "drive"), ("merged", "coordinator"),
        ]
        assert entries[0]["source"] == portal_store.JOURNAL_SOURCE_AUDIT

    def test_a_merged_rows_pr_url_becomes_the_artifact(self, coord_db) -> None:
        """Review finding on #3071: `mark_assignment_merged` now threads
        `assignments.pr_url` into `audit_log.details`, so this is the one
        artifact kind #3071 promises that a real PR merge can actually
        deliver — proved with a realistic merged row, not one hand-built
        with the field already present."""
        from coord import portal_store
        from coord.audit import record_audit

        portal_store.link_issue(
            repo_name="acme", issue_number=42, submission_id=JSUB
        )
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="Merged: acme#42",
            repo="acme", issue=42, ts=60.0,
            details={"pr_url": "https://github.com/acme/api/pull/7"},
        )

        [entry] = portal_store.render_journal_payload(JSUB)["entries"]
        assert entry["kind"] == "merged"
        assert entry["artifact"] == "https://github.com/acme/api/pull/7"

    def test_a_merged_row_with_no_pr_url_on_file_has_no_artifact(
        self, coord_db
    ) -> None:
        """The realistic gap case the review asked for: a merge recorded
        with no `pr_url` in `details` (a direct out-of-band merge, or one
        from before #3071 wired the board's PR URL through) must not raise
        and must not fabricate a URL."""
        from coord import portal_store
        from coord.audit import record_audit

        portal_store.link_issue(
            repo_name="acme", issue_number=42, submission_id=JSUB
        )
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="Merged: acme#42",
            repo="acme", issue=42, ts=60.0,
        )

        [entry] = portal_store.render_journal_payload(JSUB)["entries"]
        assert entry["kind"] == "merged"
        assert entry["artifact"] is None

    def test_a_long_business_tier_history_does_not_drop_the_oldest_row(
        self, coord_db, monkeypatch
    ) -> None:
        """Review finding on #3071: `list_audit_log` is newest-first, so a
        naive single `limit=200` page would silently drop the earliest
        `dispatched` row once an issue accumulates enough review/test
        churn — exactly what "intake to shipped" must not lose. Paginate
        with a tiny page size here so the fold is forced across >1 page."""
        from coord import portal_store, state

        portal_store.link_issue(
            repo_name="acme", issue_number=42, submission_id=JSUB
        )
        monkeypatch.setattr(portal_store, "_JOURNAL_AUDIT_PAGE_SIZE", 2)

        from coord.audit import record_audit

        record_audit(
            tier="business", category="dispatch", event_type="dispatched",
            actor="drive", summary="Dispatched work to precision: acme#42",
            repo="acme", issue=42, ts=10.0,
        )
        for i in range(5):
            record_audit(
                tier="business", category="review", event_type="review_approved",
                actor="coordinator", summary=f"noise {i}",
                repo="acme", issue=42, ts=20.0 + i,
            )
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="Merged: acme#42",
            repo="acme", issue=42, ts=90.0,
        )

        entries = portal_store.render_journal_payload(JSUB)["entries"]
        assert [e["kind"] for e in entries] == ["dispatched", "merged"]

    def test_another_submissions_issue_never_leaks_into_this_timeline(
        self, coord_db
    ) -> None:
        """Two submissions in one repo is the normal case; showing each
        client the other's dispatches is not an option."""
        from coord import portal_store
        from coord.audit import record_audit

        portal_store.link_issue(
            repo_name="acme", issue_number=42, submission_id=JSUB
        )
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="Merged: acme#99",
            repo="acme", issue=99, ts=60.0,
        )

        assert portal_store.render_journal_payload(JSUB)["entries"] == []

    def test_an_unreadable_audit_trail_is_a_gap_not_an_exception(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import portal_store, state

        portal_store.link_issue(
            repo_name="acme", issue_number=42, submission_id=JSUB
        )

        def _boom(**_kw):
            raise RuntimeError("daemon unreachable")

        monkeypatch.setattr(state, "list_audit_log", _boom)

        payload = portal_store.render_journal_payload(JSUB)
        assert any("daemon unreachable" in g for g in payload["gaps"])

    def test_an_unreadable_ledger_is_a_gap_not_an_exception(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import portal_store

        def _boom(_submission_id):
            raise RuntimeError("locked database")

        monkeypatch.setattr(portal_store, "ledger_for_submission", _boom)

        payload = portal_store.render_journal_payload(JSUB)
        assert payload["entries"] == []
        assert any("ledger unreadable" in g for g in payload["gaps"])

    def test_the_qa_history_already_on_the_ledger_is_part_of_the_timeline(
        self, coord_db
    ) -> None:
        from coord import portal_store

        portal_store.append_ledger_entry(
            JSUB, portal_store.LEDGER_KIND_QUESTION_PUSHED,
            question_revision=1, text="Offline-first?", actor="coord", now=5.0,
        )
        portal_store.append_ledger_entry(
            JSUB, portal_store.LEDGER_KIND_QUESTION_ANSWERED,
            question_revision=1, text="Yes.", actor="jane",
            source_event_id="ev-answer", now=6.0,
        )

        entries = portal_store.render_journal_payload(JSUB)["entries"]
        assert [(e["kind"], e["text"]) for e in entries] == [
            ("question_pushed", "Offline-first?"),
            ("question_answered", "Yes."),
        ]


class TestJournalCommand:
    """Black-box: the real `coord journal`, driven end to end, asserted on
    its rendered output."""

    def _run(self, *args):
        from click.testing import CliRunner

        from coord.cli import main

        return CliRunner().invoke(main, ["journal", *args])

    def test_renders_the_run_in_order_with_the_bundle_reference(
        self, coord_db
    ) -> None:
        from coord import portal_store

        portal_store.link_milestone(
            repo_name="acme", milestone_number=4, submission_id=JSUB
        )
        _seed_applied(
            "design_round",
            {"design_round": {"round": 1, "bundle_key": "r2://bundles/4"}},
            now=1_700_000_000.0,
        )
        _seed_signoff(now=1_700_000_100.0)

        result = self._run(JSUB)

        assert result.exit_code == 0, result.output
        assert f"# Journal — {JSUB}" in result.output
        assert "linked to acme ms-4" in result.output
        assert "r2://bundles/4" in result.output
        design_at = result.output.index("design round")
        signoff_at = result.output.index("sign-off")
        assert design_at < signoff_at
        assert "ship it" in result.output

    def test_an_unlinked_submission_prints_an_empty_timeline_and_exits_zero(
        self, coord_db
    ) -> None:
        result = self._run("sub-never-seen")

        assert result.exit_code == 0, result.output
        assert "(no recorded activity yet)" in result.output
        assert "## Gaps" in result.output

    def test_json_is_the_shape_a_renderer_builds_against(self, coord_db) -> None:
        import json as _json

        from coord import portal_store

        portal_store.link_milestone(
            repo_name="acme", milestone_number=4, submission_id=JSUB
        )
        _seed_applied(
            "preview", {"preview_url": "https://pr-7.pages.dev"}, now=1_700_000_000.0,
        )

        result = self._run(JSUB, "--json")

        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output)
        assert payload["submission_id"] == JSUB
        assert payload["link"]["repo_name"] == "acme"
        assert isinstance(payload["gaps"], list)
        [entry] = payload["entries"]
        assert set(entry) >= {"ts", "kind", "actor", "text", "artifact"}
        assert entry["kind"] == "preview_published"
        assert entry["artifact"] == "https://pr-7.pages.dev"

    def test_json_stays_valid_when_a_source_is_unreadable(
        self, coord_db, monkeypatch
    ) -> None:
        """A gap must never become a traceback on the one command a client is
        watching over your shoulder."""
        import json as _json

        from coord import portal_store

        def _boom(_submission_id):
            raise RuntimeError("locked database")

        monkeypatch.setattr(portal_store, "ledger_for_submission", _boom)

        result = self._run(JSUB, "--json")

        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output)
        assert payload["entries"] == []
        assert any("locked database" in g for g in payload["gaps"])

    def test_a_thin_client_says_so_rather_than_reading_as_nothing_happened(
        self, coord_db, monkeypatch
    ) -> None:
        """#2336's failure mode, in this surface: the bridge's tables live on
        the daemon host, so an empty timeline here means "wrong box", not
        "no activity"."""
        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

        result = self._run(JSUB)

        assert result.exit_code == 0, result.output
        assert "thin client" in result.output
