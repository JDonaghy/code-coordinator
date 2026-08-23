"""#1982: the portal sync bridge — pull, push, ordering, replay, heartbeat.

The tests that matter most here are the ORDERING ones. The portal accepts a
push of `awaiting-signoff` with no design round attached and immediately
emails the customer "your design is ready — approve it or tell us what to
change", landing them on an empty screen (measured in production 2026-08-14,
dogfood #835). `status` and `design_round` are separate coord-owned fields
and the portal enforces no ordering between them because it cannot. So the
ordering is ours to guarantee, and these tests are the guarantee.
"""

from __future__ import annotations

import pytest

from coord import portal_store, portal_sync
from coord.portal_bridge import PortalBridgeError, PushResult
from coord.portal_sync import (
    PortalSyncError,
    enqueue_design_round,
    enqueue_preview,
    enqueue_question,
    enqueue_status,
    sync_tick,
)

SUB = "sub-001"


class FakeClient:
    """Records every call; scripted pull pages and push outcomes."""

    def __init__(
        self,
        pages: list[dict] | None = None,
        *,
        push_outcomes: dict[str, str] | None = None,
        push_error: Exception | None = None,
        pull_error: Exception | None = None,
        heartbeat_error: Exception | None = None,
        heartbeat_ok: bool = True,
    ):
        self.pages = list(pages or [{"events": [], "cursor": None, "has_more": False}])
        self.push_outcomes = push_outcomes or {}
        self.push_error = push_error
        self.pull_error = pull_error
        self.heartbeat_error = heartbeat_error
        self.heartbeat_ok = heartbeat_ok
        self.pull_calls: list[str | None] = []
        self.pushes: list[dict] = []
        self.heartbeats = 0

    def pull(self, cursor=None, limit=None):
        self.pull_calls.append(cursor)
        if self.pull_error:
            raise self.pull_error
        if not self.pages:
            return {"events": [], "cursor": cursor, "has_more": False}
        return self.pages.pop(0)

    def push(self, updates):
        if self.push_error:
            raise self.push_error
        results = []
        for u in updates:
            self.pushes.append(u.to_wire())
            key = next(iter(u.fields))  # rows are single-field by construction
            outcome = self.push_outcomes.get(key, "applied")
            results.append(
                PushResult(
                    submission_id=u.submission_id,
                    outcome=outcome,
                    reason=None if outcome != "rejected" else f"not_owned:{key}",
                )
            )
        return results

    def heartbeat(self, at=None):
        self.heartbeats += 1
        if self.heartbeat_error:
            raise self.heartbeat_error
        return self.heartbeat_ok

    # convenience for assertions
    @property
    def pushed_kinds(self) -> list[str]:
        return [next(iter(p["fields"])) for p in self.pushes]


def _design(round_no: int = 1) -> dict:
    return {"round": round_no, "outcome": "a thing", "bundle_url": "r2://b/1"}


# ── the ordering rule (#835) ────────────────────────────────────────────────


def test_enqueue_status_refuses_awaiting_signoff_with_no_design_round():
    with pytest.raises(PortalSyncError) as exc:
        enqueue_status(SUB, "awaiting-signoff")
    assert "design_round" in str(exc.value)
    assert portal_store.pending_outbox() == []


def test_enqueue_status_refuses_needs_input_with_no_question():
    with pytest.raises(PortalSyncError) as exc:
        enqueue_status(SUB, "needs-input")
    assert "question" in str(exc.value)


def test_non_announcing_status_needs_no_prerequisite():
    row = enqueue_status(SUB, "in-design")
    assert row.requires_kind == ""
    assert row.announces == ""


def test_design_round_is_pushed_before_the_status_that_announces_it():
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    client = FakeClient()

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["design_round", "status"]
    assert result.applied == 2
    assert result.held == 0
    # ...and they were separate calls, so the design round was CONFIRMED
    # applied before the announcement was even sent.
    assert len(client.pushes) == 2


def test_announcement_is_held_while_its_design_round_is_unconfirmed():
    """The crash-window case: the design round push fails, so the mail must not go."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    client = FakeClient(push_error=PortalBridgeError("portal is down"))

    result = sync_tick(client=client)

    assert client.pushes == []  # nothing landed
    assert result.applied == 0
    assert result.errors  # the failure is surfaced, not swallowed

    # Next tick, the portal is back: the design round goes first, then the
    # announcement — same revisions, no duplicates.
    client2 = FakeClient()
    result2 = sync_tick(client=client2)
    assert client2.pushed_kinds == ["design_round", "status"]
    assert result2.applied == 2


def test_announcement_stays_held_when_its_design_round_is_rejected():
    """A rejected design round is terminal — the announcement must never go."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    client = FakeClient(push_outcomes={"design_round": "rejected"})

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["design_round"]
    assert result.rejected == 1
    assert result.applied == 0

    # ...and it stays held on every subsequent tick, rather than eventually
    # leaking out.
    client2 = FakeClient()
    result2 = sync_tick(client=client2)
    assert client2.pushes == []
    assert result2.held == 1


def test_already_applied_on_a_first_attempt_is_not_confirmation():
    """#835 from the other side: the portal ignored it, so nothing landed.

    `already_applied` means "at or below my watermark, discarded". On a
    row's FIRST attempt there was no earlier send it could be acknowledging,
    so it means coord's revision allocator is behind the portal — the design
    round was NOT stored, and treating it as confirmed would release the
    `awaiting-signoff` mail toward an empty screen.
    """
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    client = FakeClient(push_outcomes={"design_round": "already_applied"})

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["design_round"]  # the mail never went
    assert result.applied == 0
    assert portal_store.get_submission(SUB).design_round == 0

    # ...and the row was re-numbered above the allocator so the retry can
    # clear the portal's watermark.
    row = portal_store.outbox_for_submission(SUB)[0]
    assert row.state == portal_store.STATE_PENDING
    assert row.revision > 1


