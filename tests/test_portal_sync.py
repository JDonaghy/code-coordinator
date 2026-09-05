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
    enqueue_relayed_answer,
    enqueue_status,
    ordering_block_reason,
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


def _approval(**kinds: bool):
    """A :class:`coord.config.PortalApprovalConfig` merged over the defaults."""
    from coord.config import DEFAULT_PORTAL_APPROVAL, PortalApprovalConfig

    return PortalApprovalConfig(kinds={**DEFAULT_PORTAL_APPROVAL, **kinds})


class _Cfg:
    """The one attribute :func:`coord.portal_sync._approval_config` reads."""

    def __init__(self, approval):
        self.portal = type("_Portal", (), {"approval": approval})()


def _ungated():
    return _Cfg(_approval(design_round=False, question=False))


@pytest.fixture(autouse=True)
def _ungated_when_no_config_is_passed(monkeypatch):
    """#2903: keep this module's pre-existing tests about what they test.

    Almost everything here is about the DRAIN and the #835 ordering guard,
    and was written when `enqueue_design_round`/`enqueue_question` went
    straight to `pending`. The draft gate now holds both by default, which
    would turn every one of those into a test of the gate instead.

    So: a call that names no *config* reads as ungated here, and a call that
    passes one gets the REAL policy read. The gate's own tests below
    (`TestDraftGate`) therefore exercise the genuine
    :func:`coord.portal_sync._approval_config` path with an explicit config,
    and the default-when-nothing-is-configured behaviour is pinned in
    `tests/test_portal_config.py` and `tests/test_cli_portal.py` (which run
    the real `config.load()`), not stubbed away.
    """
    real = portal_sync._approval_config

    def _patched(config=None):
        if config is not None:
            return real(config)
        return _approval(design_round=False, question=False)

    monkeypatch.setattr(portal_sync, "_approval_config", _patched)


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
    # enqueue_question (#2901) queues its own needs-input status right
    # behind the question — no separate enqueue_status call needed here.
    enqueue_question(SUB, "which colour?")
    sync_tick(client=FakeClient())  # round 1 lands cleanly

    enqueue_question(SUB, "which font?")
    # The second question fails to send; its announcement must not overtake it.
    client = FakeClient(push_error=PortalBridgeError("boom"))
    sync_tick(client=client)
    assert client.pushes == []

    client2 = FakeClient()
    sync_tick(client=client2)
    assert client2.pushed_kinds == ["question", "status"]


def test_enqueue_question_queues_its_own_announcement():
    """#2901: a question pushed without a status row behind it sends no
    email (coord-portal's own rule — a push only mails the customer when it
    actually names ``status``). `enqueue_question` must queue that status
    itself, in the same call, so the announcement cannot be forgotten."""
    question_row, status_row = enqueue_question(SUB, "which colour?")

    assert question_row.kind == portal_sync.KIND_QUESTION
    assert status_row.kind == portal_sync.KIND_STATUS
    assert status_row.announces == "needs-input"
    assert status_row.requires_kind == portal_sync.KIND_QUESTION
    # The announcement is seq N+1 right behind its question — the ordering
    # guard (`ANNOUNCING_STATUSES`/`ordering_block_reason`) keys off exactly
    # this adjacency.
    assert status_row.seq == question_row.seq + 1


def test_enqueue_question_reproduces_sub_1ea1d3_second_question_also_announced():
    """Regression for #2901 / SUB-1EA1D3: question (applied) -> status
    (applied) -> a second question with NO status row behind it — the real
    outbox shape that shipped with no email. With the fold, the second
    `enqueue_question` call must also queue its own status, so the drain
    sends question -> status -> question -> status in order, never a bare
    trailing question."""
    enqueue_question(SUB, "Two things to help us scope...")
    sync_tick(client=FakeClient())  # first question + its announcement land

    enqueue_question(SUB, "Thanks -- that covers who...")
    client = FakeClient()
    sync_tick(client=client)

    assert client.pushed_kinds == ["question", "status"]
    rows = portal_store.outbox_for_submission(SUB)
    assert [r.kind for r in rows] == ["question", "status", "question", "status"]
    assert [r.state for r in rows] == [portal_store.STATE_APPLIED] * 4
    # The second question's announcement never sent ahead of its own
    # question being confirmed applied.
    assert ordering_block_reason(rows[3]) is None


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


def test_a_raised_heartbeat_always_lands_in_errors_too():
    """#2862: a heartbeat that *raises* sets ``heartbeat_ok=False`` AND appends
    to ``errors`` — the two are never independent.

    This invariant is why the daemon's old Step 3d reporting could not work.
    ``coord/serve_app.py``'s tick loop read it as::

        if portal_result.moved or portal_result.errors:
            log.info(...)
        elif not portal_result.heartbeat_ok:
            log.warning(...)      # <- written for a failed heartbeat

    and since every real heartbeat failure populates ``errors``, the ``if``
    always won and the ``elif`` was dead code for the one case it existed to
    catch.  If a future refactor stops recording the heartbeat exception as an
    error, that branch structure becomes viable again — but the daemon-side
    fix (report on ``errors or not heartbeat_ok``) stays correct either way.
    """
    result = sync_tick(client=FakeClient(heartbeat_error=PortalBridgeError("401")))
    assert result.heartbeat_ok is False
    assert result.errors, (
        "a raised heartbeat produced no error entry — the daemon's Step 3d "
        "reporting branch (see the docstring) assumes these move together"
    )
    assert any("heartbeat" in e for e in result.errors)
    # And the pass that could not heartbeat left the portal's freshness marker
    # untouched — which is exactly the frozen `last_heartbeat_at` column #2862
    # was diagnosed from.
    assert portal_store.get_sync_state().last_heartbeat_at is None


def test_a_quiet_healthy_pass_is_still_reportable():
    """The steady state the journal must show (#2862): nothing moved, no
    errors, heartbeat landed.  Before the fix this pass logged at INFO on a
    daemon where INFO was discarded, so a working bridge and a dead one were
    byte-identical from the outside."""
    result = sync_tick(client=FakeClient())
    assert result.enabled is True
    assert result.moved is False
    assert not result.errors
    assert result.heartbeat_ok is True
    assert "heartbeat=ok" in result.summary()
    assert portal_store.get_sync_state().last_heartbeat_at is not None


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


def test_changes_requested_for_an_issue_scoped_link_skips_tracking_lookup(monkeypatch):
    """#2665: a milestone-less issue link IS its own tracking issue — no
    ``_resolve_tracking_issue`` reverse lookup needed (and none attempted:
    monkeypatched to blow up if called, to prove it)."""
    portal_store.link_issue(
        repo_name="acme-portal", issue_number=77, submission_id=SUB
    )

    def boom(*_a, **_kw):
        raise AssertionError("_resolve_tracking_issue must not be called for an issue link")

    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", boom)
    calls = []

    def fake_dispatch(repo_name, tracking_issue_number, config, *, amend_briefing=None, **_):
        calls.append((repo_name, tracking_issue_number, amend_briefing))
        return ("assignment-1", "machine-1")

    monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", fake_dispatch)

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 1
    assert calls == [("acme-portal", 77, "make it blue")]
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


