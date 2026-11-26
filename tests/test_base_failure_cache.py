"""Per-base-SHA pre-existing-failure cache (#4166)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from coord.base_failure_cache import (
    MAX_RECORD_AGE_S,
    PRE_EXISTING_NOTE_PREFIX,
    PriorFailure,
    cache_path,
    lookup,
    note_line,
    read_records,
    record_failure,
)


def _cache(tmp_path: Path) -> Path:
    return tmp_path / "state" / "base-failures.jsonl"


def test_second_branch_off_the_same_base_sees_the_first_branchs_failure(tmp_path) -> None:
    """The whole point: branch B failing exactly as branch A did, off the
    same base commit, is evidence the failure belongs to the base."""
    path = _cache(tmp_path)

    first = record_failure("quadraui", "a" * 40, "sig-1", branch="issue-1", path=path)
    assert first is None  # nothing prior to point at

    prior = record_failure("quadraui", "a" * 40, "sig-1", branch="issue-2", path=path)
    assert prior is not None
    assert prior.branch == "issue-1"
    assert prior.base_sha == "a" * 40


def test_a_branch_never_matches_its_own_earlier_run(tmp_path) -> None:
    """A retry of the same leg failing the same way says nothing about the
    base — it is the same branch twice."""
    path = _cache(tmp_path)

    record_failure("quadraui", "a" * 40, "sig-1", branch="issue-1", path=path)
    again = record_failure("quadraui", "a" * 40, "sig-1", branch="issue-1", path=path)

    assert again is None


def test_a_different_base_sha_is_not_a_hit(tmp_path) -> None:
    path = _cache(tmp_path)

    record_failure("quadraui", "a" * 40, "sig-1", branch="issue-1", path=path)
    other_base = record_failure("quadraui", "b" * 40, "sig-1", branch="issue-2", path=path)

    assert other_base is None


def test_a_different_signature_or_repo_is_not_a_hit(tmp_path) -> None:
    path = _cache(tmp_path)

    record_failure("quadraui", "a" * 40, "sig-1", branch="issue-1", path=path)

    assert lookup("quadraui", "a" * 40, "sig-2", branch="issue-2", path=path) is None
    assert lookup("vimcode", "a" * 40, "sig-1", branch="issue-2", path=path) is None


def test_an_expired_record_can_never_produce_a_hit(tmp_path) -> None:
    """A fingerprint from last month says nothing about today's base."""
    path = _cache(tmp_path)
    stale = time.time() - MAX_RECORD_AGE_S - 60

    record_failure(
        "quadraui", "a" * 40, "sig-1", branch="issue-1", path=path, now=stale,
    )

    assert read_records(path=path) == []
    assert lookup("quadraui", "a" * 40, "sig-1", branch="issue-2", path=path) is None


def test_an_empty_signature_is_never_recorded_and_never_matches(tmp_path) -> None:
    """`failure_signature` returns "" for an empty evidence record — a
    caller must never key anything off "no evidence at all"."""
    path = _cache(tmp_path)

    assert record_failure("quadraui", "a" * 40, "", branch="issue-1", path=path) is None
    assert not path.exists()
    assert lookup("quadraui", "a" * 40, "", branch="issue-2", path=path) is None


def test_a_shredded_line_does_not_make_the_cache_unreadable(tmp_path) -> None:
    path = _cache(tmp_path)
    record_failure("quadraui", "a" * 40, "sig-1", branch="issue-1", path=path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"repo": "quadraui", "base_sha"\n')  # power loss mid-append
        handle.write("not json at all\n")

    record_failure("quadraui", "a" * 40, "sig-1", branch="issue-3", path=path)

    assert lookup("quadraui", "a" * 40, "sig-1", branch="issue-2", path=path) is not None


def test_record_failure_never_raises_when_the_cache_is_unwritable(tmp_path) -> None:
    """A recorder in an agent's reap path must cost the measurement, never
    the leg."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")

    assert record_failure(
        "quadraui", "a" * 40, "sig-1", branch="issue-1",
        path=blocker / "nested" / "base-failures.jsonl",
    ) is None


def test_note_line_is_bounded_and_carries_the_contract_prefix() -> None:
    """The note is the ONE thing that reaches a fix briefing from here, so
    it may never grow with the size of the failure it describes (#4166
    forbids re-injecting echoed commands or log blobs)."""
    prior = PriorFailure(
        repo="quadraui",
        base_sha="0123456789abcdef" * 2 + "01234567",
        signature="sig-1",
        branch="issue-1-some-fairly-long-branch-name",
        at=time.time(),
    )

    line = note_line(prior)

    assert line.startswith(PRE_EXISTING_NOTE_PREFIX)
    assert "\n" not in line
    assert len(line) < 400
    assert "0123456789ab" in line  # short SHA, not the full 40 chars
    assert prior.base_sha not in line


def test_records_round_trip_through_the_jsonl_file(tmp_path) -> None:
    path = _cache(tmp_path)

    record_failure("quadraui", "a" * 40, "sig-1", branch="issue-1", path=path)

    raw = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert raw[0]["repo"] == "quadraui"
    assert raw[0]["signature"] == "sig-1"
    # Nothing else: no log text, no command, no verdict.
    assert set(raw[0]) == {"repo", "base_sha", "signature", "branch", "at"}

    records = read_records(path=path)
    assert [r.branch for r in records] == ["issue-1"]


def test_cache_path_honours_the_env_override_and_the_state_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("COORD_BASE_FAILURE_CACHE", raising=False)
    assert cache_path(tmp_path / "state") == tmp_path / "state" / "base-failures.jsonl"

    monkeypatch.setenv("COORD_BASE_FAILURE_CACHE", str(tmp_path / "elsewhere.jsonl"))
    assert cache_path(tmp_path / "state") == tmp_path / "elsewhere.jsonl"