def test_already_applied_on_a_retry_is_confirmation():
    """A resend of a row we really did send: the lost-response case."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")

    # Attempt 1 fails in transport — the portal may well have stored it.
    sync_tick(client=FakeClient(push_error=PortalBridgeError("timeout")))
    # Attempt 2 comes back already_applied, which now means what it says.
    client = FakeClient(push_outcomes={"design_round": "already_applied"})
    result = sync_tick(client=client)

    assert client.pushed_kinds == ["design_round", "status"]
    assert result.applied == 2


def test_reallocation_converges_and_then_the_announcement_goes():
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    sync_tick(client=FakeClient(push_outcomes={"design_round": "already_applied"}))

    client = FakeClient()  # the re-numbered revision now clears the watermark
    result = sync_tick(client=client)
    assert client.pushed_kinds == ["design_round", "status"]
    assert result.applied == 2
    assert portal_store.get_submission(SUB).last_status == "awaiting-signoff"


def test_second_question_cannot_ride_on_the_first_questions_confirmation():
    enqueue_question(SUB, "which colour?")
    enqueue_status(SUB, "needs-input")
    sync_tick(client=FakeClient())  # round 1 lands cleanly

    enqueue_question(SUB, "which font?")
    enqueue_status(SUB, "needs-input")
    # The second question fails to send; its announcement must not overtake it.
    client = FakeClient(push_error=PortalBridgeError("boom"))
    sync_tick(client=client)
    assert client.pushes == []

    client2 = FakeClient()
    sync_tick(client=client2)
    assert client2.pushed_kinds == ["question", "status"]


def test_a_stalled_submission_does_not_stall_another_customers():
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    enqueue_status("sub-002", "in-progress")

    # sub-001's design round is rejected, so its announcement is held; the
    # other submission must still move.
    client = FakeClient(push_outcomes={"design_round": "rejected"})
    result = sync_tick(client=client)

    assert result.rejected == 1
    assert [p["submission_id"] for p in client.pushes] == [SUB, "sub-002"]
    assert result.applied == 1


# ── idempotency and revisions ───────────────────────────────────────────────


def test_a_retry_reuses_the_same_revision():
    enqueue_status(SUB, "in-progress")
    failing = FakeClient(push_error=PortalBridgeError("transient"))
    sync_tick(client=failing)

    ok = FakeClient()
    sync_tick(client=ok)
    sync_tick(client=ok)  # nothing left pending

    assert [p["revision"] for p in ok.pushes] == [1]


def test_revisions_are_monotonic_per_submission():
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    enqueue_status("sub-002", "planned")

    rows = portal_store.pending_outbox()
    by_sub: dict[str, list[int]] = {}
    for r in rows:
        by_sub.setdefault(r.submission_id, []).append(r.revision)
    assert by_sub[SUB] == [1, 2]
    assert by_sub["sub-002"] == [1]


def test_pulled_revision_seeds_the_allocator_above_the_portal_watermark():
    """Otherwise the first push comes back already_applied and silently drops."""
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"id": "e1", "submission_id": SUB, "type": "submission.created",
                     "revision": 7}
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    sync_tick(client=client)

    row = enqueue_status(SUB, "in-design")
    assert row.revision == 8


# ── pull, cursor, replay ────────────────────────────────────────────────────


def test_pull_records_events_and_advances_the_cursor():
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"id": "e1", "submission_id": SUB, "type": "submission.created",
                     "at": "2026-08-14T10:00:00Z", "data": {"intake": "build me a thing"}},
                ],
                "cursor": "cursor-1",
                "has_more": False,
            }
        ]
    )
    result = sync_tick(client=client)

    assert result.pulled == 1
    assert portal_store.get_sync_state().pull_cursor == "cursor-1"
    events = portal_store.unhandled_events()
    assert [e.event_id for e in events] == ["e1"]
    assert events[0].payload["data"]["intake"] == "build me a thing"


def test_pull_starts_from_the_stored_cursor_on_the_next_pass():
    client = FakeClient(
        pages=[{"events": [], "cursor": "cursor-9", "has_more": False}]
    )
    sync_tick(client=client)
    client2 = FakeClient()
    sync_tick(client=client2)
    assert client2.pull_calls == ["cursor-9"]


def test_replaying_a_page_from_a_stale_cursor_inserts_nothing_twice():
    page = {
        "events": [{"id": "e1", "submission_id": SUB, "type": "signoff.approved"}],
        "cursor": "c1",
        "has_more": False,
    }
    first = sync_tick(client=FakeClient(pages=[dict(page)]))
    second = sync_tick(client=FakeClient(pages=[dict(page)]))

    assert first.pulled == 1
    assert second.pulled == 0
    assert len(portal_store.unhandled_events()) == 1


def test_cursor_does_not_advance_when_persisting_the_page_fails(monkeypatch):
    """A submission made while the daemon was down must queue, never vanish."""
    def _boom(*_a, **_kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(portal_store, "record_events", _boom)
    client = FakeClient(
        pages=[
            {
                "events": [{"id": "e1", "submission_id": SUB, "type": "x"}],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    result = sync_tick(client=client)

    assert result.pulled == 0
    assert result.errors
    assert portal_store.get_sync_state().pull_cursor is None


def test_pull_walks_multiple_pages_but_stops_at_the_page_budget():
    pages = [
        {
            "events": [{"id": f"e{i}", "submission_id": SUB, "type": "x"}],
            "cursor": f"c{i}",
            "has_more": True,
        }
        for i in range(5)
    ]
    client = FakeClient(pages=pages)
    result = sync_tick(client=client, pull_pages=3)

    assert result.pulled == 3
    assert portal_store.get_sync_state().pull_cursor == "c2"


def test_events_mirror_customer_facts_but_never_coord_owned_fields():
    client = FakeClient(
        pages=[
            {
                "events": [
                    {
                        "id": "e1",
                        "submission_id": SUB,
                        "type": "signoff.changes_requested",
                        "data": {
                            "verdict": "changes_requested",
                            "comments": "make it blue",
                            # The portal is not the writer of these; if one
                            # ever appears in an event it must NOT enter the
                            # mirror as if it were a customer fact.
                            "status": "in-design",
                            "design_round": {"round": 4},
                        },
                    }
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    sync_tick(client=client)

    record = portal_store.get_submission(SUB)
    assert record is not None
    assert record.customer["verdict"] == "changes_requested"
    assert record.customer["comments"] == "make it blue"
    assert "status" not in record.customer
    assert "design_round" not in record.customer


def test_events_wrapped_in_payload_mirror_customer_facts_at_top_level():
    """#2585: coord-portal's real wire shape (`src/bridge/events.ts`) nests
    every customer fact under `payload`, not `data`/`fields`. Unhandled, the
    whole envelope lands in the mirror under one `"payload"` key and every
    `approved_work._first_str` lookup (which reads `outcome`, `audience`, ...
    at the top level) comes back empty."""
    client = FakeClient(
        pages=[
            {
                "events": [
                    {
                        "id": "e1",
                        "submission_id": SUB,
                        "type": "submission.created",
                        "revision": 1,
                        "occurred_at": "2026-08-14T10:00:00Z",
                        "payload": {
                            "outcome": "a stick figure website",
                            "audience": "my kid",
                            "done_definition": "it loads",
                        },
                    }
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    sync_tick(client=client)

    record = portal_store.get_submission(SUB)
    assert record is not None
    assert record.customer["outcome"] == "a stick figure website"
    assert record.customer["audience"] == "my kid"
    assert record.customer["done_definition"] == "it loads"
    assert "payload" not in record.customer


def test_signoff_via_payload_envelope_does_not_wipe_the_intake_text():
    """#2585's live-proof scenario: `submission.created` arrives wrapped in
    `payload`, then a later `signoff.approved` event — also wrapped in
    `payload`, carrying only the verdict fields — must merge rather than
    clobber. Before the fix, both events flattened to a single top-level
    `"payload"` key, so `mirror_customer_facts`'s dict-merge protection never
    saw two distinct keys to merge and the sign-off silently replaced the
    whole record."""
    intake_page = {
        "events": [
            {
                "id": "e1",
                "submission_id": SUB,
                "type": "submission.created",
                "revision": 1,
                "payload": {
                    "outcome": "a stick figure website",
                    "audience": "my kid",
                    "done_definition": "it loads",
                },
            }
        ],
        "cursor": "c1",
        "has_more": False,
    }
    signoff_page = {
        "events": [
            {
                "id": "e2",
                "submission_id": SUB,
                "type": "signoff.approved",
                "revision": 2,
                "payload": {"verdict": "approved", "round": 1, "comment": None},
            }
        ],
        "cursor": "c2",
        "has_more": False,
    }

    sync_tick(client=FakeClient(pages=[intake_page]))
    sync_tick(client=FakeClient(pages=[signoff_page]))

    record = portal_store.get_submission(SUB)
    assert record is not None
    assert record.customer["outcome"] == "a stick figure website"
    assert record.customer["audience"] == "my kid"
    assert record.customer["done_definition"] == "it loads"
    assert record.customer["verdict"] == "approved"


def test_client_and_project_identity_survive_a_later_signoff_event():
    """#2586, coord-portal#146: `client_id`/`project_id` are set once on
    `submission.created` and must not be clobbered by a later `signoff.*`
    event that only carries verdict fields — the same merge-not-replace
    protection #2585 proved for the intake text, exercised here for the two
    identity fields `coord/approved_work.py` reads to render the "Approved
    work items" panel and resolve `portal.project_repos`."""
    created_page = {
        "events": [
            {
                "id": "e1",
                "submission_id": SUB,
                "type": "submission.created",
                "revision": 1,
                "payload": {
                    "outcome": "a stick figure website",
                    "client_id": "cli_9f2a",
                    "project_id": "proj_9f2a",
                },
            }
        ],
        "cursor": "c1",
        "has_more": False,
    }
    signoff_page = {
        "events": [
            {
                "id": "e2",
                "submission_id": SUB,
                "type": "signoff.approved",
                "revision": 2,
                "payload": {"verdict": "approved", "round": 1, "comment": None},
            }
        ],
        "cursor": "c2",
        "has_more": False,
    }

    sync_tick(client=FakeClient(pages=[created_page]))
    sync_tick(client=FakeClient(pages=[signoff_page]))

    record = portal_store.get_submission(SUB)
    assert record is not None
    assert record.customer["client_id"] == "cli_9f2a"
    assert record.customer["project_id"] == "proj_9f2a"
    assert record.customer["verdict"] == "approved"