class TestSingleIssueAsList:
    """#2665: `_single_issue_as_list` is `_milestone_issues`'s one-issue
    counterpart — wraps a single GitHub read in the `[{"number", "state"}]`
    shape `fold_submission_status` expects."""

    def test_wraps_the_issue_in_a_one_member_list(self, monkeypatch):
        monkeypatch.setattr(
            "coord.github_ops.get_issue",
            lambda repo, number: {"number": number, "state": "OPEN", "title": "t"},
        )
        result = portal_sync._single_issue_as_list(FakeRepoCfg(), 77)
        assert result == [{"number": 77, "state": "OPEN", "title": "t"}]

    def test_raises_when_the_issue_cannot_be_read(self, monkeypatch):
        monkeypatch.setattr("coord.github_ops.get_issue", lambda repo, number: {})
        with pytest.raises(RuntimeError, match="could not be read"):
            portal_sync._single_issue_as_list(FakeRepoCfg(), 77)


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

    def test_already_shipped_collapses_new_open_work_to_post_shipped(self):
        """#3106: a newly-linked, still-open post-release issue must not
        fold to `planned`/`in-progress` once the submission has shipped —
        those buckets are pre-release granularity only."""
        issues = [_issue(1, "CLOSED"), _issue(2, "OPEN")]
        assert (
            portal_sync.fold_submission_status(
                issues, frozenset(), already_shipped=True,
            )
            == "post-shipped"
        )

    def test_already_shipped_started_work_also_reads_post_shipped(self):
        """Same as above when the new post-release issue has actually
        started — `already_shipped` collapses both pre-release buckets
        (`planned` and `in-progress`), not just `planned`."""
        issues = [_issue(1, "CLOSED"), _issue(2, "OPEN")]
        assert (
            portal_sync.fold_submission_status(
                issues, frozenset({2}), already_shipped=True,
            )
            == "post-shipped"
        )

    def test_already_shipped_all_closed_still_reads_plain_shipped(self):
        """All-closed wins over `already_shipped`, deliberately: a
        submission with nothing new since it shipped must keep folding to
        the SAME `shipped` value on every tick (what lets the #2588 churn
        guard recognise "nothing changed" and stay quiet), not drift to
        `post-shipped` on its own with no new issue behind it."""
        issues = [_issue(1, "CLOSED"), _issue(2, "CLOSED")]
        assert (
            portal_sync.fold_submission_status(
                issues, frozenset({1, 2}), already_shipped=True,
            )
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


class TestFoldStatusForIssue:
    """#2665: the one-off-issue counterpart of `TestFoldStatusForMilestone` —
    same never-raises contract, same churn guard, just folding a single
    milestone-less issue instead of a milestone's members."""

    def test_unlinked_issue_is_a_visible_no_op(self):
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        result = portal_sync.fold_status_for_issue(config, "acme-portal", 77)

        assert result.submission_id is None
        assert result.status is None
        assert result.row is None
        assert result.failed is False
        assert "no portal link recorded" in result.reason
        assert "--issue" in result.reason

    def test_repo_not_in_config_is_a_visible_no_op(self):
        portal_store.link_issue(
            repo_name="acme-portal", issue_number=77, submission_id=SUB
        )
        config = FakeConfig({})  # "acme-portal" not registered

        result = portal_sync.fold_status_for_issue(config, "acme-portal", 77)

        assert result.submission_id == SUB
        assert result.row is None
        assert result.failed is False
        assert "coordinator.yml" in result.reason

    def test_github_read_failure_surfaces_without_raising(self, monkeypatch):
        portal_store.link_issue(
            repo_name="acme-portal", issue_number=77, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})

        def boom(repo_cfg, issue_number):
            raise RuntimeError("gh api rate limited")

        monkeypatch.setattr(portal_sync, "_single_issue_as_list", boom)

        result = portal_sync.fold_status_for_issue(config, "acme-portal", 77)

        assert result.submission_id == SUB
        assert result.row is None
        assert result.failed is True
        assert "gh api rate limited" in result.reason

    def test_open_issue_folds_planned_then_closed_folds_shipped(self, monkeypatch):
        portal_store.link_issue(
            repo_name="acme-portal", issue_number=77, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        monkeypatch.setattr(
            portal_sync, "_single_issue_as_list", lambda *a: [_issue(77, "OPEN")]
        )

        result = portal_sync.fold_status_for_issue(config, "acme-portal", 77)
        assert result.status == "planned"
        assert result.row is not None
        assert result.row.fields["status"] == "planned"

        monkeypatch.setattr(
            portal_sync, "_single_issue_as_list", lambda *a: [_issue(77, "CLOSED")]
        )
        result = portal_sync.fold_status_for_issue(config, "acme-portal", 77)
        assert result.status == "shipped"
        assert result.row is not None
        assert result.row.fields["status"] == "shipped"

    def test_unchanged_status_does_not_re_notify(self, monkeypatch):
        portal_store.link_issue(
            repo_name="acme-portal", issue_number=77, submission_id=SUB
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        monkeypatch.setattr(
            portal_sync, "_single_issue_as_list", lambda *a: [_issue(77, "OPEN")]
        )

        first = portal_sync.fold_status_for_issue(config, "acme-portal", 77)
        assert first.status == "planned"
        assert first.row is not None

        second = portal_sync.fold_status_for_issue(config, "acme-portal", 77)
        assert second.status == "planned"
        assert second.row is None
        assert "unchanged" in second.reason


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

    def test_folds_a_mix_of_milestone_and_issue_scoped_links(self, monkeypatch):
        """#2665: `sync_submission_statuses` must route each link to the
        right fold — milestone-scoped through `fold_status_for_milestone`,
        issue-scoped through `fold_status_for_issue` — independently."""
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id="sub-ms"
        )
        portal_store.link_issue(
            repo_name="acme-portal", issue_number=77, submission_id="sub-issue"
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")]
        )
        monkeypatch.setattr(
            portal_sync, "_single_issue_as_list", lambda *a: [_issue(77, "OPEN")]
        )

        results = portal_sync.sync_submission_statuses(config)

        by_submission = {r.submission_id: r for r in results}
        assert by_submission["sub-ms"].status == "shipped"
        assert by_submission["sub-issue"].status == "planned"


def _status_values(submission_id: str = SUB) -> list[str]:
    """Every status ever queued for a submission, oldest first — the outbox
    shape #3096 was diagnosed from (`SELECT status FROM portal_outbox WHERE
    kind='status'`)."""
    return [
        r.fields["status"]
        for r in portal_store.outbox_for_submission(submission_id)
        if r.kind == portal_sync.KIND_STATUS
    ]


class TestOscillationBetweenTwoLinks:
    """#3096, the actual production incident: SUB-1EA1D3 accumulated 322
    status rows alternating in-progress/shipped, one PAIR per daemon tick a
    second apart, and mailed a real customer 100+ times over seven days.

    The cause was two `coord portal link` records naming one submission_id —
    a milestone-scoped one (several issues, one still open -> in-progress)
    and an issue-scoped one (#2665; that issue closed -> shipped). Each tick
    folded both independently and enqueued whichever answer each produced;
    #2588's churn guard compared only against the LAST queued status, so
    every push differed from its predecessor and none was suppressed.
    """

    @staticmethod
    def _both_links_on_one_submission():
        """Construct the pathological "two live links, one submission_id"
        state this whole class exercises #3096's arbitration backstop over.

        #3110 made `coord.state._save_portal_link_local` refuse a normal
        `link_milestone`/`link_issue` write that would create exactly this
        shape — its own escape hatch (`force=True`) deliberately REPLACES
        the other target's claim rather than leaving both alive, since
        leaving both alive is the flood bug itself. So this can no longer be
        reached through the public write path at all, which is the right
        outcome going forward — but the arbitration this class tests is a
        BACKSTOP for data that predates the guard, or slipped past it some
        other way (a stale replica, a hand-edited board_meta row), and that
        backstop still needs a way to be exercised. Write both records
        directly to `board_meta`, bypassing the guard on purpose.
        """
        import json

        from coord import sql
        from coord.db import get_connection

        conn = get_connection()
        links = [
            {
                "repo_name": "acme-portal",
                "milestone_number": 5,
                "issue_number": None,
                "submission_id": SUB,
                "linked_at": 0.0,
                "actor": "",
                "schema": 1,
            },
            {
                "repo_name": "acme-portal",
                "milestone_number": None,
                "issue_number": 77,
                "submission_id": SUB,
                "linked_at": 0.0,
                "actor": "",
                "schema": 1,
            },
        ]
        sql.upsert(
            conn,
            "board_meta",
            ["key", "value"],
            ("portal_links", json.dumps(links)),
            conflict_columns=["key"],
        )

    @staticmethod
    def _oscillating_folds(monkeypatch):
        # The milestone sees an open sibling -> in-progress.
        monkeypatch.setattr(
            portal_sync, "_milestone_issues",
            lambda *a: [_issue(77, "CLOSED"), _issue(78, "OPEN")],
        )
        # The lone linked issue is closed -> shipped.
        monkeypatch.setattr(
            portal_sync, "_single_issue_as_list", lambda *a: [_issue(77, "CLOSED")]
        )

    def test_repeated_ticks_produce_one_stable_status_not_an_alternation(
        self, monkeypatch,
    ):
        """The regression test for the flood itself: consecutive ticks over
        the exact state that produced 322 rows must produce ONE."""
        self._both_links_on_one_submission()
        self._oscillating_folds(monkeypatch)
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        board = _board_with_started("acme-portal", {78})

        for _ in range(5):
            portal_sync.sync_submission_statuses(config, board=board)

        assert _status_values() == ["in-progress"]

    def test_the_losing_link_reports_rather_than_pushing(self, monkeypatch):
        """Refused AND escalated. Silently skipping the duplicate would fix
        the flood and hide the misconfiguration that caused it — and "no
        surface in coord reported anything wrong" is half of why this ran for
        seven days."""
        self._both_links_on_one_submission()
        self._oscillating_folds(monkeypatch)
        config = FakeConfig({"acme-portal": FakeRepoCfg()})

        results = portal_sync.sync_submission_statuses(
            config, board=_board_with_started("acme-portal", {78})
        )

        refused = [r for r in results if r.failed]
        assert len(refused) == 1
        assert refused[0].submission_id == SUB
        assert refused[0].row is None
        assert "issue #77" in refused[0].reason
        assert "not the authoritative link" in refused[0].reason
        # names the winner, so an operator knows which one to drop
        assert "ms-5" in refused[0].reason

    def test_milestone_link_wins_over_issue_link(self, monkeypatch):
        """A milestone link folds every issue in the submission; an issue
        link only ever sees its own. The complete answer wins."""
        self._both_links_on_one_submission()
        self._oscillating_folds(monkeypatch)

        assert portal_sync.authoritative_link(SUB).milestone_number == 5

    def test_the_merge_time_caller_refuses_the_same_link_the_tick_does(
        self, monkeypatch,
    ):
        """Both automatic callers reach the outbox through
        `_fold_status_for_link`, so `coord.merge_queue._maybe_push_status`
        entering by issue number cannot push what the tick refuses."""
        self._both_links_on_one_submission()
        self._oscillating_folds(monkeypatch)
        config = FakeConfig({"acme-portal": FakeRepoCfg()})

        result = portal_sync.fold_status_for_issue(config, "acme-portal", 77)

        assert result.row is None
        assert result.failed is True
        assert "not the authoritative link" in result.reason
        assert _status_values() == []

    def test_a_single_link_is_unaffected(self, monkeypatch):
        """Fails open on the ordinary case — one link, which is every
        submission in the system except the one that flooded."""
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")]
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})

        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        assert result.status == "shipped"
        assert result.row is not None
        assert result.failed is False


