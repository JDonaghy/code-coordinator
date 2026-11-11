"""Tests for coord.upstream_gaps — the BLOCKED_ON_UPSTREAM marker (#3676)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from coord import state
from coord.board_doctor import STRANDED_PREREQ_OPT_OUT_LABEL
from coord.config import Config
from coord.dispatch import UPSTREAM_GAP_BRIEFING_NOTE
from coord.models import Machine, Repo, UPSTREAM_GAP_PARK_MARKER, is_upstream_gap_park_reason
from coord.upstream_gaps import (
    UPSTREAM_GAP_SOURCE,
    UpstreamGap,
    _DEDUP_RECENT_CLOSE_DAYS,
    _named_symbols,
    _park_queue_row,
    _queue_upstream,
    _verify_symbols,
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
        self.created.append({
            "repo": repo, "title": title, "body": body, "number": number,
            "labels": list(labels or []),
        })
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
            # A SECOND upstream, distinct from quadraui — exercises that
            # #3977's per-downstream-issue dedup is scoped to "the same
            # upstream target", not "this downstream issue may only ever
            # have ONE blocked-on-upstream gap, full stop" (two genuinely
            # different asks to two different repos must each still file).
            Repo(name="widgets", github="acme/widgets"),
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
        # Two gaps for DIFFERENT upstream repos — #3977's per-downstream-issue
        # dedup is scoped to "the same upstream target", so both still file.
        text = (
            "BLOCKED_ON_UPSTREAM: quadraui: Gap one\nbody one\n\n"
            "BLOCKED_ON_UPSTREAM: acme/widgets: Gap two\n\n"
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

    def test_unqueued_downstream_is_filed_and_linked_but_not_enqueued(
        self, tracker, verify_config: Config,
    ) -> None:
        # `verify_config`: FINAL_MESSAGE's `minimap::sync` is confirmed
        # MISSING against the fixture's real checkout -- a verified-real
        # gap, auto-queued same as always (#3977 only withholds auto-queue
        # from an UNVERIFIED filing; see TestUnverifiedNotQueued below).
        outcomes = process_upstream_gaps(FINAL_MESSAGE, config=verify_config,
                                         repo_name="vimcode", issue_number=42)
        assert outcomes[0].status == "filed"
        assert outcomes[0].queued_after is False
        # The DOWNSTREAM stays unqueued (it never was), but the upstream is
        # still enqueued as a root so the gap actually gets worked.
        assert state.list_drive_queue("vimcode") == []
        assert outcomes[0].upstream_queued is True
        assert _row(501, "quadraui")["issue_number"] == 501

    def test_auto_filed_upstream_is_queued_as_a_root(
        self, tracker, verify_config: Config,
    ) -> None:
        """The upstream issue is itself enqueued as a fresh drive-queue root
        (no pre-reqs), so it gets worked and the downstream row it blocks can
        relaunch — the gap vimcode#2056 hit, where quadraui#1771 was filed and
        chained but left `open, not queued`."""
        state.enqueue_drive_queue("vimcode", 42)
        out = process_upstream_gaps(FINAL_MESSAGE, config=verify_config,
                                    repo_name="vimcode", issue_number=42)[0]
        assert out.status == "filed" and out.upstream_queued is True
        assert _after(_row(501, "quadraui")) == []  # a root — nothing blocks it

    def test_upstream_already_queued_is_left_as_is(
        self, tracker, verify_config: Config,
    ) -> None:
        """Idempotent: an upstream already in the queue is never re-enqueued
        (which would reset its machine/after) nor pulled out of flight."""
        state.enqueue_drive_queue("vimcode", 42)
        first = process_upstream_gaps(FINAL_MESSAGE, config=verify_config,
                                      repo_name="vimcode", issue_number=42)[0]
        assert first.upstream_queued is True
        # Operator pins the upstream row to a machine; a replay must not reset it.
        state.enqueue_drive_queue("quadraui", 501, machine="laptop")
        second = process_upstream_gaps(FINAL_MESSAGE, config=verify_config,
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


# ── dedup per downstream issue (#3977) ──────────────────────────────────────


class TestDedupPerDownstreamIssue:
    """A worker that rewords the same bug files it under a fresh gap_key
    every time — the exact shape that chained vimcode#2114 into three
    duplicate quadraui issues (#2325/#2326/#2328). Dedup must key on the
    DOWNSTREAM issue + upstream repo, not the title."""

    def test_second_title_for_an_open_upstream_blocks_instead_of_filing(
        self, tracker, config,
    ) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        first = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Gap one\nbody one\n",
            config=config, repo_name="vimcode", issue_number=42,
        )[0]
        assert first.status == "filed" and first.number == 501

        # `issue_is_closed` patched too (even though this specific gap is
        # unverified and so never reaches it): hermetic regardless of which
        # branch `_reassert_block_and_queue` takes.
        with patch("coord.github_ops.get_issue_state", return_value="open"), \
                patch("coord.github_ops.issue_is_closed", return_value=False):
            second = process_upstream_gaps(
                "BLOCKED_ON_UPSTREAM: quadraui: Gap two, worded completely differently\n"
                "body two, with more specifics\n",
                config=config, repo_name="vimcode", issue_number=42,
            )[0]

        assert second.status == "deduped"
        assert second.number == 501
        assert len(tracker.created) == 1  # no second issue filed
        row = _row(42)
        assert _after(row) == ["quadraui#501"]
        # The second marker's own report is not silently discarded.
        assert tracker.comments[-1][:2] == ("acme/vimcode", 42)
        assert "body two, with more specifics" in tracker.comments[-1][2]
        ctx = [e for e in state.list_issue_context("vimcode", 42)
               if e["source"] == UPSTREAM_GAP_SOURCE]
        assert any("quadraui#501" in e["body"] and "still open" in e["body"] for e in ctx)

        # A replay of the SAME second title reasserts the block without a
        # fresh `get_issue_state` round-trip — `issue_is_closed`/
        # `get_issue_state` are unpatched here, so a call to either would
        # raise `AttributeError` (no live `gh`), proving neither ran.
        third = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Gap two, worded completely differently\n"
            "body two, with more specifics\n",
            config=config, repo_name="vimcode", issue_number=42,
        )[0]
        assert third.status == "deduped" and third.number == 501
        assert len(tracker.created) == 1

    def test_open_dup_still_auto_queues_a_verified_second_gap(
        self, tracker, verify_config,
    ) -> None:
        """The dedup-open hold only ever withholds auto-queue for an
        UNVERIFIED filing — a verified one still gets its upstream issue
        queued as a root exactly like a fresh filing would."""
        state.enqueue_drive_queue("vimcode", 42)
        first = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Add FormSpacing::padding\n"
            "needs `RowAlign` (present) and `FormSpacing::padding` (missing).\n",
            config=verify_config, repo_name="vimcode", issue_number=42,
        )[0]
        assert first.status == "filed" and first.upstream_queued is True

        with patch("coord.github_ops.get_issue_state", return_value="open"):
            second = process_upstream_gaps(
                "BLOCKED_ON_UPSTREAM: quadraui: Also needs FormSpacing::padding\n"
                "worded completely differently\n",
                config=verify_config, repo_name="vimcode", issue_number=42,
            )[0]

        assert second.status == "deduped"
        # Already queued by the first call — left as-is, not re-enqueued —
        # but critically an ATTEMPT was made (unlike the unverified case,
        # which skips `_queue_upstream` outright): no "unverified" note.
        assert second.upstream_queued is False
        assert not any("unverified" in n for n in second.notes)
        assert any("already" in n for n in second.notes)

    def test_second_title_after_recent_close_parks_instead_of_refiling(
        self, tracker, config,
    ) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        first = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Gap one\nbody one\n",
            config=config, repo_name="vimcode", issue_number=42,
        )[0]
        assert first.number == 501

        with patch("coord.github_ops.get_issue_state", return_value="closed"):
            second = process_upstream_gaps(
                "BLOCKED_ON_UPSTREAM: quadraui: Gap two, worded completely differently\n"
                "body two\n",
                config=config, repo_name="vimcode", issue_number=42,
            )[0]

        assert second.status == "deduped"
        assert second.parked is True
        assert len(tracker.created) == 1  # no second issue filed
        row = _row(42)
        assert row["state"] == "parked"
        assert is_upstream_gap_park_reason(row["last_reason"])
        assert tracker.comments[-1][:2] == ("acme/vimcode", 42)
        assert "quadraui#501" in tracker.comments[-1][2]

        # A replay of this SAME second title must report "deduped" again
        # (it never ran a real verification check for THIS title), not
        # "not-a-gap" — reusing `_NOT_A_GAP_MARK` for this outcome would say
        # "checked against upstream and confirmed not real", which is false.
        again = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Gap two, worded completely differently\n"
            "body two\n",
            config=config, repo_name="vimcode", issue_number=42,
        )[0]
        assert again.status == "deduped"
        assert again.detail != "already checked against upstream and confirmed not real — not re-checked"
        assert len(tracker.comments) == 2  # not reposted a third time
        assert len(tracker.created) == 1

    def test_stale_closed_dup_falls_through_to_a_fresh_filing(
        self, tracker, config,
    ) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        first = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Gap one\nbody one\n",
            config=config, repo_name="vimcode", issue_number=42,
        )[0]
        assert first.number == 501

        # Backdate the original filing's ledger entry past the dedup window.
        old_ts = time.time() - (_DEDUP_RECENT_CLOSE_DAYS + 1) * 86400
        conn = state.get_connection()
        conn.execute(
            "UPDATE issue_context SET created_at = ? WHERE repo_name = ? AND issue_number = ?",
            (old_ts, "vimcode", 42),
        )
        conn.commit()

        with patch("coord.github_ops.get_issue_state", return_value="closed"):
            second = process_upstream_gaps(
                "BLOCKED_ON_UPSTREAM: quadraui: Gap two, worded completely differently\n"
                "body two\n",
                config=config, repo_name="vimcode", issue_number=42,
            )[0]

        assert second.status == "filed"
        assert second.number == 502
        assert len(tracker.created) == 2

    def test_missing_dedup_timestamp_treats_as_recent_not_stale(self) -> None:
        """A dedup entry with no recorded `created_at` (the DB column is
        `NOT NULL`, but a daemon-routed response dict is not schema-checked
        the same way) must fail toward NOT re-filing — treated as
        just-closed, never as ancient — the opposite of what reading a
        missing timestamp as epoch 0 would compute."""
        from coord.upstream_gaps import _dedup_entry_age_days

        assert _dedup_entry_age_days({}) == 0.0
        assert _dedup_entry_age_days({"created_at": None}) == 0.0
        assert _dedup_entry_age_days({"created_at": 0}) == 0.0
        recent_age = _dedup_entry_age_days({"created_at": time.time() - 3600})
        assert 0 < recent_age < 1


# ── gap verification (#3930) ──────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def quadraui_checkout(tmp_path: Path) -> Path:
    """A real local checkout of a `main` branch that already has `RowAlign`
    on disk but no `FormSpacing::padding` — the fixture upstream #3930's
    acceptance means by "the fixture upstream's default branch"."""
    seed = tmp_path / "quadraui-seed"
    origin = tmp_path / "quadraui-origin.git"
    checkout = tmp_path / "quadraui-checkout"

    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "layout.rs").write_text("pub enum RowAlign { Start, Center, End }\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")

    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)],
                   check=True, capture_output=True, text=True)
    return checkout


@pytest.fixture
def verify_config(quadraui_checkout: Path) -> Config:
    return Config(
        repos=[
            Repo(name="vimcode", github="acme/vimcode"),
            Repo(name="quadraui", github="acme/quadraui"),
        ],
        machines=[
            Machine(name="laptop", host="laptop.tailnet", repos=["vimcode", "quadraui"],
                    repo_paths={"quadraui": str(quadraui_checkout)}),
        ],
    )


@pytest.fixture
def quadraui_fieldkind_checkout(tmp_path: Path) -> Path:
    """A local checkout whose default branch already has `FieldKind::
    NumberInput`/`FieldKind::ComboBox` — the vimcode#2114 incident's own
    shape: both variants exist on quadraui's `develop`, just not in the
    0.9.0 release vimcode pins."""
    seed = tmp_path / "fieldkind-seed"
    origin = tmp_path / "fieldkind-origin.git"
    checkout = tmp_path / "fieldkind-checkout"

    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "primitives").mkdir()
    (seed / "primitives" / "form.rs").write_text(
        "pub enum FieldKind {\n"
        "    NumberInput,\n"
        "    RadioGroup,\n"
        "    ComboBox(ComboBox),\n"
        "}\n"
        "\n"
        "// A `git grep -F` matches a literal substring regardless of\n"
        "// context, same as the real quadraui#2325/#2326/#2328 incident's\n"
        "// own enum-path usages (FieldKind::NumberInput, FieldKind::RadioGroup,\n"
        "// FieldKind::ComboBox) would have been found at.\n"
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")

    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)],
                   check=True, capture_output=True, text=True)
    return checkout


@pytest.fixture
def fieldkind_config(quadraui_fieldkind_checkout: Path) -> Config:
    return Config(
        repos=[
            Repo(name="vimcode", github="JDonaghy/vimcode"),
            Repo(name="quadraui", github="JDonaghy/quadraui"),
        ],
        machines=[
            Machine(name="laptop", host="laptop.tailnet", repos=["vimcode", "quadraui"],
                    repo_paths={"quadraui": str(quadraui_fieldkind_checkout)}),
        ],
    )


class TestReplayVimcode2114:
    """Replays the vimcode#2114 incident's own shape (three
    `BLOCKED_ON_UPSTREAM` markers, three separate worker legs, all for
    `FieldKind` variants already on quadraui's default branch) through the
    real pipeline — the issue's own acceptance bullet, not a synthetic
    `Gap one`/`Gap two` stand-in."""

    def test_replaying_the_three_markers_files_nothing(
        self, tracker, fieldkind_config,
    ) -> None:
        state.enqueue_drive_queue("vimcode", 2114)

        # quadraui#2325's own shape: a path-prefixed span.
        leg_one = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Add FieldKind::NumberInput\n"
            "vimcode's form widget needs "
            "`primitives/form.rs::FieldKind::NumberInput`.\n",
            config=fieldkind_config, repo_name="vimcode", issue_number=2114,
            assignment_id="59d117bdd0c9",
        )
        # quadraui#2326's own shape: a differently-worded title.
        leg_two = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Add FieldKind::RadioGroup\n"
            "vimcode's form widget needs `FieldKind::RadioGroup`.\n",
            config=fieldkind_config, repo_name="vimcode", issue_number=2114,
            assignment_id="ff3fb9f139a6",
        )
        # quadraui#2328's own shape: both variants, worded a third way,
        # plus the tuple-arg-decorated span.
        leg_three = process_upstream_gaps(
            "BLOCKED_ON_UPSTREAM: quadraui: Expose the missing FieldKind variants\n"
            "vimcode's form widget still needs `FieldKind::NumberInput` and "
            "`FieldKind::ComboBox(ComboBox)`.\n",
            config=fieldkind_config, repo_name="vimcode", issue_number=2114,
            assignment_id="66eee603d85d",
        )

        assert tracker.created == []  # nothing ever filed
        for outcomes in (leg_one, leg_two, leg_three):
            assert [o.status for o in outcomes] == ["not-a-gap"]
            assert outcomes[0].parked is True
        row = _row(2114)
        assert row["state"] == "parked"
        assert is_upstream_gap_park_reason(row["last_reason"])
        assert "unreleased" in row["last_reason"]


class TestNamedSymbols:
    def test_backtick_identifiers_are_symbols(self) -> None:
        gap = UpstreamGap(repo="quadraui", title="Expose `RowAlign`",
                           body="also needs `FormSpacing::padding` and `foo()`.")
        assert _named_symbols(gap) == ["RowAlign", "FormSpacing::padding", "foo()"]

    def test_prose_and_commands_are_not_symbols(self) -> None:
        gap = UpstreamGap(repo="quadraui", title="Expose the scroll offset",
                           body="run `cargo build` then check the offset is f32.")
        assert _named_symbols(gap) == []

    def test_no_backticks_yields_no_symbols(self) -> None:
        gap = UpstreamGap(repo="quadraui", title="Expose per-pane scroll offset",
                           body="today only the integer line index is public.")
        assert _named_symbols(gap) == []

    def test_duplicates_collapse_to_first_seen(self) -> None:
        gap = UpstreamGap(repo="quadraui", title="`RowAlign`", body="`RowAlign` again")
        assert _named_symbols(gap) == ["RowAlign"]

    def test_path_prefixed_span_strips_to_the_symbol(self) -> None:
        """#3977 (quadraui#2325): a worker names the file the symbol lives
        in, not a bare identifier."""
        gap = UpstreamGap(repo="quadraui", title="Add FieldKind::NumberInput",
                           body="needs `primitives/form.rs::FieldKind`.")
        assert _named_symbols(gap) == ["FieldKind"]

    def test_tuple_arg_span_strips_to_the_symbol(self) -> None:
        """#3977 (quadraui#2326): a worker names the constructor with its own
        arg, not the bare variant."""
        gap = UpstreamGap(repo="quadraui", title="Add FieldKind::ComboBox",
                           body="needs `FieldKind::ComboBox(ComboBox)`.")
        assert _named_symbols(gap) == ["FieldKind::ComboBox"]

    def test_path_prefix_and_tuple_arg_combine(self) -> None:
        gap = UpstreamGap(repo="quadraui", title="x",
                           body="`primitives/form.rs::FieldKind::ComboBox(ComboBox)`")
        assert _named_symbols(gap) == ["FieldKind::ComboBox"]

    def test_bare_empty_call_is_kept_literal(self) -> None:
        """A bare no-arg call (`foo()`) is left exactly as written — only a
        NON-empty trailing arg list is stripped as decoration."""
        gap = UpstreamGap(repo="quadraui", title="x", body="`foo()`")
        assert _named_symbols(gap) == ["foo()"]

    def test_real_identifier_with_a_dot_is_not_mistaken_for_a_path(self) -> None:
        # No leading "::" at all -- nothing for the path-prefix strip to do.
        gap = UpstreamGap(repo="quadraui", title="x", body="`Pane.scroll_offset`")
        assert _named_symbols(gap) == ["Pane.scroll_offset"]


class TestVerifySymbols:
    """Direct coverage of the mechanical fetch+grep check — #3930 wants it
    cheap, real, and honest about when it couldn't run at all."""

    def test_existing_symbol_is_found_with_a_location(
        self, verify_config: Config,
    ) -> None:
        quadraui = verify_config.repo("quadraui")
        result = _verify_symbols(quadraui, verify_config, ["RowAlign"])
        assert result.checked is True
        assert result.missing == ()
        assert "layout.rs" in result.found_at["RowAlign"]

    def test_missing_symbol_is_reported_missing(self, verify_config: Config) -> None:
        quadraui = verify_config.repo("quadraui")
        result = _verify_symbols(quadraui, verify_config, ["FormSpacing::padding"])
        assert result.checked is True
        assert result.missing == ("FormSpacing::padding",)

    def test_mixed_symbols_report_each_correctly(self, verify_config: Config) -> None:
        quadraui = verify_config.repo("quadraui")
        result = _verify_symbols(quadraui, verify_config, ["RowAlign", "FormSpacing::padding"])
        assert result.checked is True
        assert result.missing == ("FormSpacing::padding",)
        assert "RowAlign" in result.found_at

    def test_no_local_checkout_is_unchecked_not_missing(self, config: Config) -> None:
        # `config` (module fixture) points quadraui at a path that doesn't
        # exist on this machine — the check must say so, not claim either a
        # confirmation or a refutation it never actually performed.
        quadraui = config.repo("quadraui")
        result = _verify_symbols(quadraui, config, ["RowAlign"])
        assert result.checked is False
        assert result.missing == ()
        assert "no local checkout" in result.detail


class TestVerifyGapFiling:
    """#3930 acceptance: filing consults the upstream repo before it ever
    files or blocks anything."""

    def test_every_symbol_present_files_nothing_and_adds_no_after_edge(
        self, tracker, verify_config: Config,
    ) -> None:
        """#3977: found on the upstream default branch means UNRELEASED, not
        missing — nothing filed, no `after=` edge, and the downstream row is
        parked (not left to loop) rather than merely commented on."""
        state.enqueue_drive_queue("vimcode", 42, after=["vimcode#40"])
        msg = (
            "BLOCKED_ON_UPSTREAM: quadraui: Expose RowAlign\n"
            "vimcode's layout needs `RowAlign`.\n"
        )

        outcomes = process_upstream_gaps(msg, config=verify_config,
                                         repo_name="vimcode", issue_number=42)

        assert [o.status for o in outcomes] == ["not-a-gap"]
        assert outcomes[0].parked is True
        assert tracker.created == []  # nothing filed upstream
        row = _row(42)
        assert _after(row) == ["vimcode#40"]  # no edge added
        assert row["state"] == "parked"
        assert "unreleased" in (row["last_reason"] or "")
        # The parked walk (`coord/drive_queue.py`) must never auto-resume
        # this on its own — no release-qualified `after=` edge exists yet,
        # so nothing on the board signals when one lands.
        assert is_upstream_gap_park_reason(row["last_reason"])
        # The next leg reads where the symbol was found, not just that it
        # wasn't filed.
        assert tracker.comments and tracker.comments[0][:2] == ("acme/vimcode", 42)
        comment = tracker.comments[0][2]
        assert "RowAlign" in comment and "layout.rs" in comment
        assert "unreleased" in comment
        # A ledger entry IS recorded (so a replay never re-greps or re-posts
        # the same finding) — but it is not a "filed" entry: a later real
        # gap on this issue must still be able to file normally.
        ctx = [e for e in state.list_issue_context("vimcode", 42)
               if e["source"] == UPSTREAM_GAP_SOURCE]
        assert len(ctx) == 1 and ctx[0]["pinned"]
        assert "RowAlign" in ctx[0]["body"]

        # Replaying the same message re-checks nothing and posts nothing new,
        # but re-asserts the park (idempotent).
        again = process_upstream_gaps(msg, config=verify_config,
                                      repo_name="vimcode", issue_number=42)
        assert [o.status for o in again] == ["not-a-gap"]
        assert again[0].parked is True
        assert len(tracker.comments) == 1
        assert len(state.list_issue_context("vimcode", 42)) == 1
        assert _row(42)["state"] == "parked"

    def test_path_prefixed_symbol_is_stripped_and_checked(
        self, tracker, verify_config: Config,
    ) -> None:
        """#3977: a marker naming `primitives/form.rs::RowAlign` — the exact
        path-prefixed shape quadraui#2325 wrote — is still checked against
        upstream, not silently skipped as unverifiable."""
        msg = (
            "BLOCKED_ON_UPSTREAM: quadraui: Add RowAlign\n"
            "vimcode's layout needs `layout.rs::RowAlign`.\n"
        )

        outcomes = process_upstream_gaps(msg, config=verify_config,
                                         repo_name="vimcode", issue_number=42)

        assert [o.status for o in outcomes] == ["not-a-gap"]
        assert tracker.created == []

    def test_tuple_arg_symbol_is_stripped_and_checked(
        self, tracker, verify_config: Config,
    ) -> None:
        """#3977: a marker naming `RowAlign(Start)` — the tuple-arg-decorated
        shape quadraui#2326 wrote — is still checked against upstream."""
        msg = (
            "BLOCKED_ON_UPSTREAM: quadraui: Add RowAlign\n"
            "vimcode's layout needs `RowAlign(Start)`.\n"
        )

        outcomes = process_upstream_gaps(msg, config=verify_config,
                                         repo_name="vimcode", issue_number=42)

        assert [o.status for o in outcomes] == ["not-a-gap"]
        assert tracker.created == []

    def test_one_missing_symbol_files_as_today(
        self, tracker, verify_config: Config,
    ) -> None:
        msg = (
            "BLOCKED_ON_UPSTREAM: quadraui: Add FormSpacing::padding\n"
            "vimcode needs `RowAlign` (already there) and `FormSpacing::padding` "
            "(missing).\n"
        )

        outcomes = process_upstream_gaps(msg, config=verify_config,
                                         repo_name="vimcode", issue_number=42)

        assert [o.status for o in outcomes] == ["filed"]
        assert len(tracker.created) == 1
        body = tracker.created[0]["body"]
        assert "Not verified" not in body  # a real check ran and confirmed it
        # A VERIFIED filing never carries the stranded-prereq opt-out label
        # — that's reserved for an unverified one.
        assert tracker.created[0]["labels"] == []
        # Unqueued downstream: nothing to hold, but the upstream is still
        # auto-filed exactly as an unverified gap would be.
        assert outcomes[0].number == 501

    def test_no_checkable_symbols_files_with_a_note(
        self, tracker, verify_config: Config,
    ) -> None:
        state.enqueue_drive_queue("vimcode", 42)
        msg = (
            "BLOCKED_ON_UPSTREAM: quadraui: Expose per-pane scroll offset\n"
            "vimcode's minimap needs the f32 scroll offset of a pane; today "
            "only the integer line index is public.\n"
        )

        outcomes = process_upstream_gaps(msg, config=verify_config,
                                         repo_name="vimcode", issue_number=42)

        assert [o.status for o in outcomes] == ["filed"]
        created = tracker.created[0]
        assert "Not verified against upstream before filing" in created["body"]
        assert "no checkable symbols" in created["body"]
        # #3977: unverified -- never auto-queued as a drive-queue root, so no
        # worker is spent on a gap taken on the reporting worker's word alone
        # — but the dependency is real either way, so the downstream row
        # still blocks on it.
        assert outcomes[0].upstream_queued is False
        assert outcomes[0].queued_after is True
        assert state.list_drive_queue("quadraui") == []
        assert _after(_row(42)) == ["quadraui#501"]
        # Opts the filed issue itself out of the stranded-prereq sweep too.
        assert created["labels"] == [STRANDED_PREREQ_OPT_OUT_LABEL]

        # A replay (same message twice) must not auto-queue it either —
        # the hold is read back off the ledger entry, not re-derived.
        again = process_upstream_gaps(msg, config=verify_config,
                                      repo_name="vimcode", issue_number=42)
        assert again[0].status == "already-filed"
        assert again[0].upstream_queued is False
        assert state.list_drive_queue("quadraui") == []

    def test_unverifiable_check_still_files_with_a_note(
        self, tracker, config: Config,
    ) -> None:
        """`config` (module fixture) has no real local checkout for
        quadraui, so the check itself cannot run — that must never be read
        as a refutation, only as "not verified"."""
        state.enqueue_drive_queue("vimcode", 42)
        msg = (
            "BLOCKED_ON_UPSTREAM: quadraui: Expose RowAlign\n"
            "vimcode's layout needs `RowAlign`.\n"
        )

        outcomes = process_upstream_gaps(msg, config=config,
                                         repo_name="vimcode", issue_number=42)

        assert [o.status for o in outcomes] == ["filed"]
        created = tracker.created[0]
        assert "Not verified against upstream before filing" in created["body"]
        assert "no local checkout" in created["body"]
        # #3977: a tool failure must never silently swallow a real gap (it's
        # still filed, and the downstream row still blocks on it) but must
        # also never spend a worker on an unconfirmed one — not auto-queued.
        assert outcomes[0].upstream_queued is False
        assert outcomes[0].queued_after is True
        assert state.list_drive_queue("quadraui") == []
        assert created["labels"] == [STRANDED_PREREQ_OPT_OUT_LABEL]


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
        self, tracker, verify_config, stopped,
    ) -> None:
        from coord.drive_queue import STATE_WAITING, QueueEntry

        state.enqueue_drive_queue("vimcode", 41)
        state.enqueue_drive_queue("vimcode", 42, machine="laptop", after=["vimcode#40"],
                                  max_fix_rounds=3)
        state.enqueue_drive_queue("vimcode", 43)
        self._launch(42)
        position_before = _row(42)["position"]

        # `verify_config`: FINAL_MESSAGE's `minimap::sync` is confirmed
        # missing, a verified-real gap -- auto-queued as a root same as
        # always (#3977 only withholds that from an unverified filing).
        outcomes = process_upstream_gaps(
            FINAL_MESSAGE, config=verify_config, repo_name="vimcode", issue_number=42,
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
        # Two gaps for DIFFERENT upstream repos — #3977's per-downstream-issue
        # dedup is scoped to "the same upstream target", so both still file
        # and both block the row.
        text = ("BLOCKED_ON_UPSTREAM: quadraui: Gap one\n\n"
                "BLOCKED_ON_UPSTREAM: widgets: Gap two\n")
        process_upstream_gaps(text, config=config, repo_name="vimcode", issue_number=42)
        assert stopped == [("vimcode", 42)]
        row = _row(42)
        assert row["state"] == "waiting"
        assert _after(row) == ["quadraui#501", "widgets#502"]

    def test_running_row_is_stopped_and_parked_for_an_unreleased_symbol(
        self, tracker, verify_config, stopped,
    ) -> None:
        """`_park_queue_row`'s `running` branch (#3977's own park path, not
        `_block_queue_row`'s) — the module docstring says a row is usually
        `running` when a gap is read, so this must be covered too, not just
        a plain `waiting` row."""
        state.enqueue_drive_queue("vimcode", 42, machine="laptop", max_fix_rounds=3)
        self._launch(42)
        msg = (
            "BLOCKED_ON_UPSTREAM: quadraui: Expose RowAlign\n"
            "vimcode's layout needs `RowAlign`.\n"
        )

        outcomes = process_upstream_gaps(msg, config=verify_config,
                                         repo_name="vimcode", issue_number=42)

        assert [o.status for o in outcomes] == ["not-a-gap"]
        assert outcomes[0].parked is True
        assert stopped == [("vimcode", 42)]
        row = _row(42)
        assert row["state"] == "parked"
        assert row["machine"] == "laptop"
        assert row["max_fix_rounds"] == 3
        assert is_upstream_gap_park_reason(row["last_reason"])

    def test_park_restores_the_row_if_the_re_enqueue_fails(
        self, tracker, verify_config, stopped,
    ) -> None:
        """Mirrors `_block_queue_row`'s own restore guard (review of
        #3676): a `running` row taken out of flight must not be left
        dropped from the queue if putting it back fails."""
        state.enqueue_drive_queue("vimcode", 42, machine="laptop")
        self._launch(42)

        calls = {"n": 0}
        real_enqueue = state.enqueue_drive_queue

        def flaky_enqueue(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated enqueue failure")
            return real_enqueue(*args, **kwargs)

        with patch("coord.state.enqueue_drive_queue", side_effect=flaky_enqueue):
            with pytest.raises(RuntimeError, match="simulated enqueue failure"):
                _park_queue_row("vimcode", 42, "blocked on upstream quadraui release: x")

        # Restored, not dropped from the queue — the retry inside the
        # except block put it back (as a fresh `waiting` row, same as
        # `_block_queue_row`'s identical restore guard: the row was already
        # taken out of flight by the preceding dequeue, so "restored" means
        # "still in the queue", not "still running").
        row = _row(42)
        assert row["state"] == "waiting"
        assert row["machine"] == "laptop"


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