def test_mirror_merges_rather_than_clobbers_across_events():
    sync_tick(
        client=FakeClient(
            pages=[
                {
                    "events": [{"id": "e1", "submission_id": SUB, "type": "created",
                                "data": {"intake": "the original ask"}}],
                    "cursor": "c1", "has_more": False,
                }
            ]
        )
    )
    sync_tick(
        client=FakeClient(
            pages=[
                {
                    "events": [{"id": "e2", "submission_id": SUB, "type": "signoff",
                                "data": {"verdict": "approved"}}],
                    "cursor": "c2", "has_more": False,
                }
            ]
        )
    )
    record = portal_store.get_submission(SUB)
    assert record.customer == {"intake": "the original ask", "verdict": "approved"}


def test_an_id_less_event_does_not_stop_the_rest_of_the_page_landing():
    """Both are stored — see the content-hash test below for why."""
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"submission_id": SUB, "type": "no-id-here"},
                    {"id": "e2", "submission_id": SUB, "type": "fine"},
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    result = sync_tick(client=client)
    assert result.pulled == 2
    assert "e2" in [e.event_id for e in portal_store.unhandled_events()]


# ── heartbeat and failure posture ───────────────────────────────────────────


def test_heartbeat_is_sent_even_when_pull_and_push_both_fail():
    enqueue_status(SUB, "in-progress")
    client = FakeClient(
        pull_error=PortalBridgeError("pull broke"),
        push_error=PortalBridgeError("push broke"),
    )
    result = sync_tick(client=client)

    assert client.heartbeats == 1
    assert result.heartbeat_ok is True
    assert len(result.errors) == 2
    assert portal_store.get_sync_state().last_heartbeat_at is not None


def test_sync_tick_never_raises_on_an_arbitrary_client_explosion():
    class Exploding:
        def pull(self, cursor=None, limit=None):
            raise ZeroDivisionError("not even a bridge error")

        def push(self, updates):
            raise ZeroDivisionError("nope")

        def heartbeat(self, at=None):
            raise ZeroDivisionError("nope")

    enqueue_status(SUB, "in-progress")  # so the push phase is actually reached
    result = sync_tick(client=Exploding())
    assert result.enabled is True
    assert result.heartbeat_ok is False
    # one per phase — none of the three may take the others down with it
    assert len(result.errors) == 3
    assert len(portal_store.pending_outbox()) == 1  # nothing was lost