class TestStatusReentryGuard:
    """#3096: the customer vocabulary is a lifecycle, not a state machine
    that revisits. #2588's guard suppressed a REPEAT (`A -> A`); these cover
    the ALTERNATION (`A -> B -> A`) it could never see."""

    @staticmethod
    def _linked_config():
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        return FakeConfig({"acme-portal": FakeRepoCfg()})

    def test_alternating_fold_is_refused_after_one_round_trip(self, monkeypatch):
        """A pre-release `A -> B -> A` oscillation is still refused — this
        no longer routes through `shipped` (#3106 made the specific
        `in-progress -> shipped -> in-progress` shape this test used to use
        legitimate: it now folds to `shipped -> post-shipped`, not a refused
        re-entry into `in-progress` — see
        `test_shipped_moves_forward_into_post_shipped_when_new_work_lands`
        below for that case). The board here toggles whether issue 1 has
        started, oscillating in-progress/planned without ever reaching
        all-closed, to keep exercising the ordinary rule."""
        config = self._linked_config()
        started_board = _board_with_started("acme-portal", {1})
        stages = [
            (started_board, [_issue(1, "OPEN")]),  # in-progress
            (None, [_issue(1, "OPEN")]),  # planned
            (started_board, [_issue(1, "OPEN")]),  # in-progress again
        ]
        for board, issues in stages:
            monkeypatch.setattr(portal_sync, "_milestone_issues", lambda *a, _i=issues: _i)
            result = portal_sync.fold_status_for_milestone(
                config, "acme-portal", 5, board=board,
            )

        # The third fold wanted in-progress again — a genuine CHANGE from the
        # last push, which is exactly what #2588's guard waves through.
        assert result.status == "in-progress"
        assert result.row is None
        assert result.failed is True
        assert _status_values() == ["in-progress", "planned"]

    def test_shipped_no_longer_blocks_planned_or_in_progress_reentry_at_the_guard(
        self,
    ):
        """`shipped` is still terminal for the PRE-release buckets — the
        guard-level check that used to be exercised by the fold before
        #3106 (the fold itself can no longer reach `planned`/`in-progress`
        once shipped; see `fold_submission_status`'s `already_shipped`)."""
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        enqueue_status(SUB, "shipped")

        reason = portal_sync._reentry_block_reason(SUB, "planned")

        assert reason is not None
        assert "terminal" in reason

    def test_shipped_moves_forward_into_post_shipped_when_new_work_lands(
        self, monkeypatch,
    ):
        """#3106: `shipped` no longer strands a submission — new post-release
        work (a bug fix, a small enhancement) folds into `post-shipped`
        instead of the fold getting silently stuck re-computing `planned`/
        `in-progress` and being refused as an "un-ship"."""
        config = self._linked_config()
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")]
        )
        first = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)
        assert first.status == "shipped"

        # A brand-new issue lands under the shipped milestone — exactly
        # #3106's own reproduction (SUB-1EA1D3 took twelve of these).
        monkeypatch.setattr(
            portal_sync, "_milestone_issues",
            lambda *a: [_issue(1, "CLOSED"), _issue(2, "OPEN")],
        )
        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        assert result.status == "post-shipped"
        assert result.row is not None
        assert result.failed is False
        assert _status_values() == ["shipped", "post-shipped"]

    def test_post_shipped_is_refused_before_any_shipped_notification(self):
        """#3106: post-release maintenance implies a release happened first
        — `post-shipped` cannot be entered on a submission that has never
        been notified `shipped`, even if it has seen other statuses."""
        enqueue_status(SUB, "in-progress")

        reason = portal_sync._reentry_block_reason(SUB, "post-shipped")

        assert reason is not None
        assert "shipped" in reason

    def test_post_shipped_can_be_re_entered_after_shipped(self):
        """#3106: unlike every other status, `post-shipped` is not one-shot
        — a submission legitimately takes many post-release fixes over a
        long time, and the guard must not treat a second `post-shipped`
        notification as an oscillation."""
        enqueue_status(SUB, "shipped")
        enqueue_status(SUB, "post-shipped")

        assert portal_sync._reentry_block_reason(SUB, "post-shipped") is None

    def test_re_shipping_after_shipped_stays_the_quiet_unchanged_no_op(
        self, monkeypatch,
    ):
        """The overwhelmingly common post-ship tick: still shipped. #2588's
        churn guard catches it first, so it must NOT be reported as a
        failure — an error line on every tick of every shipped submission
        would be its own kind of flood."""
        config = self._linked_config()
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")]
        )
        portal_sync.fold_status_for_milestone(config, "acme-portal", 5)
        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        assert result.row is None
        assert result.failed is False
        assert "unchanged" in result.reason

    def test_the_ordinary_forward_lifecycle_still_flows(self, monkeypatch):
        """planned -> in-progress -> shipped is three distinct statuses and
        must still produce three pushes."""
        config = self._linked_config()
        board = _board_with_started("acme-portal", {1})
        stages = [
            (None, [_issue(1, "OPEN")]),
            (board, [_issue(1, "OPEN")]),
            (board, [_issue(1, "CLOSED")]),
        ]
        for b, issues in stages:
            monkeypatch.setattr(portal_sync, "_milestone_issues", lambda *a, _i=issues: _i)
            portal_sync.fold_status_for_milestone(config, "acme-portal", 5, board=b)

        assert _status_values() == ["planned", "in-progress", "shipped"]


class TestStatusFloodCeiling:
    """#3096's backstop: a per-submission ceiling that holds regardless of
    which bug is upstream of it. 322 pushes for one submission while every
    other in the system had four should have tripped something."""

    @staticmethod
    def _linked_config():
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        return FakeConfig({"acme-portal": FakeRepoCfg()})

    @staticmethod
    def _fill_to_ceiling(now: float):
        # Six rows, none of them `shipped`, so the re-entry guard above lets
        # a `shipped` fold through and the ceiling is what stops it.
        for status in (
            "describing", "in-design", "planned", "in-progress", "on-hold", "on-hold",
        ):
            enqueue_status(SUB, status, now=now)

    def test_pushes_over_the_ceiling_are_refused_and_reported(self, monkeypatch):
        config = self._linked_config()
        self._fill_to_ceiling(1000.0)
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")]
        )

        result = portal_sync.fold_status_for_milestone(
            config, "acme-portal", 5, now=1000.0,
        )

        assert result.status == "shipped"
        assert result.row is None
        assert result.failed is True
        assert "ceiling" in result.reason
        assert "shipped" not in _status_values()

    def test_the_window_rolls_so_a_long_lived_submission_is_not_frozen(
        self, monkeypatch,
    ):
        """A submission legitimately moves through the vocabulary more than a
        handful of times over months — just never in one day."""
        config = self._linked_config()
        self._fill_to_ceiling(1000.0)
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")]
        )

        later = 1000.0 + 25 * 60 * 60
        result = portal_sync.fold_status_for_milestone(
            config, "acme-portal", 5, now=later,
        )

        assert result.row is not None
        assert result.status == "shipped"

    def test_a_healthy_submission_never_comes_near_it(self, monkeypatch):
        """Calibration check: the whole planned -> in-progress -> shipped
        lifecycle inside one tick-window stays well under the ceiling —
        every non-flooding submission measured in production had <= 4 status
        rows, ever."""
        config = self._linked_config()
        board = _board_with_started("acme-portal", {1})
        for b, issues in (
            (None, [_issue(1, "OPEN")]),
            (board, [_issue(1, "OPEN")]),
            (board, [_issue(1, "CLOSED")]),
        ):
            monkeypatch.setattr(portal_sync, "_milestone_issues", lambda *a, _i=issues: _i)
            result = portal_sync.fold_status_for_milestone(
                config, "acme-portal", 5, board=b, now=1000.0,
            )
            assert result.failed is False

        assert len(_status_values()) == 3


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


