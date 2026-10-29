"""Tests for coord.upstream_gaps — the BLOCKED_ON_UPSTREAM marker (#3676)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coord import state
from coord.config import Config
from coord.dispatch import UPSTREAM_GAP_BRIEFING_NOTE
from coord.models import Machine, Repo
from coord.upstream_gaps import (
    UPSTREAM_GAP_SOURCE,
    UpstreamGap,
    _queue_upstream,
    final_message_from_log_text,
    gap_key,
    parse_upstream_markers,
    process_upstream_gaps,
)


class FakeTracker:
    """Stands in for the GitHub backend behind the issue-tracker seam
    (`coord.state.create_issue` -> `github_ops.create_issue`), so the test
    exercises the real seam and only the `gh` shell-out is faked."""

    def __init__(self, first_number: int = 501) -> None:
        self.next_number = first_number
        self.created: list[dict] = []
        self.comments: list[tuple[str, int, str]] = []

    def create_issue(self, repo, title, body, labels=None, milestone=None):
        number = self.next_number
        self.next_number += 1
        self.created.append({"repo": repo, "title": title, "body": body, "number": number})
        return {"number": number, "url": f"https://github.com/{repo}/issues/{number}"}

    def post_issue_comment(self, repo, issue_number, body, *args, **kwargs):
        self.comments.append((repo, issue_number, body))


@pytest.fixture
def tracker():
    fake = FakeTracker()
    with patch("coord.github_ops.create_issue", side_effect=fake.create_issue), \
            patch("coord.github_ops.post_issue_comment", side_effect=fake.post_issue_comment):
        yield fake


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[
            Repo(name="vimcode", github="acme/vimcode"),
            Repo(name="quadraui", github="acme/quadraui"),
        ],
        machines=[
            Machine(name="laptop", host="laptop.tailnet", repos=["vimcode", "quadraui"],
                    repo_paths={"vimcode": "/src/vimcode", "quadraui": "/src/quadraui"}),
        ],
    )


FINAL_MESSAGE = """\
Implemented the minimap shell; the scroll sync needs a quadraui API.

**BLOCKED_ON_UPSTREAM:** quadraui: Expose per-pane f32 scroll offset
The minimap needs `Pane::scroll_offset() -> f32`; today only the integer
top-line index is public. vimcode would call it from `minimap::sync`.

ISSUE_RESOLUTION: partial — blocked on upstream (BLOCKED_ON_UPSTREAM above)