def test_disabled_portal_config_sends_nothing():
    from coord.config import PortalConfig  # noqa: PLC0415

    class Cfg:
        portal = PortalConfig(enabled=False)

    result = sync_tick(Cfg())
    assert result.enabled is False
    assert result.summary() == "portal sync: disabled"


def test_errors_are_recorded_then_cleared_on_a_clean_pass():
    enqueue_status(SUB, "in-progress")
    sync_tick(client=FakeClient(push_error=PortalBridgeError("down")))
    assert "down" in portal_store.get_sync_state().last_error

    sync_tick(client=FakeClient())
    assert portal_store.get_sync_state().last_error == ""


def test_push_is_bounded_per_pass():
    for i in range(5):
        enqueue_status(f"sub-{i}", "in-progress")
    client = FakeClient()
    result = sync_tick(client=client, push_limit=2)
    assert result.applied == 2
    assert len(portal_store.pending_outbox()) == 3


def test_applied_rows_update_the_confirmed_record():
    enqueue_design_round(SUB, _design(round_no=3))
    enqueue_status(SUB, "awaiting-signoff")
    sync_tick(client=FakeClient())

    record = portal_store.get_submission(SUB)
    assert record.design_round == 3
    assert record.last_status == "awaiting-signoff"


# ── #2359: the preview-approval gate's ordering rule ────────────────────────


def test_enqueue_status_refuses_quality_check_with_no_preview():
    with pytest.raises(PortalSyncError) as exc:
        enqueue_status(SUB, "quality-check")
    assert "preview" in str(exc.value)
    assert portal_store.pending_outbox() == []


def test_enqueue_preview_refuses_an_empty_url():
    with pytest.raises(PortalSyncError):
        enqueue_preview(SUB, "")
    with pytest.raises(PortalSyncError):
        enqueue_preview(SUB, "   ")


def test_preview_is_pushed_before_the_status_that_announces_it():
    enqueue_preview(SUB, "https://pr-42.natal-chart.pages.dev")
    enqueue_status(SUB, "quality-check")
    client = FakeClient()

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["preview_url", "status"]
    assert result.applied == 2
    assert result.held == 0


def test_quality_check_announcement_is_held_while_its_preview_is_unconfirmed():
    enqueue_preview(SUB, "https://pr-42.natal-chart.pages.dev")
    enqueue_status(SUB, "quality-check")
    client = FakeClient(push_outcomes={"preview_url": "rejected"})

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["preview_url"]
    assert result.rejected == 1
    assert result.applied == 0

    # ...and it stays held on every subsequent tick, rather than leaking out.
    client2 = FakeClient()
    result2 = sync_tick(client=client2)
    assert client2.pushes == []
    assert result2.held == 1


def test_applied_preview_rows_update_the_confirmed_record():
    enqueue_preview(SUB, "https://pr-42.natal-chart.pages.dev")
    enqueue_status(SUB, "quality-check")
    sync_tick(client=FakeClient())

    record = portal_store.get_submission(SUB)
    assert record.preview_url == "https://pr-42.natal-chart.pages.dev"
    assert record.last_status == "quality-check"


def test_summary_reports_a_failed_heartbeat_loudly():
    result = sync_tick(client=FakeClient(heartbeat_ok=False))
    assert "heartbeat=FAILED" in result.summary()
    assert result.moved is False


# ── the daemon wiring ───────────────────────────────────────────────────────


def test_serve_tick_helper_delegates_to_sync_tick(monkeypatch):
    from coord import serve_app  # noqa: PLC0415

    seen = {}

    def _fake(config, **kw):
        seen["config"] = config
        return portal_sync.SyncResult(enabled=False)

    monkeypatch.setattr(portal_sync, "sync_tick", _fake)
    sentinel = object()
    result = serve_app._portal_sync_tick(sentinel)
    assert seen["config"] is sentinel
    assert result.enabled is False


def test_a_misconfigured_portal_block_does_not_read_as_merely_disabled():
    """Half a credential is not a credential — and must not print as 'disabled'."""
    from coord.config import PortalConfig  # noqa: PLC0415

    class Cfg:
        portal = PortalConfig(
            enabled=True, base_url="https://x", bridge_client_id="id"
        )  # no secret

    result = sync_tick(Cfg())
    assert result.enabled is False
    assert result.errors
    assert "NOT RUNNING" in result.summary()
    assert portal_store.get_sync_state().last_error


# ── review round 2: retry budget, malformed pages, nested revisions ─────────


def test_a_permanently_failing_row_is_retired_instead_of_freezing_the_queue():
    """A 4xx raises the same PortalBridgeError a timeout does — and repeats
    forever. Without a budget it would block every later row for this
    customer, re-issuing a known-bad request every tick."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "in-progress")  # a later, innocent row

    for _ in range(portal_sync.MAX_PUSH_ATTEMPTS):
        sync_tick(client=FakeClient(push_error=PortalBridgeError("400 malformed")))

    rows = portal_store.outbox_for_submission(SUB)
    assert rows[0].state == portal_store.STATE_REJECTED
    assert "gave up after" in rows[0].reason

    # The innocent row behind it is now free to go.
    client = FakeClient()
    result = sync_tick(client=client)
    assert client.pushed_kinds == ["status"]
    assert result.applied == 1


def test_retiring_a_prerequisite_still_never_releases_its_announcement():
    """Failing forward on the retry budget must not fail OPEN on the mail."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")

    for _ in range(portal_sync.MAX_PUSH_ATTEMPTS + 2):
        sync_tick(client=FakeClient(push_error=PortalBridgeError("400 malformed")))

    rows = portal_store.outbox_for_submission(SUB)
    assert rows[0].state == portal_store.STATE_REJECTED
    assert rows[1].state == portal_store.STATE_PENDING  # held, never sent

    client = FakeClient()
    sync_tick(client=client)
    assert client.pushes == []