# ── consuming question answers (#2749, IL-3) ─────────────────────────────────
#
# The gap the issue describes directly: `question.answered` is pulled and
# stored (`portal_events`) but nothing ever reads it — a client answers into
# a void. These tests are the "Done when" bar: a pushed question, answered,
# shows up paired in the ledger, the event is stamped `handled_at`, and the
# submission's status moves off `needs-input`.


def _question_answered_page(
    *, revision: int = 1, answer: str = "Yes, offline-first.", answered_by: str = "jane",
    event_id: str = "e1",
) -> dict:
    return {
        "events": [
            {
                "id": event_id,
                "submission_id": SUB,
                "type": "question.answered",
                "data": {"revision": revision, "answer": answer, "answered_by": answered_by},
            }
        ],
        "cursor": "c1",
        "has_more": False,
    }


def _push_and_apply_question(question: str = "Offline-first, yes or no?"):
    """Enqueue a question and simulate the portal confirming it — the same
    path `mark_applied`'s `kind == "question"` branch runs for real once
    `_push` gets a 200 back, which is what writes the `question_pushed`
    ledger entry `_consume_questions` later pairs an answer against."""
    question_row, _status_row = enqueue_question(SUB, question)
    portal_store.mark_applied(question_row)
    return question_row


class TestQuestionAnswerFields:
    def test_recognizes_dotted_kind_with_nested_data(self) -> None:
        event = portal_store.PortalEvent(
            event_id="e1", submission_id=SUB, kind="question.answered",
            occurred_at="", payload={"data": {"revision": 1, "answer": "Yes."}},
            received_at=1.0,
        )
        fields = portal_sync._question_answer_fields(event)
        assert fields == {
            "answer": "Yes.", "question_revision": 1, "answered_by": "customer",
        }

    def test_recognizes_underscore_kind_and_top_level_fields(self) -> None:
        event = portal_store.PortalEvent(
            event_id="e1", submission_id=SUB, kind="question_answered",
            occurred_at="",
            payload={"answer_text": "No thanks.", "revision": "2", "actor": "jane"},
            received_at=1.0,
        )
        fields = portal_sync._question_answer_fields(event)
        assert fields == {
            "answer": "No thanks.", "question_revision": 2, "answered_by": "jane",
        }

    def test_non_matching_kind_is_none(self) -> None:
        event = portal_store.PortalEvent(
            event_id="e1", submission_id=SUB, kind="signoff.approved",
            occurred_at="", payload={}, received_at=1.0,
        )
        assert portal_sync._question_answer_fields(event) is None

    def test_matching_kind_with_no_answer_text_is_none(self) -> None:
        event = portal_store.PortalEvent(
            event_id="e1", submission_id=SUB, kind="question.answered",
            occurred_at="", payload={"revision": 1}, received_at=1.0,
        )
        assert portal_sync._question_answer_fields(event) is None


class TestConsumeQuestions:
    def test_answer_is_ledgered_paired_with_its_question_and_event_marked_handled(
        self,
    ) -> None:
        row = _push_and_apply_question()
        client = FakeClient(pages=[_question_answered_page(revision=row.revision)])
        result = sync_tick(client=client)

        assert result.questions_consumed == 1
        [q_entry, a_entry] = portal_store.ledger_for_submission(SUB)
        assert q_entry.kind == portal_store.LEDGER_KIND_QUESTION_PUSHED
        assert a_entry.kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED
        assert a_entry.question_revision == q_entry.question_revision == row.revision
        assert a_entry.text == "Yes, offline-first."
        assert a_entry.actor == "jane"
        assert portal_store.unhandled_events() == []

    def test_with_no_config_the_ledger_append_still_happens(self) -> None:
        """Unlike `_consume_verdicts`, ledgering needs no repo/dispatch
        context — only the courtesy status nudge does
        (`_record_question_answer` degrades that alone to a no-op) — so
        calling `_consume_questions` directly with `config=None` (the
        documented `sync_tick(client=...)` bypass path) must still ledger
        the answer and mark the event handled."""
        row = _push_and_apply_question()
        answer_event = _question_answered_page(revision=row.revision)["events"][0]
        portal_store.record_events([answer_event])

        consumed, errors = portal_sync._consume_questions(None)

        assert consumed == 1
        assert errors == []
        assert any(
            e.kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED
            for e in portal_store.ledger_for_submission(SUB)
        )
        assert portal_store.unhandled_events() == []

    def test_an_answer_with_no_matching_pushed_question_is_still_ledgered(self) -> None:
        """No `_push_and_apply_question` here — the revision in the answer
        matches nothing in the outbox. The answer must still be recorded
        (never dropped); it is just imperfectly paired."""
        client = FakeClient(pages=[_question_answered_page(revision=999)])
        result = sync_tick(client=client)

        assert result.questions_consumed == 1
        [entry] = portal_store.ledger_for_submission(SUB)
        assert entry.kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED
        assert entry.question_revision == 999

    def test_a_second_tick_does_not_re_ledger_the_same_event(self) -> None:
        row = _push_and_apply_question()
        client = FakeClient(pages=[_question_answered_page(revision=row.revision)])
        first = sync_tick(client=client)
        second = sync_tick(client=FakeClient())  # nothing new to pull

        assert first.questions_consumed == 1
        assert second.questions_consumed == 0
        assert len(portal_store.ledger_for_submission(SUB)) == 2  # push + answer, not 3

    def test_a_ledger_write_failure_freezes_the_watermark_for_retry(
        self, monkeypatch
    ) -> None:
        """Unlike "no link on file" / "no matching queued question" (both
        best-effort, never raise), a genuine `append_ledger_entry` failure
        (a locked DB, say) is transient and deserves a retry — the watermark
        must NOT advance past it, mirroring `_consume_verdicts`'s freeze."""
        row = _push_and_apply_question()

        real_append = portal_store.append_ledger_entry

        def _boom(submission_id, kind, **kw):
            if kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED:
                raise RuntimeError("database is locked")
            return real_append(submission_id, kind, **kw)

        monkeypatch.setattr(portal_store, "append_ledger_entry", _boom)

        client = FakeClient(pages=[_question_answered_page(revision=row.revision)])
        result = sync_tick(client=client)

        assert result.questions_consumed == 0
        assert any("database is locked" in e for e in result.errors)
        assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]

        # Retry once the DB is healthy again: the watermark was frozen, so
        # the SAME event is reachable next tick rather than skipped.
        monkeypatch.setattr(portal_store, "append_ledger_entry", real_append)
        retry = sync_tick(client=FakeClient())
        assert retry.questions_consumed == 1
        assert portal_store.unhandled_events() == []

    def test_answering_nudges_the_submission_off_needs_input(self, monkeypatch) -> None:
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        row = _push_and_apply_question()
        needs_input_row = enqueue_status(SUB, "needs-input")
        portal_store.mark_applied(needs_input_row)
        assert portal_store.get_submission(SUB).last_status == "needs-input"

        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "OPEN")],
        )
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        client = FakeClient(pages=[_question_answered_page(revision=row.revision)])

        result = sync_tick(config=config, client=client)

        assert result.questions_consumed == 1
        # The status fold queued (and, since this client never errors,
        # drained) a folded status different from `needs-input` — the
        # submission's CONFIRMED status is no longer `needs-input`.
        assert portal_store.get_submission(SUB).last_status != "needs-input"

    def test_no_link_on_file_does_not_block_the_ledger_append(self, monkeypatch) -> None:
        """No `coord portal link` recorded — the courtesy status nudge has
        nowhere to resolve to, but that must not stop the answer itself
        from being ledgered and the event from being marked handled."""
        row = _push_and_apply_question()
        config = FakeConfig({"acme-portal": FakeRepoCfg()})
        client = FakeClient(pages=[_question_answered_page(revision=row.revision)])

        result = sync_tick(config=config, client=client)

        assert result.questions_consumed == 1
        assert portal_store.unhandled_events() == []

    def test_a_large_non_actionable_backlog_does_not_starve_a_later_answer(self) -> None:
        """Same #2509-style regression this issue explicitly calls out
        (`portal_sync.py:1092`'s comment) reproduced for THIS consumer: 150
        non-actionable events ahead of one real `question.answered` event
        must not make it unreachable."""
        row = _push_and_apply_question()
        noise = [
            {"id": f"noise-{i}", "submission_id": SUB, "type": "created"} for i in range(150)
        ]
        portal_store.record_events(noise)
        answer_event = _question_answered_page(revision=row.revision)["events"][0]
        portal_store.record_events([answer_event])

        for _ in range(5):
            consumed, _errors = portal_sync._consume_questions(
                None, limit=50, pages=1
            )
            if consumed:
                break

        assert consumed == 1
        assert any(
            entry.kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED
            and entry.question_revision == row.revision
            for entry in portal_store.ledger_for_submission(SUB)
        )


