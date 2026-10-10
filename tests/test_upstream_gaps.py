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
        assert state.list_drive_queue("vimcode") == []

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
        # … and the row is back in the walk, at its old slot, carrying the
        # edge and every operator-declared field, with a fresh run state.
        row = _row(42)
        assert row["state"] == STATE_WAITING
        assert _after(row) == ["vimcode#40", "quadraui#501"]
        assert row["position"] == position_before
        assert row["machine"] == "laptop"
        assert row["max_fix_rounds"] == 3
        assert not row["session_name"]
        assert int(row["attempts"] or 0) == 0
        # A `waiting` row is what the tick's walk feeds to `_resolve_prereqs`;
        # the upstream issue is queued nowhere and open in the cache, so it
        # defers "waiting on quadraui#501 (open, not queued)".
        assert QueueEntry.from_row(row).state == STATE_WAITING
        assert state.get_cached_issue_state("quadraui", 501) == "open"
        # The other queue rows were untouched.
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