def test_an_event_with_no_id_is_stored_not_dropped_and_still_dedupes():
    """The cursor advances past it either way — dropping it would lose it."""
    page = {
        "events": [{"submission_id": SUB, "type": "submission.created"}],
        "cursor": "c1",
        "has_more": False,
    }
    first = sync_tick(client=FakeClient(pages=[dict(page)]))
    assert first.pulled == 1
    stored = portal_store.unhandled_events()
    assert len(stored) == 1
    assert stored[0].event_id.startswith("sha256:")

    # A replay of the same page derives the same content-hash id.
    portal_store.set_pull_cursor(None)
    second = sync_tick(client=FakeClient(pages=[dict(page)]))
    assert second.pulled == 0
    assert len(portal_store.unhandled_events()) == 1


def test_an_integer_zero_event_id_is_not_treated_as_missing():
    client = FakeClient(
        pages=[
            {"events": [{"id": 0, "submission_id": SUB, "type": "x"}],
             "cursor": "c1", "has_more": False}
        ]
    )
    sync_tick(client=client)
    assert [e.event_id for e in portal_store.unhandled_events()] == ["0"]


def test_a_malformed_page_does_not_advance_the_cursor():
    client = FakeClient(
        pages=[{"events": "not-a-list", "cursor": "c1", "has_more": False}]
    )
    result = sync_tick(client=client)
    assert result.errors
    assert portal_store.get_sync_state().pull_cursor is None


def test_a_nested_revision_seeds_the_allocator_too():
    """The mirror reads the nested shape, so the seed must as well."""
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"id": "e1", "submission_id": SUB, "type": "created",
                     "data": {"revision": 4, "intake": "x"}}
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    sync_tick(client=client)
    assert enqueue_status(SUB, "in-design").revision == 5


def test_sync_tick_returns_even_when_recording_pass_state_fails(monkeypatch):
    """The bookkeeping write must not be the thing that breaks 'never raises'."""
    def _boom(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(portal_store, "clear_error", _boom)
    monkeypatch.setattr(portal_store, "note_error", _boom)
    result = sync_tick(client=FakeClient())
    assert result.enabled is True
    assert result.heartbeat_ok is True


def test_requeue_revives_a_retired_row_with_a_fresh_revision():
    enqueue_design_round(SUB, _design())
    for _ in range(portal_sync.MAX_PUSH_ATTEMPTS):
        sync_tick(client=FakeClient(push_error=PortalBridgeError("400 malformed")))
    retired = portal_store.outbox_for_submission(SUB)[0]
    assert retired.state == portal_store.STATE_REJECTED

    revived = portal_store.requeue(SUB, retired.seq)
    assert revived.state == portal_store.STATE_PENDING
    assert revived.attempts == 0
    assert revived.revision > retired.revision

    client = FakeClient()
    assert sync_tick(client=client).applied == 1


def test_requeue_of_an_unknown_row_is_a_clean_none():
    assert portal_store.requeue("nope", 1) is None


# ── consuming portal verdicts (#2509, PDR-4) ────────────────────────────────


class FakeRepoCfg:
    def __init__(self, github: str = "acme/portal-repo") -> None:
        self.github = github


class FakeConfig:
    """Just enough of `coord.config.Config` for `_consume_verdicts`: a
    `.repo(name)` lookup. Real dispatch is monkeypatched out in every test
    below, so nothing else on Config is ever touched."""

    def __init__(self, repos: dict | None = None) -> None:
        self._repos = repos or {}

    def repo(self, name):
        return self._repos.get(name)


def _changes_requested_page(comments: str | None = "make it blue") -> dict:
    data = {"verdict": "changes_requested"}
    if comments is not None:
        data["comments"] = comments
    return {
        "events": [
            {"id": "e1", "submission_id": SUB, "type": "signoff.changes_requested", "data": data}
        ],
        "cursor": "c1",
        "has_more": False,
    }


def test_changes_requested_dispatches_amend_and_marks_the_event_consumed(monkeypatch):
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id=SUB
    )
    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", lambda repo_cfg, ms: 42)
    calls = []

    def fake_dispatch(repo_name, tracking_issue_number, config, *, amend_briefing=None, **_):
        calls.append((repo_name, tracking_issue_number, amend_briefing))
        return ("assignment-1", "machine-1")

    monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", fake_dispatch)

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 1
    assert calls == [("acme-portal", 42, "make it blue")]
    assert portal_store.unhandled_events() == []


def test_approved_verdict_is_left_unconsumed_not_auto_decided(monkeypatch):
    """#2509's open policy question: an `approved` verdict must not silently
    auto-record Gate A here — it stays unhandled instead of being acted on."""
    dispatched = []
    monkeypatch.setattr(
        "coord.mock_author.dispatch_acceptance_mock",
        lambda *a, **kw: dispatched.append((a, kw)),
    )
    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"id": "e1", "submission_id": SUB, "type": "signoff.approved"}
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 0
    assert dispatched == []
    assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]


def test_changes_requested_with_no_link_recorded_stays_unhandled_and_errors():
    """No `coord portal link` for this submission yet — nothing to dispatch
    to, and the client's feedback must not be dropped."""
    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 0
    assert any("no milestone is linked" in e for e in result.errors)
    assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]


def test_a_dispatch_failure_leaves_the_event_to_retry_next_tick(monkeypatch):
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id=SUB
    )
    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", lambda repo_cfg, ms: 42)

    def boom(*_a, **_kw):
        raise RuntimeError("Gate A already in flight")

    monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", boom)

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 0
    assert any("Gate A already in flight" in e for e in result.errors)
    assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]


def test_a_missing_comment_falls_back_to_a_placeholder_amend_text(monkeypatch):
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id=SUB
    )
    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", lambda repo_cfg, ms: 42)
    calls = []
    monkeypatch.setattr(
        "coord.mock_author.dispatch_acceptance_mock",
        lambda repo, issue, cfg, **kw: calls.append(kw.get("amend_briefing")),
    )

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page(comments=None)])
    sync_tick(config=config, client=client)

    assert len(calls) == 1
    assert SUB in calls[0]


def test_with_no_config_the_verdict_phase_is_a_no_op_not_a_crash():
    """`sync_tick(client=...)` with no config is the documented test/CLI
    bypass — verdict consumption must not explode with nowhere to dispatch."""
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(client=client)

    assert result.verdicts_consumed == 0
    assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]