# ── #2903 (phase 1 of #2902): the draft gate ────────────────────────────────


class TestDraftGate:
    """No design round or question leaves the outbox unapproved.

    Every test here passes an explicit *config*, so it exercises the real
    `_approval_config` read rather than the module-level ungating fixture
    above.
    """

    def test_default_policy_gates_the_two_prose_kinds(self):
        gated = _Cfg(_approval())
        assert portal_sync.initial_outbox_state("design_round", config=gated) == (
            portal_store.STATE_DRAFT
        )
        assert portal_sync.initial_outbox_state("question", config=gated) == (
            portal_store.STATE_DRAFT
        )
        assert portal_sync.initial_outbox_state("status", config=gated) == (
            portal_store.STATE_PENDING
        )
        assert portal_sync.initial_outbox_state("preview", config=gated) == (
            portal_store.STATE_PENDING
        )

    def test_a_gated_design_round_lands_in_draft_and_the_drain_never_sends_it(self):
        row = enqueue_design_round(SUB, _design(), config=_Cfg(_approval()))
        assert row.state == portal_store.STATE_DRAFT
        assert portal_store.pending_outbox() == []

        client = FakeClient()
        result = sync_tick(client=client)

        assert client.pushes == []
        assert result.applied == 0

    def test_a_gated_question_holds_its_own_needs_input_announcement(self):
        """The #835 guarantee, straight through the gate: the mail cannot
        overtake the question just because the question is with the operator."""
        question, status = enqueue_question(SUB, "which blue?", config=_Cfg(_approval()))
        assert question.state == portal_store.STATE_DRAFT
        assert status.state == portal_store.STATE_PENDING

        client = FakeClient()
        result = sync_tick(client=client)

        assert client.pushes == []
        assert result.held == 1
        held = [r for r in portal_store.outbox_for_submission(SUB) if r.seq == status.seq][0]
        assert "unapproved draft" in held.reason

    def test_status_and_preview_are_unchanged_by_the_gate(self):
        gated = _Cfg(_approval())
        preview = enqueue_preview(SUB, "https://pr-1.example.pages.dev", config=gated)
        status = enqueue_status(SUB, "quality-check", config=gated)
        assert preview.state == portal_store.STATE_PENDING
        assert status.state == portal_store.STATE_PENDING

        client = FakeClient()
        result = sync_tick(client=client)

        assert client.pushed_kinds == ["preview_url", "status"]
        assert result.applied == 2

    def test_approving_a_draft_lets_the_next_drain_send_it_in_order(self):
        gated = _Cfg(_approval())
        enqueue_design_round(SUB, _design(), config=gated)
        enqueue_status(SUB, "awaiting-signoff", config=gated)

        assert sync_tick(client=FakeClient()).applied == 0  # gated

        portal_store.approve_draft(SUB, 1, actor="john")
        client = FakeClient()
        result = sync_tick(client=client)

        assert client.pushed_kinds == ["design_round", "status"]
        assert result.applied == 2

    def test_ordering_block_reason_names_the_unapproved_draft(self):
        gated = _Cfg(_approval())
        enqueue_design_round(SUB, _design(), config=gated)
        announcement = enqueue_status(SUB, "awaiting-signoff", config=gated)

        reason = ordering_block_reason(announcement)
        assert reason is not None
        assert "unapproved draft" in reason
        assert "coord portal drafts" in reason

    def test_a_relaxed_policy_sends_a_design_round_straight_through(self):
        row = enqueue_design_round(SUB, _design(), config=_ungated())
        assert row.state == portal_store.STATE_PENDING

        client = FakeClient()
        assert sync_tick(client=client).applied == 1

    def test_a_gated_status_is_possible_too(self):
        row = enqueue_status(SUB, "in-design", config=_Cfg(_approval(status=True)))
        assert row.state == portal_store.STATE_DRAFT

    def test_the_drain_refuses_a_non_pending_row_even_if_handed_one(self, monkeypatch):
        """The belt-and-braces guard in `_push`: `pending_outbox()` already
        filters, so this can only fire if that query is ever loosened — which
        is exactly the regression the acceptance bar ("by any path") is
        about."""
        draft = enqueue_design_round(SUB, _design(), config=_Cfg(_approval()))
        monkeypatch.setattr(portal_store, "pending_outbox", lambda limit=None: [draft])

        client = FakeClient()
        result = sync_tick(client=client)

        assert client.pushes == []
        assert result.applied == 0

    def test_rejecting_a_draft_question_also_rejects_its_announcement(self):
        """Otherwise the `needs-input` behind it is held forever."""
        _question, status = enqueue_question(SUB, "which blue?", config=_Cfg(_approval()))
        portal_store.reject_draft(SUB, 1, "already answered in intake")

        rows = portal_store.outbox_for_submission(SUB)
        assert [r.state for r in rows] == [
            portal_store.STATE_REJECTED,
            portal_store.STATE_REJECTED,
        ]
        assert portal_store.pending_outbox() == []
        # ...and the drain has nothing left to hold forever.
        assert sync_tick(client=FakeClient()).held == 0
        assert status.seq == 2

    def test_a_draft_blocks_rows_queued_behind_it_on_the_same_submission(self):
        """It keeps its seq, so per-submission FIFO still holds. Correct: a
        later status must not describe a submission state the customer was
        never told about."""
        enqueue_design_round(SUB, _design(), config=_Cfg(_approval()))
        later = enqueue_status(SUB, "in-design", config=_Cfg(_approval()))

        client = FakeClient()
        result = sync_tick(client=client)

        assert client.pushes == []
        assert result.held == 1
        held = [r for r in portal_store.outbox_for_submission(SUB) if r.seq == later.seq][0]
        assert "an earlier design_round (seq 1) is an unapproved draft" in held.reason
        # It is a HOLD, not a failure: nothing was sent, so nothing counts as
        # an attempt.
        assert held.attempts == 0

    def test_a_gated_submission_does_not_stall_another_customers(self):
        enqueue_design_round(SUB, _design(), config=_Cfg(_approval()))
        enqueue_status("sub-002", "in-design", config=_Cfg(_approval()))

        client = FakeClient()
        result = sync_tick(client=client)

        assert client.pushed_kinds == ["status"]
        assert result.applied == 1


class TestApprovalConfigRead:
    def test_an_object_with_no_portal_block_reads_as_the_default(self, monkeypatch):
        monkeypatch.undo()  # drop the module-level ungating fixture
        assert portal_sync.initial_outbox_state(
            "question", config=object()
        ) == portal_store.STATE_DRAFT

    def test_an_unreadable_config_falls_back_to_the_defaults(self, monkeypatch):
        monkeypatch.undo()
        from coord import config as config_mod

        def _boom(*_a, **_k):
            raise config_mod.ConfigError("no coordinator.yml anywhere")

        monkeypatch.setattr(config_mod, "load", _boom)
        assert portal_sync.initial_outbox_state("design_round") == (
            portal_store.STATE_DRAFT
        )
        assert portal_sync.initial_outbox_state("status") == portal_store.STATE_PENDING