SMOKE_TESTS:
- minimap — open a long file — the minimap renders
END_SMOKE_TESTS
"""


# ── parsing ──────────────────────────────────────────────────────────────────


class TestParse:
    def test_marker_with_body_stops_at_blank_line(self) -> None:
        gaps = parse_upstream_markers(FINAL_MESSAGE)
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.repo == "quadraui"
        assert gap.title == "Expose per-pane f32 scroll offset"
        assert "Pane::scroll_offset() -> f32" in gap.body
        assert "minimap::sync" in gap.body
        assert "ISSUE_RESOLUTION" not in gap.body
        assert "SMOKE_TESTS" not in gap.body

    def test_multiple_markers_and_markdown_decoration(self) -> None:
        text = (
            "- **BLOCKED_ON_UPSTREAM:** quadraui: Expose `scroll_offset()`\n"
            "  needs f32\n"
            "\n"
            "`BLOCKED_ON_UPSTREAM: acme/quadraui: Add codicon font`\n"
            "> BLOCKED_ON_UPSTREAM: vimcode: Quoted gap\n"
            "```\n"
            "BLOCKED_ON_UPSTREAM: quadraui: Fenced gap\n"
            "fenced body\n"
            "```\n"
        )
        gaps = parse_upstream_markers(text)
        assert [(g.repo, g.title) for g in gaps] == [
            ("quadraui", "Expose `scroll_offset()`"),
            ("acme/quadraui", "Add codicon font"),
            ("vimcode", "Quoted gap"),
            ("quadraui", "Fenced gap"),
        ]
        assert gaps[0].body == "needs f32"
        assert gaps[3].body == "fenced body"

    def test_same_gap_twice_collapses_to_one(self) -> None:
        text = (
            "BLOCKED_ON_UPSTREAM: quadraui: Expose `scroll_offset()`\n\n"
            "BLOCKED_ON_UPSTREAM: acme/quadraui: expose scroll_offset().\n"
            "the longer body wins\n"
        )
        gaps = parse_upstream_markers(text)
        assert len(gaps) == 1
        assert gaps[0].body == "the longer body wins"
        assert gap_key("quadraui", "Expose `scroll_offset()`") == gap_key(
            "acme/quadraui", "expose scroll_offset()."
        )

    @pytest.mark.parametrize("line", [
        "BLOCKED_ON_UPSTREAM: <repo>: <one-line upstream issue title>",
        "BLOCKED_ON_UPSTREAM: quadraui",            # no title
        "BLOCKED_ON_UPSTREAM: : title only",        # no repo
        "see the BLOCKED_ON_UPSTREAM: quadraui: x marker",  # not at line start
        "ISSUE_RESOLUTION: partial — blocked on upstream (BLOCKED_ON_UPSTREAM above)",
    ])
    def test_non_markers_are_ignored(self, line: str) -> None:
        assert parse_upstream_markers(line) == []

    def test_briefing_note_example_never_parses_as_a_gap(self) -> None:
        # A plain-text log that echoes the briefing must not file a fake gap.
        assert parse_upstream_markers(UPSTREAM_GAP_BRIEFING_NOTE) == []


class TestFinalMessage:
    def test_stream_json_uses_the_result_event(self) -> None:
        log = "\n".join(json.dumps(e) for e in [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "thinking: BLOCKED_ON_UPSTREAM: quadraui: Early idea"}]}},
            {"type": "result", "result": FINAL_MESSAGE},
        ])
        assert final_message_from_log_text(log) == FINAL_MESSAGE

    def test_stream_json_without_result_falls_back_to_last_turn(self) -> None:
        log = "\n".join(json.dumps(e) for e in [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "last"}]}},
        ])
        assert final_message_from_log_text(log) == "last"

    def test_plain_text_is_returned_whole(self) -> None:
        assert final_message_from_log_text("plain\ntext") == "plain\ntext"


# ── filing (acceptance: fake tracker + row blocked) ──────────────────────────


class TestProcessUpstreamGaps:
    def test_files_upstream_issue_and_blocks_the_queue_row(self, tracker, config) -> None:
        """#3676 acceptance: a final message carrying BLOCKED_ON_UPSTREAM files
        the upstream issue through the tracker seam and blocks the downstream
        drive-queue row `after=` it."""
        state.enqueue_drive_queue("vimcode", 42, machine="laptop", after=["vimcode#40"])

        outcomes = process_upstream_gaps(
            FINAL_MESSAGE, config=config, repo_name="vimcode", issue_number=42,
            assignment_id="aid-1",
        )

        assert len(outcomes) == 1
        out = outcomes[0]
        assert out.status == "filed"
        assert (out.upstream_repo, out.number) == ("quadraui", 501)
        assert out.queued_after is True

        # Filed upstream, in the upstream repo, with the worker's body and a
        # back-link to the downstream issue.
        assert len(tracker.created) == 1
        created = tracker.created[0]
        assert created["repo"] == "acme/quadraui"
        assert created["title"] == "Expose per-pane f32 scroll offset"
        assert "Pane::scroll_offset() -> f32" in created["body"]
        assert "Blocks acme/vimcode#42" in created["body"]

        # The downstream row now waits on it — existing edges and the
        # operator's machine pin survive.
        row = next(r for r in state.list_drive_queue("vimcode") if r["issue_number"] == 42)
        after = row["after_json"]
        if isinstance(after, str):
            after = json.loads(after)
        assert after == ["vimcode#40", "quadraui#501"]
        assert row["machine"] == "laptop"

        # Linked: pinned context entry on the downstream issue + a comment.
        ctx = [e for e in state.list_issue_context("vimcode", 42)
               if e["source"] == UPSTREAM_GAP_SOURCE]
        assert len(ctx) == 1 and ctx[0]["pinned"]
        assert "quadraui#501" in ctx[0]["body"]
        assert tracker.comments and tracker.comments[0][:2] == ("acme/vimcode", 42)
        assert "acme/quadraui#501" in tracker.comments[0][2]

        # The upstream issue lands in the cache as open, which is what makes
        # the drive queue read the new edge as "waiting on quadraui#501".
        assert state.get_cached_issue_state("quadraui", 501) == "open"

    def test_rereading_the_same_message_files_nothing_new(self, tracker, config) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        first = process_upstream_gaps(FINAL_MESSAGE, config=config,
                                      repo_name="vimcode", issue_number=42)
        second = process_upstream_gaps(FINAL_MESSAGE, config=config,
                                       repo_name="vimcode", issue_number=42)
        assert [o.status for o in first] == ["filed"]
        assert [o.status for o in second] == ["already-filed"]
        assert second[0].number == 501
        assert len(tracker.created) == 1
        assert len(tracker.comments) == 1
        row = next(r for r in state.list_drive_queue("vimcode") if r["issue_number"] == 42)
        after = row["after_json"] if isinstance(row["after_json"], list) else json.loads(row["after_json"])
        assert after == ["quadraui#501"]  # not appended twice

    def test_multiple_gaps_each_filed_once(self, tracker, config) -> None:
        text = (
            "BLOCKED_ON_UPSTREAM: quadraui: Gap one\nbody one\n\n"
            "BLOCKED_ON_UPSTREAM: acme/quadraui: Gap two\n\n"
            "BLOCKED_ON_UPSTREAM: quadraui: gap one.\n"
        )
        outcomes = process_upstream_gaps(text, config=config,
                                         repo_name="vimcode", issue_number=42)
        assert [o.status for o in outcomes] == ["filed", "filed"]
        assert [c["title"] for c in tracker.created] == ["Gap one", "Gap two"]

    def test_unknown_repo_is_not_filed(self, tracker, config) -> None:
        outcomes = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: nosuchrepo: Something\n", config=config,
            repo_name="vimcode", issue_number=42,
        )
        assert [o.status for o in outcomes] == ["unknown-repo"]
        assert tracker.created == []

    def test_unqueued_downstream_is_filed_and_linked_but_not_enqueued(self, tracker, config) -> None:
        outcomes = process_upstream_gaps(FINAL_MESSAGE, config=config,
                                         repo_name="vimcode", issue_number=42)
        assert outcomes[0].status == "filed"
        assert outcomes[0].queued_after is False
        # The DOWNSTREAM stays unqueued (it never was), but the upstream is
        # still enqueued as a root so the gap actually gets worked.
        assert state.list_drive_queue("vimcode") == []
        assert outcomes[0].upstream_queued is True
        assert _row(501, "quadraui")["issue_number"] == 501

    def test_auto_filed_upstream_is_queued_as_a_root(self, tracker, config) -> None:
        """The upstream issue is itself enqueued as a fresh drive-queue root
        (no pre-reqs), so it gets worked and the downstream row it blocks can
        relaunch — the gap vimcode#2056 hit, where quadraui#1771 was filed and
        chained but left `open, not queued`."""
        state.enqueue_drive_queue("vimcode", 42)
        out = process_upstream_gaps(FINAL_MESSAGE, config=config,
                                    repo_name="vimcode", issue_number=42)[0]
        assert out.status == "filed" and out.upstream_queued is True
        assert _after(_row(501, "quadraui")) == []  # a root — nothing blocks it

    def test_upstream_already_queued_is_left_as_is(self, tracker, config) -> None:
        """Idempotent: an upstream already in the queue is never re-enqueued
        (which would reset its machine/after) nor pulled out of flight."""
        state.enqueue_drive_queue("vimcode", 42)
        first = process_upstream_gaps(FINAL_MESSAGE, config=config,
                                      repo_name="vimcode", issue_number=42)[0]
        assert first.upstream_queued is True
        # Operator pins the upstream row to a machine; a replay must not reset it.
        state.enqueue_drive_queue("quadraui", 501, machine="laptop")
        second = process_upstream_gaps(FINAL_MESSAGE, config=config,
                                       repo_name="vimcode", issue_number=42)[0]
        assert second.status == "already-filed" and second.upstream_queued is False
        rows = [r for r in state.list_drive_queue("quadraui") if r["issue_number"] == 501]
        assert len(rows) == 1 and rows[0]["machine"] == "laptop"

    def test_no_marker_does_nothing(self, tracker, config) -> None:
        assert process_upstream_gaps("all done", config=config,
                                     repo_name="vimcode", issue_number=42) == []
        assert tracker.created == []


def _row(issue: int, repo: str = "vimcode") -> dict:
    return next(r for r in state.list_drive_queue(repo) if r["issue_number"] == issue)


def _after(row: dict) -> list[str]:
    after = row["after_json"]
    return after if isinstance(after, list) else json.loads(after)


class TestInFlightRow:
    """Review of #3676: the gap is read as the work leg ends, i.e. mid-drive,
    when the row is `running` — and the tick reads `after=` only for
    `waiting` rows. The row must be taken out of flight (drive stopped, row
    back to `waiting` with the edge), not just annotated."""

    @pytest.fixture
    def stopped(self):
        calls: list[tuple[str, int]] = []

        def fake_stop(repo, issue, **_kw):
            calls.append((repo, issue))
            return True, f"coord-drive-{repo}-{issue}", None

        with patch("coord.drive.stop_live_driver_session", side_effect=fake_stop):
            yield calls

    def _launch(self, issue: int = 42) -> None:
        state.update_drive_queue_entry(
            "vimcode", issue, state="running", attempts=1,
            session_name=f"coord-drive-vimcode-{issue}", launched_at=1000.0,
            launch_host="daemon", last_reason="launched",
        )

    def test_running_row_is_stopped_and_returned_to_waiting(
        self, tracker, config, stopped,
    ) -> None:
        from coord.drive_queue import STATE_WAITING, QueueEntry

        state.enqueue_drive_queue("vimcode", 41)
        state.enqueue_drive_queue("vimcode", 42, machine="laptop", after=["vimcode#40"],
                                  max_fix_rounds=3)
        state.enqueue_drive_queue("vimcode", 43)
        self._launch(42)
        position_before = _row(42)["position"]

        outcomes = process_upstream_gaps(
            FINAL_MESSAGE, config=config, repo_name="vimcode", issue_number=42,
            assignment_id="aid-1",
        )

        assert [o.status for o in outcomes] == ["filed"]
        assert outcomes[0].queued_after is True
        assert any("returned to waiting" in n for n in outcomes[0].notes)
        # The live driver was stopped through the dequeue seam (#3282) …
        assert stopped == [("vimcode", 42)]
        # … and the row is back in the walk, carrying the edge and every
        # operator-declared field, with a fresh run state. Its slot shifted
        # down by exactly one: the upstream was inserted at the FRONT of the
        # queue (a blocker is not sunk below the backlog), which renumbers the
        # global order but preserves the vimcode rows' order among themselves.
        row = _row(42)
        assert row["state"] == STATE_WAITING
        assert _after(row) == ["vimcode#40", "quadraui#501"]
        assert row["position"] == position_before + 1
        assert row["machine"] == "laptop"
        assert row["max_fix_rounds"] == 3
        assert not row["session_name"]
        assert int(row["attempts"] or 0) == 0
        # A `waiting` row is what the tick's walk feeds to `_resolve_prereqs`;
        # the upstream is now queued as a root at the front, so the row defers
        # on an in-flight dep ("waiting on quadraui#501 (queued, …)") rather
        # than on an unworked "open, not queued" one — the whole point of #3776.
        assert QueueEntry.from_row(row).state == STATE_WAITING
        assert _row(501, "quadraui")["position"] == 0
        assert state.get_cached_issue_state("quadraui", 501) == "open"
        # The vimcode rows keep their relative order.
        assert [r["issue_number"] for r in state.list_drive_queue("vimcode")] == [41, 42, 43]

    def test_replay_does_not_stop_a_relaunched_drive(
        self, tracker, config, stopped,
    ) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        self._launch(42)
        process_upstream_gaps(FINAL_MESSAGE, config=config,
                              repo_name="vimcode", issue_number=42)
        assert stopped == [("vimcode", 42)]
        # Upstream lands; the tick relaunches the row. A replayed transition
        # of the ORIGINAL work leg must leave the new drive alone.
        self._launch(42)
        again = process_upstream_gaps(FINAL_MESSAGE, config=config,
                                      repo_name="vimcode", issue_number=42)
        assert [o.status for o in again] == ["already-filed"]
        assert stopped == [("vimcode", 42)]
        assert _row(42)["state"] == "running"
        assert len(tracker.created) == 1

    def test_blocked_row_is_returned_to_waiting(self, tracker, config, stopped) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        state.update_drive_queue_entry("vimcode", 42, state="blocked",
                                       last_reason="DEAD END: nothing to do")
        process_upstream_gaps(FINAL_MESSAGE, config=config,
                              repo_name="vimcode", issue_number=42)
        row = _row(42)
        assert row["state"] == "waiting"
        assert _after(row) == ["quadraui#501"]

    def test_landed_row_is_left_alone(self, tracker, config, stopped) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        state.update_drive_queue_entry("vimcode", 42, state="done")
        outcomes = process_upstream_gaps(FINAL_MESSAGE, config=config,
                                         repo_name="vimcode", issue_number=42)
        assert outcomes[0].status == "filed"
        assert outcomes[0].queued_after is False
        assert stopped == []
        row = _row(42)
        assert row["state"] == "done"
        assert _after(row) == []

    def test_multiple_gaps_on_a_running_row_stop_it_once(
        self, tracker, config, stopped,
    ) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        self._launch(42)
        text = ("BLOCKED_ON_UPSTREAM: quadraui: Gap one\n\n"
                "BLOCKED_ON_UPSTREAM: quadraui: Gap two\n")
        process_upstream_gaps(text, config=config, repo_name="vimcode", issue_number=42)
        assert stopped == [("vimcode", 42)]
        row = _row(42)
        assert row["state"] == "waiting"
        assert _after(row) == ["quadraui#501", "quadraui#502"]


class TestQueueUpstream:
    """`_queue_upstream` guards (#3776 review): membership alone is not a safe
    idempotency signal — the row's live STATE and the issue's resolution decide
    whether to enqueue, leave as-is, or surface — and the enqueue goes to the
    FRONT, because the upstream is a blocker, not backlog."""

    def test_enqueued_at_the_front_not_the_tail(self, config) -> None:
        # A blocker must not sink below an existing backlog in its repo.
        state.enqueue_drive_queue("quadraui", 601)
        state.enqueue_drive_queue("quadraui", 602)
        enqueued, note = _queue_upstream("quadraui", 501, assume_open=True)
        assert enqueued is True and "front" in note
        assert [r["issue_number"] for r in state.list_drive_queue("quadraui")] == [501, 601, 602]
        assert _row(501, "quadraui")["position"] == 0

    def test_landed_row_is_not_re_queued(self, config) -> None:
        # A done/merged row is satisfied — re-driving burns a slot the tick
        # would only reconcile back to done.
        state.enqueue_drive_queue("quadraui", 501)
        state.update_drive_queue_entry("quadraui", 501, state="done")
        enqueued, note = _queue_upstream("quadraui", 501, assume_open=True)
        assert enqueued is False and "landed" in note
        assert _row(501, "quadraui")["state"] == "done"

    @pytest.mark.parametrize("stuck", ["blocked", "failed"])
    def test_terminally_stuck_row_is_surfaced_not_re_driven(self, config, stuck: str) -> None:
        # Re-driving a fix-round-ceiling failure on every gap pass would loop;
        # surface it instead. The row is left exactly as it is.
        state.enqueue_drive_queue("quadraui", 501, machine="laptop")
        state.update_drive_queue_entry("quadraui", 501, state=stuck,
                                       last_reason="fix-round ceiling")
        enqueued, note = _queue_upstream("quadraui", 501, assume_open=True)
        assert enqueued is False
        assert stuck in note and "not auto-re-driven" in note
        row = _row(501, "quadraui")
        assert row["state"] == stuck and row["machine"] == "laptop"

    def test_waiting_row_is_left_exactly_as_is(self, config) -> None:
        state.enqueue_drive_queue("quadraui", 501, machine="laptop")
        enqueued, note = _queue_upstream("quadraui", 501, assume_open=True)
        assert enqueued is False and "left as-is" in note
        assert _row(501, "quadraui")["machine"] == "laptop"

    def test_closed_issue_with_no_row_is_not_re_queued(self, config) -> None:
        # The #1771-heal replay path: the upstream was resolved out-of-band
        # (closed, or its done row removed) since the gap was first filed.
        with patch("coord.github_ops.issue_is_closed", return_value=True) as closed:
            enqueued, note = _queue_upstream("quadraui", 501, upstream_github="acme/quadraui")
        assert enqueued is False and "closed" in note
        assert state.list_drive_queue("quadraui") == []
        closed.assert_called_once_with("acme/quadraui", 501)

    def test_open_issue_with_no_row_is_enqueued(self, config) -> None:
        with patch("coord.github_ops.issue_is_closed", return_value=False):
            enqueued, _ = _queue_upstream("quadraui", 501, upstream_github="acme/quadraui")
        assert enqueued is True
        assert _row(501, "quadraui")["issue_number"] == 501

    def test_assume_open_skips_the_closed_check(self, config) -> None:
        # The just-filed path knows the issue is open (created moments ago) and
        # must not pay a gh round-trip — nor be fooled by a would-be closed read.
        with patch("coord.github_ops.issue_is_closed", return_value=True) as closed:
            enqueued, _ = _queue_upstream("quadraui", 501, assume_open=True)
        assert enqueued is True
        closed.assert_not_called()


class TestNotifyHook:
    """The coordinator half runs when a work-like leg's transition is posted."""

    def _transition(self):
        from coord.notify import Transition

        return Transition(assignment_id="aid-9", machine_name="laptop",
                          repo_name="vimcode", issue_number=42,
                          event="completion", exit_code=0)

    def _log(self, tmp_path: Path, final: str) -> Path:
        p = tmp_path / "worker.log"
        p.write_text("\n".join(json.dumps(e) for e in [
            {"type": "system", "subtype": "init", "session_id": "s"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": final}]}},
            {"type": "result", "result": final},
        ]) + "\n")
        return p

    def test_work_leg_final_message_files_and_blocks(self, tracker, config, tmp_path) -> None:
        from coord.notify import _capture_upstream_gaps

        state.enqueue_drive_queue("vimcode", 42)
        log_path = self._log(tmp_path, FINAL_MESSAGE)
        with patch("coord.config.load", return_value=config):
            _capture_upstream_gaps(self._transition(), {"log_path": str(log_path)},
                                   {"type": "work"})
            # A replayed transition must not file a duplicate.
            _capture_upstream_gaps(self._transition(), {"log_path": str(log_path)},
                                   {"type": "work"})
        assert len(tracker.created) == 1
        row = next(r for r in state.list_drive_queue("vimcode") if r["issue_number"] == 42)
        after = row["after_json"] if isinstance(row["after_json"], list) else json.loads(row["after_json"])
        assert after == ["quadraui#501"]

    def test_review_leg_is_ignored(self, tracker, config, tmp_path) -> None:
        from coord.notify import _capture_upstream_gaps

        log_path = self._log(tmp_path, FINAL_MESSAGE)
        with patch("coord.config.load", return_value=config):
            _capture_upstream_gaps(self._transition(), {"log_path": str(log_path)},
                                   {"type": "review"})
        assert tracker.created == []


class TestWorkerLogText:
    """Review of #3676: `_worker_log_text` must stay a thin wrapper around
    the shared `_fetch_raw_log_text_by_id` primitive for the remote-fetch
    branch — not a second, independently-hardcoded-port implementation of
    it — while keeping its own seek-based tail-read for the local-file
    branch, where it earns its keep against huge logs."""

    def _transition(self):
        from coord.notify import Transition

        return Transition(assignment_id="aid-9", machine_name="laptop",
                          repo_name="vimcode", issue_number=42,
                          event="completion", exit_code=0)

    def test_local_file_under_tail_threshold_returns_full_text(self, tmp_path) -> None:
        from coord.notify import _worker_log_text

        p = tmp_path / "worker.log"
        p.write_text("line one\nline two\n")
        text = _worker_log_text(self._transition(), {"log_path": str(p)})
        assert text == "line one\nline two\n"

    def test_local_file_over_tail_threshold_returns_only_the_tail(self, tmp_path) -> None:
        from coord.notify import _UPSTREAM_GAP_LOG_TAIL_BYTES, _worker_log_text

        p = tmp_path / "worker.log"
        filler = "x" * (_UPSTREAM_GAP_LOG_TAIL_BYTES + 1000)
        marker = "THE-ACTUAL-TAIL-CONTENT\n"
        p.write_text(filler + "\n" + marker)
        text = _worker_log_text(self._transition(), {"log_path": str(p)})
        assert marker in text
        assert "x" * 100 not in text  # none of the filler survived the seek
        assert len(text) < len(filler)

    def test_missing_log_path_falls_back_to_shared_remote_primitive(self) -> None:
        """No local log_path at all -> remote fetch must go through
        `_fetch_raw_log_text_by_id` (so it inherits AGENT_PORT, not a
        hardcoded literal)."""
        from coord.notify import _worker_log_text

        with (
            patch("coord.notify._agent_host", return_value="laptop.ts.net"),
            patch("coord.notify._fetch_raw_log_text_by_id",
                  return_value="remote log text") as fetch,
        ):
            text = _worker_log_text(self._transition(), {})
        assert text == "remote log text"
        fetch.assert_called_once_with("aid-9", None, "laptop.ts.net")

    def test_remote_fetch_uses_agent_port_constant_not_a_literal(self) -> None:
        """A change to AGENT_PORT must flow through to the remote-fetch URL
        this helper ultimately issues (via the shared primitive) — pinning
        down the exact split-brain bug this review round fixed."""
        from coord.notify import _worker_log_text

        class _Resp:
            text = "remote log text"

            def raise_for_status(self) -> None:
                return None

        captured_urls: list[str] = []

        def _fake_get(url, timeout=None):
            captured_urls.append(url)
            return _Resp()

        with (
            patch("coord.notify._agent_host", return_value="laptop.ts.net"),
            patch("coord.notify.AGENT_PORT", 19999),
            patch("httpx.get", side_effect=_fake_get),
        ):
            text = _worker_log_text(self._transition(), {})
        assert text == "remote log text"
        assert len(captured_urls) == 1
        assert ":19999/logs/aid-9" in captured_urls[0]
        assert ":7433" not in captured_urls[0]

    def test_unreadable_local_file_falls_back_to_remote(self, tmp_path) -> None:
        from coord.notify import _worker_log_text

        missing = tmp_path / "does-not-exist.log"
        with (
            patch("coord.notify._agent_host", return_value="laptop.ts.net"),
            patch("coord.notify._fetch_raw_log_text_by_id",
                  return_value="remote log text") as fetch,
        ):
            text = _worker_log_text(self._transition(), {"log_path": str(missing)})
        assert text == "remote log text"
        fetch.assert_called_once_with("aid-9", None, "laptop.ts.net")

    def test_no_host_resolves_to_empty_string(self) -> None:
        from coord.notify import _worker_log_text

        with patch("coord.notify._agent_host", return_value=None):
            text = _worker_log_text(self._transition(), {})
        assert text == ""


def test_upstream_gap_dataclass_key_matches_gap_key() -> None:
    gap = UpstreamGap(repo="quadraui", title="X")
    assert gap.key == gap_key("quadraui", "X")


def test_worker_system_prompt_agrees_with_the_briefing_marker_contract() -> None:
    """Review of #3676: the work-leg system prompt used to forbid exactly the
    `partial — blocked on upstream` form the briefing note prescribes (it
    demanded a real upstream issue number), so every work leg got
    conflicting instructions."""
    from coord.agent import WORKER_SYSTEM_PROMPT

    assert "drafted-but-unfiled" not in WORKER_SYSTEM_PROMPT
    assert "only a real GitHub issue number counts" not in WORKER_SYSTEM_PROMPT
    assert "BLOCKED_ON_UPSTREAM: <repo>: <title>" in WORKER_SYSTEM_PROMPT
    partial = "partial — blocked on upstream (BLOCKED_ON_UPSTREAM above)"
    assert partial in WORKER_SYSTEM_PROMPT
    assert partial in " ".join(UPSTREAM_GAP_BRIEFING_NOTE.split())
    # The prompt's own example of the marker never parses as a real gap.
    assert parse_upstream_markers(WORKER_SYSTEM_PROMPT) == []