def test_a_large_non_actionable_backlog_does_not_starve_a_later_changes_requested_event(
    monkeypatch,
):
    """Review finding (#2509): `_consume_verdicts` used to scan
    `unhandled_events()` — oldest `handled_at IS NULL` rows first — but only
    ever stamps `handled_at` on a `changes_requested` event it dispatches.
    Every other kind (here: 150 `"created"` events) piles up unmarked FOREVER,
    always sorted ahead of anything newer, so once that pile exceeds one page
    a real sign-off behind it is never returned again by any future tick.

    Reproduced directly against the old code: 150 noise events + 1
    `changes_requested` event, 5 ticks of `_consume_verdicts(limit=100)`,
    `consumed == 0` every time and the targeted event never appeared in
    `unhandled_events()`'s result at all.

    The fix is a private watermark independent of `handled_at`
    (`portal_store.get/set_verdict_watermark`,
    `events_after_verdict_watermark`) that advances past every event this
    consumer has looked at, acted on or not — so paging through the pile,
    even a handful of events at a time, eventually reaches the real one.
    """
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id=SUB
    )
    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", lambda repo_cfg, ms: 42)
    calls = []
    monkeypatch.setattr(
        "coord.mock_author.dispatch_acceptance_mock",
        lambda repo, issue, cfg, **kw: calls.append(kw.get("amend_briefing")),
    )

    noise = [
        {"id": f"noise-{i}", "submission_id": SUB, "type": "created"} for i in range(150)
    ]
    portal_store.record_events(noise)
    portal_store.record_events(
        [
            {
                "id": "signoff-1",
                "submission_id": SUB,
                "type": "signoff.changes_requested",
                "data": {"verdict": "changes_requested", "comments": "fix it"},
            }
        ]
    )

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    # One narrow page (50) per simulated tick, on purpose — proves it's the
    # watermark doing the work, not just a limit generous enough to cover the
    # whole backlog in a single call.
    total_consumed = 0
    for _tick in range(5):
        consumed, errors = portal_sync._consume_verdicts(config, limit=50, pages=1)
        total_consumed += consumed
        assert errors == []

    assert total_consumed == 1
    assert calls == ["fix it"]


def test_a_dispatch_failure_freezes_the_watermark_but_not_later_independent_events(
    monkeypatch,
):
    """A still-broken event must not be silently skipped once the watermark
    passes it (that would drop the client's feedback) — but a LATER,
    unrelated `changes_requested` event in the same page must still get its
    own dispatch attempt (per-event isolation, mirroring `_push`)."""
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id="sub-bad"
    )
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=6, submission_id="sub-good"
    )
    monkeypatch.setattr(
        portal_sync,
        "_resolve_tracking_issue",
        lambda repo_cfg, ms: 42 if ms == 5 else 43,
    )
    dispatched = []

    def fake_dispatch(repo_name, tracking_issue_number, config, *, amend_briefing=None, **_):
        if tracking_issue_number == 42:
            raise RuntimeError("Gate A already in flight")
        dispatched.append((repo_name, tracking_issue_number, amend_briefing))

    monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", fake_dispatch)

    portal_store.record_events(
        [
            {
                "id": "bad-1",
                "submission_id": "sub-bad",
                "type": "signoff.changes_requested",
                "data": {"verdict": "changes_requested", "comments": "broken"},
            },
            {
                "id": "good-1",
                "submission_id": "sub-good",
                "type": "signoff.changes_requested",
                "data": {"verdict": "changes_requested", "comments": "fine"},
            },
        ]
    )

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    consumed, errors = portal_sync._consume_verdicts(config)

    assert consumed == 1
    assert dispatched == [("acme-portal", 43, "fine")]
    assert any("Gate A already in flight" in e for e in errors)
    # The failing event is still visible for retry; the successful sibling
    # was still marked handled despite arriving after it in the same page.
    unhandled_ids = {e.event_id for e in portal_store.unhandled_events()}
    assert unhandled_ids == {"bad-1"}

    # Next tick: watermark is frozen before "bad-1", so "good-1" is walked
    # again (harmless — already `handled_at`, not re-dispatched) and "bad-1"
    # is retried rather than skipped.
    dispatched.clear()
    consumed_again, errors_again = portal_sync._consume_verdicts(config)
    assert consumed_again == 0
    assert dispatched == []  # "good-1" was not re-dispatched
    assert any("Gate A already in flight" in e for e in errors_again)


def test_changes_requested_verdict_with_a_space_separator_is_recognized(monkeypatch):
    """Non-blocking review finding: the portal's own event contract for a
    sign-off is not fully pinned down — a verdict spelled with a space
    (`"changes requested"`) rather than a hyphen must still be recognized,
    not silently left unconsumed."""
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id=SUB
    )
    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", lambda repo_cfg, ms: 42)
    calls = []
    monkeypatch.setattr(
        "coord.mock_author.dispatch_acceptance_mock",
        lambda repo, issue, cfg, **kw: calls.append(kw.get("amend_briefing")),
    )
    portal_store.record_events(
        [
            {
                "id": "e1",
                "submission_id": SUB,
                "type": "signoff",
                "data": {"verdict": "Changes Requested", "comments": "space separated"},
            }
        ]
    )

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    consumed, errors = portal_sync._consume_verdicts(config)

    assert consumed == 1
    assert errors == []
    assert calls == ["space separated"]


class TestSignoffVerdict:
    def _event(self, kind: str, payload: dict | None = None):
        return portal_store.PortalEvent(
            event_id="e1",
            submission_id=SUB,
            kind=kind,
            occurred_at="",
            payload=payload or {},
            received_at=0.0,
        )

    def test_verdict_suffix_on_type(self):
        assert (
            portal_sync._signoff_verdict(self._event("signoff.changes_requested"))
            == "changes_requested"
        )

    def test_verdict_nested_in_data(self):
        event = self._event("signoff", {"data": {"verdict": "approved"}})
        assert portal_sync._signoff_verdict(event) == "approved"

    def test_non_signoff_kind_is_not_a_verdict(self):
        assert portal_sync._signoff_verdict(self._event("created")) is None

    def test_comment_read_from_nested_data(self):
        event = self._event(
            "signoff.changes_requested", {"data": {"comments": "make it bigger"}}
        )
        assert portal_sync._signoff_comment(event) == "make it bigger"

    def test_comment_defaults_to_empty_string(self):
        event = self._event("signoff.changes_requested")
        assert portal_sync._signoff_comment(event) == ""