# ── #2987: relayed answers pushed OUT, and their client confirmation ───────
#
# The coord half of coord-portal#159. #2986 already records an out-of-band
# answer in the ledger; this is the OUTBOUND half that lets the client see
# it and confirm/correct it, draft-gated and ordering-guarded like every
# other prose kind.


def _relayed_answer_entry(
    *,
    question_revision: int = 1,
    text: str = "Yes, offline-first.",
    source: str = "phone",
    actor: str = "operator:jane",
) -> portal_store.LedgerEntry:
    """A #2986 relayed-answer ledger row, built directly
    (`append_ledger_entry`, not `answer_question`) so these tests exercise
    `enqueue_relayed_answer` in isolation from the #2987 auto-push hook
    `portal_store._push_relayed_answer` wires into `answer_question` itself
    — that integration has its own test below
    (`TestAnswerQuestionPushesOutbound`)."""
    return portal_store.append_ledger_entry(
        SUB,
        portal_store.LEDGER_KIND_QUESTION_ANSWERED,
        question_revision=question_revision,
        text=text,
        actor=actor,
        payload={"relayed": True, "source": source},
    )


def _relayed_answer_confirmed_page(
    *, question_revision: int = 1, event_id: str = "e1", confirmed_by: str = "jane",
) -> dict:
    return {
        "events": [
            {
                "id": event_id,
                "submission_id": SUB,
                "type": "relayed_answer.confirmed",
                "data": {
                    "question_revision": question_revision,
                    "confirmed_by": confirmed_by,
                },
            }
        ],
        "cursor": "c1",
        "has_more": False,
    }


def _relayed_answer_ungated():
    """Unlike the module-level `_ungated()` (which only relaxes
    `design_round`/`question`), this ALSO relaxes `relayed_answer` — needed
    whenever a test wants `enqueue_relayed_answer`'s two rows to land
    `pending` and actually reach the drain, rather than exercising the
    #2903 draft gate."""
    return _Cfg(_approval(relayed_answer=False))


class TestEnqueueRelayedAnswer:
    def test_queues_the_answer_and_its_own_needs_input_announcement(self):
        entry = _relayed_answer_entry()
        answer_row, status_row = enqueue_relayed_answer(SUB, entry, config=_ungated())

        assert answer_row.kind == portal_sync.KIND_RELAYED_ANSWER
        assert answer_row.fields["relayed_answer"]["text"] == "Yes, offline-first."
        assert answer_row.fields["relayed_answer"]["question_revision"] == 1
        assert answer_row.fields["relayed_answer"]["source"] == "phone"
        assert status_row.kind == portal_sync.KIND_STATUS
        assert status_row.fields["status"] == "needs-input"
        assert status_row.requires_kind == portal_sync.KIND_RELAYED_ANSWER
        assert status_row.announces == "needs-input"
        assert answer_row.seq < status_row.seq

    def test_refuses_a_non_relayed_ledger_entry(self):
        entry = portal_store.append_ledger_entry(
            SUB,
            portal_store.LEDGER_KIND_QUESTION_ANSWERED,
            question_revision=1,
            text="Yes.",
            actor="customer",
            payload={},
        )
        with pytest.raises(PortalSyncError, match="relayed-answer ledger entry"):
            enqueue_relayed_answer(SUB, entry)

    def test_refuses_an_empty_answer(self):
        entry = _relayed_answer_entry(text="   ")
        with pytest.raises(PortalSyncError, match="non-empty"):
            enqueue_relayed_answer(SUB, entry)

    def test_default_policy_gates_the_relayed_answer_too(self):
        gated = _Cfg(_approval())
        assert portal_sync.initial_outbox_state(
            portal_sync.KIND_RELAYED_ANSWER, config=gated
        ) == portal_store.STATE_DRAFT

    def test_a_gated_relayed_answer_lands_in_draft_and_is_never_sent_unapproved(self):
        entry = _relayed_answer_entry()
        answer_row, _status_row = enqueue_relayed_answer(
            SUB, entry, config=_Cfg(_approval())
        )
        assert answer_row.state == portal_store.STATE_DRAFT
        assert answer_row not in portal_store.pending_outbox()

        client = FakeClient()
        result = sync_tick(client=client)
        assert "relayed_answer" not in client.pushed_kinds
        assert result.applied == 0

    def test_editable_draft_field_is_the_answer_text(self):
        entry = _relayed_answer_entry()
        answer_row, _status = enqueue_relayed_answer(SUB, entry, config=_Cfg(_approval()))
        assert portal_store.EDITABLE_DRAFT_FIELDS[answer_row.kind] == (
            "relayed_answer.text",
        )


class TestRelayedAnswerOrdering:
    """#835, applied to `relayed_answer`: the mail cannot outrun the row it
    announces."""

    def test_answer_is_pushed_before_the_status_that_announces_it(self):
        entry = _relayed_answer_entry()
        enqueue_relayed_answer(SUB, entry, config=_relayed_answer_ungated())
        client = FakeClient()

        result = sync_tick(client=client)

        assert client.pushed_kinds == ["relayed_answer", "status"]
        assert result.applied == 2
        assert result.held == 0

    def test_announcement_is_held_while_the_answer_is_unconfirmed(self):
        """The crash-window case, #835's exact shape reproduced for
        `relayed_answer`: the answer push fails, so the mail must not go."""
        entry = _relayed_answer_entry()
        enqueue_relayed_answer(SUB, entry, config=_relayed_answer_ungated())
        client = FakeClient(push_error=PortalBridgeError("portal is down"))

        result = sync_tick(client=client)

        assert client.pushes == []
        assert result.applied == 0
        assert result.errors  # the failure is surfaced, not swallowed

        # Next tick, the portal is back: the answer goes first, then the
        # announcement — same revisions, no duplicates.
        client2 = FakeClient()
        result2 = sync_tick(client=client2)
        assert client2.pushed_kinds == ["relayed_answer", "status"]
        assert result2.applied == 2

    def test_second_relayed_answer_cannot_ride_on_the_first_confirmation(self):
        first = _relayed_answer_entry(question_revision=1, text="First.")
        answer1, _status1 = enqueue_relayed_answer(SUB, first, config=_ungated())
        portal_store.mark_applied(answer1)

        second = _relayed_answer_entry(question_revision=1, text="Second.")
        answer2, status2 = enqueue_relayed_answer(SUB, second, config=_ungated())

        # answer2 is still PENDING (unconfirmed) — the announcement behind it
        # must wait on IT, the latest, not answer1's already-applied state.
        reason = ordering_block_reason(status2)
        assert reason is not None
        assert f"seq {answer2.seq}" in reason

    def test_gated_relayed_answer_holds_its_own_needs_input_announcement(self):
        entry = _relayed_answer_entry()
        answer_row, status_row = enqueue_relayed_answer(
            SUB, entry, config=_Cfg(_approval())
        )
        assert answer_row.state == portal_store.STATE_DRAFT
        assert status_row.state == portal_store.STATE_PENDING

        client = FakeClient()
        result = sync_tick(client=client)

        assert client.pushes == []
        assert result.held == 1
        held = [
            r for r in portal_store.outbox_for_submission(SUB) if r.seq == status_row.seq
        ][0]
        assert "unapproved draft" in held.reason


class TestRelayedAnswerConfirmationFields:
    def test_recognizes_dotted_kind_with_nested_data(self) -> None:
        event = portal_store.PortalEvent(
            event_id="e1", submission_id=SUB, kind="relayed_answer.confirmed",
            occurred_at="", payload={"data": {"question_revision": 1}},
            received_at=1.0,
        )
        fields = portal_sync._relayed_answer_confirmation_fields(event)
        assert fields == {"question_revision": 1, "confirmed_by": "customer"}

    def test_recognizes_underscore_kind_and_top_level_fields(self) -> None:
        event = portal_store.PortalEvent(
            event_id="e1", submission_id=SUB, kind="relayed_answer_confirmed",
            occurred_at="",
            payload={"revision": "3", "actor": "jane"},
            received_at=1.0,
        )
        fields = portal_sync._relayed_answer_confirmation_fields(event)
        assert fields == {"question_revision": 3, "confirmed_by": "jane"}

    def test_non_matching_kind_is_none(self) -> None:
        event = portal_store.PortalEvent(
            event_id="e1", submission_id=SUB, kind="question.answered",
            occurred_at="", payload={}, received_at=1.0,
        )
        assert portal_sync._relayed_answer_confirmation_fields(event) is None


