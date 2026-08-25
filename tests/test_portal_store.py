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