# ── #2513 (PDR-5): the shared upload+enqueue tail ───────────────────────────
#
# `push_design_round_bundle` is the sequence extracted out of
# `coord.merge_queue._maybe_push_design_round` so PDR-3's merge-triggered
# auto-push and PDR-5's on-demand `coord portal publish-mocks` both call the
# SAME upload → build → enqueue steps rather than duplicating them. These
# tests exercise the function directly, independent of either caller.


class _UploadClient:
    """A `PortalBridgeClient` stand-in that only needs `upload_bundle` —
    `push_design_round_bundle` never calls `push`/`pull`/`heartbeat`."""

    def __init__(self, bundle_key: str = "bundles/sub-001/r1.tar", error=None):
        self.bundle_key = bundle_key
        self.error = error
        self.uploads: list[tuple[str, dict]] = []

    def upload_bundle(self, submission_id: str, files: dict) -> str:
        if self.error:
            raise self.error
        self.uploads.append((submission_id, dict(files)))
        return self.bundle_key


def test_push_design_round_bundle_uploads_then_enqueues():
    client = _UploadClient(bundle_key="bundles/sub-001/r7.tar")
    files = {"contract.md": "# contract", "mocks/index.html": "<html></html>"}

    bundle_key, row = portal_sync.push_design_round_bundle(
        client,
        SUB,
        files,
        milestone_title="ms title",
        tracking_issue_title="Q3 push",
        tracking_issue_body="Ship it.",
    )

    assert bundle_key == "bundles/sub-001/r7.tar"
    assert client.uploads == [(SUB, files)]
    assert row.kind == portal_sync.KIND_DESIGN_ROUND
    stored = portal_store.outbox_for_submission(SUB)
    assert len(stored) == 1
    assert stored[0].fields["design_round"]["bundle_key"] == "bundles/sub-001/r7.tar"
    assert "Ship it." in stored[0].fields["design_round"]["outcome_definition"]


def test_push_design_round_bundle_propagates_an_upload_failure():
    client = _UploadClient(error=PortalBridgeError("401 unauthorized"))
    with pytest.raises(PortalBridgeError):
        portal_sync.push_design_round_bundle(
            client,
            SUB,
            {"contract.md": "# contract"},
            milestone_title="ms title",
            tracking_issue_title="Q3 push",
            tracking_issue_body="Ship it.",
        )
    # Nothing was queued — the enqueue step never ran.
    assert portal_store.outbox_for_submission(SUB) == []


def test_push_design_round_bundle_round_number_flows_through():
    client = _UploadClient()
    _, row = portal_sync.push_design_round_bundle(
        client,
        SUB,
        {"contract.md": "# contract"},
        milestone_title="ms title",
        tracking_issue_title="Q3 push",
        tracking_issue_body="Ship it.",
        round_number=2,
    )
    assert row.fields["design_round"]["round"] == 2


# ── automatic status fold (#2588) ────────────────────────────────────────────
#
# Four real submissions shipped and closed in production while the portal
# kept showing "describing"/"planned" — `enqueue_status` had exactly one
# caller (a human) before this. These tests cover the fold that gives it an
# automatic one: the pure planned/in-progress/shipped derivation, the
# unlinked/no-issues no-ops, the churn guard, and a GitHub read failure
# surfacing without raising.


def _issue(number: int, state: str) -> dict:
    return {"number": number, "state": state}


class TestFoldSubmissionStatus:
    """Pure: no I/O, no portal_store, no config."""

    def test_all_closed_is_shipped(self):
        issues = [_issue(1, "CLOSED"), _issue(2, "closed")]
        assert portal_sync.fold_submission_status(issues, frozenset()) == "shipped"

    def test_none_started_is_planned(self):
        issues = [_issue(1, "OPEN"), _issue(2, "OPEN")]
        assert portal_sync.fold_submission_status(issues, frozenset()) == "planned"

    def test_one_started_not_all_closed_is_in_progress(self):
        issues = [_issue(1, "OPEN"), _issue(2, "CLOSED")]
        assert (
            portal_sync.fold_submission_status(issues, frozenset({1}))
            == "in-progress"
        )

    def test_all_closed_wins_over_started(self):
        """The last issue closing must always read shipped, never a stale
        in-progress left over from an assignment that started before the
        submission finished."""
        issues = [_issue(1, "CLOSED"), _issue(2, "CLOSED")]
        assert (
            portal_sync.fold_submission_status(issues, frozenset({1, 2}))
            == "shipped"
        )


class TestStartedIssueNumbers:
    def test_none_board_is_the_empty_set(self):
        assert portal_sync._started_issue_numbers(None, "acme-portal") == frozenset()

    def test_dispatched_work_assignment_counts_as_started(self):
        from coord.models import Assignment, Board

        a = Assignment(
            machine_name="m1", repo_name="acme-portal", issue_number=7,
            issue_title="t", type="work", dispatched_at=123.0,
        )
        board = Board(active=[a], completed=[])
        assert portal_sync._started_issue_numbers(board, "acme-portal") == {7}

    def test_undispatched_assignment_does_not_count(self):
        from coord.models import Assignment, Board

        a = Assignment(
            machine_name="m1", repo_name="acme-portal", issue_number=7,
            issue_title="t", type="work", dispatched_at=None,
        )
        board = Board(active=[a], completed=[])
        assert portal_sync._started_issue_numbers(board, "acme-portal") == frozenset()

    def test_other_repo_does_not_count(self):
        from coord.models import Assignment, Board

        a = Assignment(
            machine_name="m1", repo_name="other-repo", issue_number=7,
            issue_title="t", type="work", dispatched_at=123.0,
        )
        board = Board(active=[a], completed=[])
        assert portal_sync._started_issue_numbers(board, "acme-portal") == frozenset()

    def test_non_work_assignment_does_not_count(self):
        """A review/smoke/plan assignment against the issue is not "the work
        has started" — only `type="work"` is."""
        from coord.models import Assignment, Board

        a = Assignment(
            machine_name="m1", repo_name="acme-portal", issue_number=7,
            issue_title="t", type="review", dispatched_at=123.0,
        )
        board = Board(active=[a], completed=[])
        assert portal_sync._started_issue_numbers(board, "acme-portal") == frozenset()

    def test_completed_assignments_also_count(self):
        from coord.models import Assignment, Board

        a = Assignment(
            machine_name="m1", repo_name="acme-portal", issue_number=7,
            issue_title="t", type="work", dispatched_at=123.0, status="done",
        )
        board = Board(active=[], completed=[a])
        assert portal_sync._started_issue_numbers(board, "acme-portal") == {7}