class TestConsumeRelayedAnswerConfirmations:
    def test_confirmation_is_ledgered_paired_with_its_answer_and_event_marked_handled(
        self,
    ) -> None:
        entry = _relayed_answer_entry(question_revision=1)
        enqueue_relayed_answer(SUB, entry, config=_ungated())
        client = FakeClient(pages=[_relayed_answer_confirmed_page(question_revision=1)])

        result = sync_tick(client=client)

        assert result.relayed_answer_confirmations_consumed == 1
        confirmations = [
            e for e in portal_store.ledger_for_submission(SUB)
            if e.kind == portal_store.LEDGER_KIND_ANSWER_CONFIRMED
        ]
        assert len(confirmations) == 1
        assert confirmations[0].question_revision == 1
        assert confirmations[0].actor == "jane"
        assert portal_store.unhandled_events() == []

    def test_confirmation_and_answer_both_stay_visible_in_the_briefing(self) -> None:
        pushed = _push_and_apply_question()
        entry = _relayed_answer_entry(
            question_revision=pushed.revision, text="Yes, offline-first."
        )
        enqueue_relayed_answer(SUB, entry, config=_ungated())
        client = FakeClient(
            pages=[_relayed_answer_confirmed_page(question_revision=pushed.revision)]
        )
        sync_tick(client=client)

        payload = portal_store.render_ledger_payload(SUB)
        [bucket] = [
            b for b in payload["qa"] if b["question_revision"] == pushed.revision
        ]
        assert len(bucket["answers"]) == 1
        assert bucket["answers"][0]["relayed"] is True
        assert len(bucket["confirmations"]) == 1

    def test_a_second_tick_does_not_re_ledger_the_same_confirmation(self) -> None:
        entry = _relayed_answer_entry(question_revision=1)
        enqueue_relayed_answer(SUB, entry, config=_ungated())
        client = FakeClient(pages=[_relayed_answer_confirmed_page(question_revision=1)])
        first = sync_tick(client=client)
        second = sync_tick(client=FakeClient())

        assert first.relayed_answer_confirmations_consumed == 1
        assert second.relayed_answer_confirmations_consumed == 0
        confirmations = [
            e for e in portal_store.ledger_for_submission(SUB)
            if e.kind == portal_store.LEDGER_KIND_ANSWER_CONFIRMED
        ]
        assert len(confirmations) == 1

    def test_a_correction_lands_as_a_normal_answer_alongside_the_relayed_one(
        self,
    ) -> None:
        """The other half of #2987's acceptance bar: a CORRECTION needs no
        new consumer — it is an ordinary `question.answered` event, handled
        by the pre-existing #2749 consumer, and both rows stay visible."""
        pushed = _push_and_apply_question()
        entry = _relayed_answer_entry(
            question_revision=pushed.revision, text="Relayed: yes."
        )
        enqueue_relayed_answer(SUB, entry, config=_ungated())
        client = FakeClient(
            pages=[
                _question_answered_page(
                    revision=pushed.revision, answer="Actually, no.",
                    answered_by="the client", event_id="e2",
                )
            ]
        )

        result = sync_tick(client=client)

        assert result.questions_consumed == 1
        payload = portal_store.render_ledger_payload(SUB)
        [bucket] = [
            b for b in payload["qa"] if b["question_revision"] == pushed.revision
        ]
        assert [a["text"] for a in bucket["answers"]] == [
            "Relayed: yes.", "Actually, no.",
        ]
        assert [a["relayed"] for a in bucket["answers"]] == [True, False]

    def test_a_ledger_write_failure_freezes_the_watermark_for_retry(
        self, monkeypatch
    ) -> None:
        entry = _relayed_answer_entry(question_revision=1)
        enqueue_relayed_answer(SUB, entry, config=_ungated())

        real_append = portal_store.append_ledger_entry

        def _boom(submission_id, kind, **kw):
            if kind == portal_store.LEDGER_KIND_ANSWER_CONFIRMED:
                raise RuntimeError("database is locked")
            return real_append(submission_id, kind, **kw)

        monkeypatch.setattr(portal_store, "append_ledger_entry", _boom)

        client = FakeClient(pages=[_relayed_answer_confirmed_page(question_revision=1)])
        result = sync_tick(client=client)

        assert result.relayed_answer_confirmations_consumed == 0
        assert any("database is locked" in e for e in result.errors)
        assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]

        monkeypatch.setattr(portal_store, "append_ledger_entry", real_append)
        retry = sync_tick(client=FakeClient())
        assert retry.relayed_answer_confirmations_consumed == 1
        assert portal_store.unhandled_events() == []

    def test_a_large_non_actionable_backlog_does_not_starve_a_later_confirmation(
        self,
    ) -> None:
        entry = _relayed_answer_entry(question_revision=1)
        enqueue_relayed_answer(SUB, entry, config=_ungated())
        noise = [
            {"id": f"noise-{i}", "submission_id": SUB, "type": "created"} for i in range(150)
        ]
        portal_store.record_events(noise)
        confirm_event = _relayed_answer_confirmed_page(question_revision=1)["events"][0]
        portal_store.record_events([confirm_event])

        consumed = 0
        for _ in range(5):
            consumed, _errors = portal_sync._consume_relayed_answer_confirmations(
                None, limit=50, pages=1
            )
            if consumed:
                break

        assert consumed == 1


class TestAnswerQuestionPushesOutbound:
    """#2987 acceptance criterion #1: `coord portal answer` (#2986) enqueues
    the relayed answer outbound, draft-gated, going through
    `portal_store.answer_question` exactly as `coord portal answer` and the
    dashboard/daemon seams all do — not `enqueue_relayed_answer` directly."""

    def test_answering_a_pushed_question_enqueues_a_relayed_answer_row(self) -> None:
        row = _push_and_apply_question()

        portal_store.answer_question(SUB, "Yes, offline-first.", source="phone", actor="jane")

        relayed_rows = [
            r for r in portal_store.outbox_for_submission(SUB)
            if r.kind == portal_sync.KIND_RELAYED_ANSWER
        ]
        assert len(relayed_rows) == 1
        assert relayed_rows[0].fields["relayed_answer"]["question_revision"] == (
            row.revision
        )
        assert relayed_rows[0].fields["relayed_answer"]["text"] == "Yes, offline-first."
        # Gated by default (no config passed here — the ungating fixture
        # only relaxes design_round/question, matching production's real
        # default policy for relayed_answer): a human must approve it.
        assert relayed_rows[0].state == portal_store.STATE_DRAFT

    def test_a_failure_to_enqueue_never_breaks_the_ledger_write(self, monkeypatch) -> None:
        """#2179's failure posture, at the OUTBOUND-enqueue layer: even if
        queuing to the portal blows up, the answer stays recorded locally
        and `answer_question` does not raise."""
        _push_and_apply_question()

        def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(portal_sync, "enqueue_relayed_answer", _boom)

        entry = portal_store.answer_question(SUB, "Yes.", actor="jane")

        assert entry.text == "Yes."
        assert entry.payload == {"relayed": True, "source": "verbal"}
        assert [
            e for e in portal_store.ledger_for_submission(SUB)
            if e.kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED
        ] == [entry]
        assert [
            r for r in portal_store.outbox_for_submission(SUB)
            if r.kind == portal_sync.KIND_RELAYED_ANSWER
        ] == []


# ── #3071: the run kinds — emitting a submission's timeline ────────────────
#
# `portal_ledger` was built to carry a submission's whole run and only ever
# got Q&A rows. These tests are the "it now carries the rest" half; the join
# and `coord journal` itself are covered in `tests/test_portal_store.py`.


def _signoff_page(verdict: str = "approved", comments: str = "looks great") -> dict:
    return {
        "events": [
            {
                "id": "signoff-3071",
                "submission_id": SUB,
                "type": f"signoff.{verdict}",
                "data": {"verdict": verdict, "comments": comments},
            }
        ],
        "cursor": "c1",
        "has_more": False,
    }


