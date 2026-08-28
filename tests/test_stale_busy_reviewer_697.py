"""#697 — zombie `running` rows must stop inflating reviewer selection's busy set.

The issue's live 2026-06-23 board carried `running` rows aged 4.4h, 7.9h, 51h
and 70h whose workers were long dead (empty logs; the owning agent answered
404 on `/cancel`).  Reaping those rows is owned elsewhere (#2275's no-record
arm, #1396's tmux reaper, #2536's fleet sweep); this file covers the OTHER
half of #697's acceptance — that `pick_reviewer_machine` /
`_ranked_reviewer_candidates` no longer treat such a row as evidence that its
machine is occupied.
"""

from __future__ import annotations

import time

import pytest

from coord.config import Config, ReviewsConfig
from coord.models import Assignment, Board, Machine, Repo
from coord.review import (
    STALE_BUSY_INTERACTIVE_SECONDS,
    _ranked_reviewer_candidates,
    busy_machine_names,
    pick_reviewer_machine,
)


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", depends_on=[], default_branch="main")


@pytest.fixture
def two_machine_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server", host="server.tail",
                capabilities=["python", "gtk"], repos=["api"],
                repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


def _row(
    *,
    machine: str = "server",
    age_seconds: float,
    status: str = "running",
    type_: str = "work",
    provider_name: str | None = None,
) -> Assignment:
    return Assignment(
        machine_name=machine,
        repo_name="api",
        issue_number=669,
        issue_title="busy work",
        assignment_id=f"zombie-{type_}-{int(age_seconds)}",
        status=status,
        type=type_,
        provider_name=provider_name,
        dispatched_at=time.time() - age_seconds,
    )


# ── busy_machine_names ──────────────────────────────────────────────────────


def test_fresh_running_row_still_counts_as_busy(two_machine_config: Config) -> None:
    board = Board(active=[_row(age_seconds=60.0)])
    assert busy_machine_names(board, two_machine_config) == {"server"}


def test_zombie_work_row_no_longer_counts_as_busy(two_machine_config: Config) -> None:
    # 4.4h — the shortest zombie in #697's table. `work`'s threshold is 45m,
    # plus a 1h buffer, so this is far past the horizon.
    board = Board(active=[_row(age_seconds=4.4 * 3600.0)])
    assert busy_machine_names(board, two_machine_config) == set()


def test_row_just_under_the_horizon_still_counts_as_busy(
    two_machine_config: Config,
) -> None:
    # 45m threshold + 1h buffer = 1h45m; 1h30m must NOT be presumed dead.
    board = Board(active=[_row(age_seconds=90 * 60.0)])
    assert busy_machine_names(board, two_machine_config) == {"server"}


def test_row_without_dispatched_at_is_never_presumed_dead(
    two_machine_config: Config,
) -> None:
    """No `dispatched_at` → nothing to compute an age from → never guess."""
    board = Board(active=[Assignment(
        machine_name="server", repo_name="api", issue_number=99,
        issue_title="busy work", status="running", assignment_id="no-time",
    )])
    assert busy_machine_names(board, two_machine_config) == {"server"}


def test_pending_zombie_row_is_also_dropped(two_machine_config: Config) -> None:
    board = Board(active=[_row(age_seconds=8 * 3600.0, status="pending")])
    assert busy_machine_names(board, two_machine_config) == set()


def test_terminal_rows_never_counted(two_machine_config: Config) -> None:
    board = Board(active=[_row(age_seconds=60.0, status="done")])
    assert busy_machine_names(board, two_machine_config) == set()


# ── Attended (inf-threshold) rows ───────────────────────────────────────────


def test_attended_chat_row_still_busy_within_a_day(two_machine_config: Config) -> None:
    """A human really attending a chat session for 6h is normal, not a zombie."""
    board = Board(active=[_row(
        age_seconds=6 * 3600.0, type_="chat", provider_name="claude-pty",
    )])
    assert busy_machine_names(board, two_machine_config) == {"server"}


def test_51_hour_chat_zombie_is_dropped(two_machine_config: Config) -> None:
    """#697's `dc2257047c6f`: a `chat` row `running` for 51 hours."""
    board = Board(active=[_row(
        age_seconds=51 * 3600.0, type_="chat", provider_name="claude-pty",
    )])
    assert 51 * 3600.0 > STALE_BUSY_INTERACTIVE_SECONDS
    assert busy_machine_names(board, two_machine_config) == set()


# ── Reviewer selection ──────────────────────────────────────────────────────


def test_zombie_row_no_longer_demotes_its_machine_to_the_busy_tier(
    two_machine_config: Config,
) -> None:
    """The #697 dispatch bug, end to end through the public entry point.

    With the zombie counted as busy, `server` fell to fallback 1 ("currently
    busy, review will queue") — on the real fleet that pushed selection onto
    the only other "free" candidate, which was offline, and the review could
    not be dispatched at all.
    """
    board = Board(active=[_row(age_seconds=7.9 * 3600.0)])
    choice = pick_reviewer_machine("laptop", "api", board, two_machine_config)
    assert choice is not None
    assert choice.machine.name == "server"
    assert choice.same_as_worker is False
    assert "currently busy" not in choice.rationale
    assert "different machine from worker" in choice.rationale


def test_live_row_still_lands_in_the_busy_tier(two_machine_config: Config) -> None:
    board = Board(active=[_row(age_seconds=120.0)])
    choice = pick_reviewer_machine("laptop", "api", board, two_machine_config)
    assert choice is not None
    assert choice.machine.name == "server"
    assert "currently busy" in choice.rationale


def test_ranked_candidates_promote_the_zombie_machine_above_a_live_one(
    repo: Repo,
) -> None:
    """Tier 1 (different + idle) must be decided on *believable* rows only."""
    config = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/work/api"}),
            Machine(name="busy-box", host="busy.tail", repos=["api"],
                    repo_paths={"api": "/srv/api"}),
            Machine(name="zombie-box", host="zombie.tail", repos=["api"],
                    repo_paths={"api": "/srv/api"}),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )
    board = Board(active=[
        _row(machine="busy-box", age_seconds=120.0),
        _row(machine="zombie-box", age_seconds=70 * 3600.0),
    ])
    ranked = [m.name for m, _ in _ranked_reviewer_candidates(
        "laptop", "api", board, config,
    )]
    # zombie-box is idle-tier, busy-box is busy-tier, laptop is last resort.
    assert ranked == ["zombie-box", "busy-box", "laptop"]