class TestFoldStatusForMilestone:
    """`fold_status_for_milestone` never raises — every outcome, including
    success, comes back as a `StatusFoldResult` with a populated reason."""

    def test_unlinked_milestone_is_a_visible_no_op(self):
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        assert result.submission_id is None
        assert result.status is None
        assert result.row is None
        assert result.failed is False
        assert "no portal link recorded" in result.reason

    def test_repo_not_in_config_is_a_visible_no_op(self):
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        config = FakeConfig({})  # "acme-portal" not registered

        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        assert result.submission_id == SUB
        assert result.row is None
        assert result.failed is False
        assert "coordinator.yml" in result.reason

    def test_github_read_failure_surfaces_without_raising(self, monkeypatch):
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})

        def boom(repo_cfg, milestone_number):
            raise RuntimeError("gh api rate limited")

        monkeypatch.setattr(portal_sync, "_milestone_issues", boom)

        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        assert result.submission_id == SUB
        assert result.row is None
        assert result.failed is True
        assert "gh api rate limited" in result.reason

    def test_no_issues_yet_is_a_visible_no_op(self, monkeypatch):
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        monkeypatch.setattr(portal_sync, "_milestone_issues", lambda *a: [])

        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        assert result.row is None
        assert result.failed is False
        assert "no issues yet" in result.reason

    def test_five_issues_one_shipped_fold(self, monkeypatch):
        """The scenario #2588 names explicitly: a submission decomposed into
        several issues only reads `shipped` once every one of them has
        closed — not on the first, not on the fourth."""
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        four_closed_one_open = [
            _issue(1, "CLOSED"), _issue(2, "CLOSED"), _issue(3, "CLOSED"),
            _issue(4, "CLOSED"), _issue(5, "OPEN"),
        ]
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: four_closed_one_open
        )

        result = portal_sync.fold_status_for_milestone(
            config, "acme-portal", 5,
            board=_board_with_started("acme-portal", {5}),
        )
        assert result.status == "in-progress"
        assert result.row is not None
        assert result.row.fields["status"] == "in-progress"

        # The fifth and final issue closes.
        all_closed = [_issue(n, "CLOSED") for n in range(1, 6)]
        monkeypatch.setattr(portal_sync, "_milestone_issues", lambda *a: all_closed)

        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)
        assert result.status == "shipped"
        assert result.row is not None
        assert result.row.fields["status"] == "shipped"

    def test_unchanged_status_does_not_re_notify(self, monkeypatch):
        """Churn must not become mail: folding to the SAME status twice in a
        row must only enqueue once."""
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        monkeypatch.setattr(
            portal_sync, "_milestone_issues",
            lambda *a: [_issue(1, "OPEN"), _issue(2, "OPEN")],
        )

        first = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)
        assert first.status == "planned"
        assert first.row is not None

        second = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)
        assert second.status == "planned"
        assert second.row is None
        assert "unchanged" in second.reason

        rows = [
            r for r in portal_store.outbox_for_submission(SUB)
            if r.kind == portal_sync.KIND_STATUS
        ]
        assert len(rows) == 1


def _board_with_started(repo_name: str, issue_numbers: set) -> "object":
    from coord.models import Assignment, Board

    return Board(
        active=[
            Assignment(
                machine_name="m1", repo_name=repo_name, issue_number=n,
                issue_title="t", type="work", dispatched_at=1.0,
            )
            for n in issue_numbers
        ],
        completed=[],
    )


class TestSyncSubmissionStatuses:
    def test_no_config_is_a_no_op(self):
        assert portal_sync.sync_submission_statuses(None) == []

    def test_folds_every_linked_milestone_independently(self, monkeypatch):
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id="sub-a"
        )
        portal_store.link_milestone(
            repo_name="other-repo", milestone_number=9, submission_id="sub-b"
        )
        config = FakeConfig(
            {"acme-portal": FakeRepoCfg(), "other-repo": FakeRepoCfg("acme/other")}
        )

        def fake_issues(repo_cfg, milestone_number):
            if repo_cfg.github == "acme/other":
                raise RuntimeError("network blip")
            return [_issue(1, "CLOSED")]

        monkeypatch.setattr(portal_sync, "_milestone_issues", fake_issues)

        results = portal_sync.sync_submission_statuses(config)

        by_submission = {r.submission_id: r for r in results}
        assert by_submission["sub-a"].status == "shipped"
        assert by_submission["sub-a"].row is not None
        # The other link's GitHub failure doesn't stop this one from folding.
        assert by_submission["sub-b"].failed is True


class TestSyncTickStatusFold:
    """The end-to-end wiring through `sync_tick`: a folded, changed status
    is queued AND drained within the same tick (#2588's ordering — status
    fold runs before push)."""

    def test_folded_status_is_pushed_within_the_same_tick(self, monkeypatch):
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")],
        )
        client = FakeClient()

        result = sync_tick(config=config, client=client)

        assert result.status_queued == 1
        assert result.applied == 1
        assert client.pushed_kinds == ["status"]

    def test_a_portal_outage_does_not_prevent_the_status_fold(self, monkeypatch):
        """The fold enqueues locally regardless of whether the drain that
        same tick can reach the portal — a portal outage must never block
        this."""
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")],
        )
        client = FakeClient(push_error=PortalBridgeError("portal unreachable"))

        result = sync_tick(config=config, client=client)

        assert result.status_queued == 1
        rows = [
            r for r in portal_store.outbox_for_submission(SUB)
            if r.kind == portal_sync.KIND_STATUS
        ]
        assert len(rows) == 1
        assert rows[0].state == portal_store.STATE_PENDING

    def test_no_config_skips_the_fold_but_not_the_rest_of_the_tick(self):
        client = FakeClient()
        result = sync_tick(client=client)
        assert result.status_queued == 0
        assert result.heartbeat_ok is True