class TestLedgerSignoffEvents:
    """#3071: every sign-off the customer records lands on the ledger —
    including an `approved` one, which `_consume_verdicts` deliberately
    dispatches nothing for and would therefore never have recorded."""

    def test_an_approved_signoff_is_ledgered_with_its_comment(self) -> None:
        client = FakeClient(pages=[_signoff_page()])
        sync_tick(client=client)

        [entry] = portal_store.ledger_for_submission(SUB)
        assert entry.kind == portal_store.LEDGER_KIND_SIGNOFF_RECORDED
        assert entry.text == "sign-off: approved — looks great"
        assert entry.actor == "customer"
        assert entry.source_event_id == "signoff-3071"
        assert entry.payload["verdict"] == "approved"

    def test_replaying_the_same_event_produces_no_duplicate_row(self) -> None:
        """The acceptance bar's fourth bullet: the existing `INSERT OR
        IGNORE` / `UNIQUE(submission_id, kind, source_event_id)` behaviour
        must still hold for the NEW kinds."""
        page = _signoff_page()
        sync_tick(client=FakeClient(pages=[page]))
        sync_tick(client=FakeClient(pages=[dict(page)]))

        signoffs = [
            e for e in portal_store.ledger_for_submission(SUB)
            if e.kind == portal_store.LEDGER_KIND_SIGNOFF_RECORDED
        ]
        assert len(signoffs) == 1

    def test_a_signoff_pulled_before_this_shipped_is_still_ledgered(self) -> None:
        """The watermark reason `_ledger_signoff_events` exists: on every
        existing install the verdict consumer's cursor has already moved past
        the sign-offs an operator most wants a timeline for. Simulated here
        by recording the event and advancing that watermark past it, exactly
        as a pre-#3071 tick would have left things."""
        event = _signoff_page(verdict="changes_requested", comments="bluer")["events"][0]
        portal_store.record_events([event])
        stored = portal_store.events_for_submission(SUB)[0]
        portal_store.set_verdict_watermark(stored.received_at + 1, "zzz")

        assert portal_sync._ledger_signoff_events() == 1
        [entry] = portal_store.ledger_for_submission(SUB)
        assert entry.text == "sign-off: changes_requested — bluer"

    def test_a_non_signoff_event_is_not_ledgered(self) -> None:
        portal_store.record_events(
            [{"id": "e-created", "submission_id": SUB, "type": "created"}]
        )
        assert portal_sync._ledger_signoff_events() == 0
        assert portal_store.ledger_for_submission(SUB) == []

    def test_a_ledger_write_failure_does_not_stop_the_batch(self, monkeypatch) -> None:
        portal_store.record_events(
            [
                {"id": "s1", "submission_id": SUB, "type": "signoff.approved"},
                {"id": "s2", "submission_id": "sub-002", "type": "signoff.approved"},
            ]
        )
        real = portal_store.append_ledger_entry

        def flaky(submission_id, kind, **kw):
            if submission_id == SUB:
                raise RuntimeError("locked database")
            return real(submission_id, kind, **kw)

        monkeypatch.setattr(portal_store, "append_ledger_entry", flaky)

        assert portal_sync._ledger_signoff_events() == 1
        assert portal_store.ledger_for_submission(SUB) == []
        assert len(portal_store.ledger_for_submission("sub-002")) == 1


class TestLedgerDesignRoundPublished:
    """#3071: written in `push_design_round_bundle` — the SHARED tail of
    `coord portal publish-mocks` and PDR-3's merge hook — so a round
    published either way lands on the timeline identically."""

    def test_publishing_a_bundle_ledgers_the_round_and_its_bundle_key(self) -> None:
        client = _UploadClient(bundle_key="bundles/sub-001/r2.tar")

        _key, row = portal_sync.push_design_round_bundle(
            client, SUB, {"contract.md": "x"},
            milestone_title="ms-4", tracking_issue_title="Epic",
            tracking_issue_body="body", round_number=2, config=_ungated(),
        )

        [entry] = portal_store.ledger_for_submission(SUB)
        assert entry.kind == portal_store.LEDGER_KIND_DESIGN_ROUND_PUBLISHED
        assert entry.text == "design round R2 published (bundle bundles/sub-001/r2.tar)"
        assert entry.payload["bundle_key"] == "bundles/sub-001/r2.tar"
        assert entry.source_event_id == portal_store.outbox_source_key(row.id)

    def test_the_ledgered_round_round_trips_through_the_journal_with_no_artifact(
        self,
    ) -> None:
        """Review finding on #3071: the test above only proves the ledger
        row itself is right — it never round-trips through
        `render_journal_payload`/`coord journal`, so it never observed that
        a REALISTIC bare `bundle_key` (this client's default,
        `"bundles/sub-001/r1.tar"`, with no scheme — exactly what
        `PortalBridgeClient.upload_bundle` actually returns) yields a null
        `artifact`, not a URL. `--json`'s contract is "null or a URL"; a bare
        object key must never be smuggled past it, even though it is still
        readable in `text`/`details`."""
        _key, _row = portal_sync.push_design_round_bundle(
            _UploadClient(), SUB, {"contract.md": "x"},
            milestone_title="ms-4", tracking_issue_title="Epic",
            tracking_issue_body="body", config=_ungated(),
        )

        [entry] = portal_store.render_journal_payload(SUB)["entries"]
        assert entry["kind"] == portal_store.LEDGER_KIND_DESIGN_ROUND_PUBLISHED
        assert entry["artifact"] is None
        assert entry["details"]["bundle_key"] == "bundles/sub-001/r1.tar"
        assert "bundles/sub-001/r1.tar" in entry["text"]

    def test_a_ledger_failure_does_not_fail_the_publish(self, monkeypatch) -> None:
        """The round really has been uploaded and queued by this point; a
        lost timeline row must not turn that into a raised error inside a
        merge hook."""
        def _boom(*_a, **_kw):
            raise RuntimeError("locked database")

        monkeypatch.setattr(portal_store, "append_ledger_entry", _boom)

        key, row = portal_sync.push_design_round_bundle(
            _UploadClient(), SUB, {"contract.md": "x"},
            milestone_title="ms-4", tracking_issue_title="Epic",
            tracking_issue_body="body", config=_ungated(),
        )
        assert key == "bundles/sub-001/r1.tar"
        assert row.kind == portal_sync.KIND_DESIGN_ROUND


class TestLedgerStatusChange:
    """#3071: `status_changed` (plus `work_started`/`work_shipped`) is
    written on `_fold_status_for_link`'s ENQUEUEING arm only."""

    def _link_and_config(self):
        portal_store.link_milestone(
            repo_name="acme-portal", milestone_number=5, submission_id=SUB
        )
        return FakeConfig({"acme-portal": FakeRepoCfg()})

    def test_work_starting_ledgers_both_status_changed_and_work_started(
        self, monkeypatch
    ):
        config = self._link_and_config()
        monkeypatch.setattr(
            portal_sync, "_milestone_issues",
            lambda *a: [_issue(1, "OPEN"), _issue(2, "CLOSED")],
        )

        portal_sync.fold_status_for_milestone(
            config, "acme-portal", 5, board=_board_with_started("acme-portal", {1}),
        )

        kinds = [e.kind for e in portal_store.ledger_for_submission(SUB)]
        assert kinds == [
            portal_store.LEDGER_KIND_STATUS_CHANGED,
            portal_store.LEDGER_KIND_WORK_STARTED,
        ]

    def test_everything_closing_ledgers_work_shipped(self, monkeypatch):
        config = self._link_and_config()
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "CLOSED")],
        )

        portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        kinds = [e.kind for e in portal_store.ledger_for_submission(SUB)]
        assert portal_store.LEDGER_KIND_WORK_SHIPPED in kinds

    def test_the_unchanged_arm_writes_nothing(self, monkeypatch):
        """The churn guard's whole point, restated in this surface: a
        timeline that logged 'still planned' once per tick is the same
        failure as re-mailing the customer."""
        config = self._link_and_config()
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "OPEN")],
        )

        portal_sync.fold_status_for_milestone(config, "acme-portal", 5)
        before = len(portal_store.ledger_for_submission(SUB))
        for _tick in range(3):
            portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        assert len(portal_store.ledger_for_submission(SUB)) == before

    def test_planned_gets_a_status_row_but_no_work_milestone(self, monkeypatch):
        config = self._link_and_config()
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "OPEN")],
        )

        portal_sync.fold_status_for_milestone(config, "acme-portal", 5)

        kinds = [e.kind for e in portal_store.ledger_for_submission(SUB)]
        assert kinds == [portal_store.LEDGER_KIND_STATUS_CHANGED]

    def test_a_ledger_failure_still_reports_the_status_as_queued(self, monkeypatch):
        config = self._link_and_config()
        monkeypatch.setattr(
            portal_sync, "_milestone_issues", lambda *a: [_issue(1, "OPEN")],
        )

        def _boom(*_a, **_kw):
            raise RuntimeError("locked database")

        monkeypatch.setattr(portal_store, "append_ledger_entry", _boom)

        result = portal_sync.fold_status_for_milestone(config, "acme-portal", 5)
        assert result.queued is True
        assert result.failed is False
