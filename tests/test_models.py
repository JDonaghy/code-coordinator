"""Tests for coord.models — Board state operations."""

from __future__ import annotations

import pytest

from coord.models import Assignment, Board, Machine, Repo


def _board() -> Board:
    return Board(
        repos=[
            Repo(name="api", github="acme/api"),
            Repo(name="shared", github="acme/shared"),
        ],
        machines=[
            Machine(name="laptop", host="laptop.tailnet", repos=["api", "shared"]),
            Machine(name="server", host="server.tailnet", repos=["api"]),
        ],
    )


def test_idle_machines_all_idle() -> None:
    b = _board()
    assert [m.name for m in b.idle_machines()] == ["laptop", "server"]


def test_idle_machines_one_busy() -> None:
    b = _board()
    b.active.append(
        Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="x",
            status="running",
        )
    )
    assert [m.name for m in b.idle_machines()] == ["server"]


def test_mark_done_moves_assignment_to_completed() -> None:
    b = _board()
    b.active.append(
        Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="x",
            status="running",
        )
    )
    done = b.mark_done("laptop", branch="feat/x", pr_url="http://pr/1")
    assert done is not None
    assert done.status == "done"
    assert done.branch == "feat/x"
    assert b.active == []
    assert b.completed[0].issue_number == 42


def test_mark_failed_moves_assignment_to_completed() -> None:
    b = _board()
    b.active.append(
        Assignment(
            machine_name="server",
            repo_name="api",
            issue_number=7,
            issue_title="y",
            status="running",
        )
    )
    failed = b.mark_failed("server")
    assert failed is not None
    assert failed.status == "failed"
    assert b.active == []
    assert b.completed[0].status == "failed"


def test_active_files_by_repo_groups_by_repo() -> None:
    b = _board()
    b.active.extend(
        [
            Assignment(
                machine_name="laptop",
                repo_name="api",
                issue_number=1,
                issue_title="x",
                files_allowed=["a.py"],
                status="running",
            ),
            Assignment(
                machine_name="server",
                repo_name="api",
                issue_number=2,
                issue_title="y",
                files_allowed=["b.py"],
                status="running",
            ),
        ]
    )
    files = b.active_files_by_repo()
    assert set(files["api"]) == {"a.py", "b.py"}


def test_machine_can_work_on() -> None:
    m = Machine(name="laptop", host="h", repos=["api"])
    assert m.can_work_on("api")
    assert not m.can_work_on("other")


# ── #685/#2024: the per-issue Test-stage policy label ────────────────────────


def test_test_mode_from_labels_reads_both_policies():
    from coord.models import test_mode_from_labels

    assert test_mode_from_labels(["enhancement", "test-mode:smoke"]) == "smoke"
    assert test_mode_from_labels(["test-mode:auto"]) == "auto"


def test_test_mode_from_labels_prefers_auto_when_both_are_present():
    """Preserves `coord.state._get_issue_test_mode_local`'s original ordering
    exactly — this function was hoisted out of it (#2024) so the dispatcher's
    reading and the driver's reading cannot drift, which only holds if the
    hoist changed no behaviour."""
    from coord.models import test_mode_from_labels

    assert test_mode_from_labels(["test-mode:smoke", "test-mode:auto"]) == "auto"


@pytest.mark.parametrize("labels", [None, [], ["enhancement"], 7, "test-mode:smoke"])
def test_test_mode_from_labels_fails_open(labels):
    """Anything that isn't an explicit policy label reads as "no policy set" —
    never as a policy nobody asked for. A bare string is deliberately included:
    `"test-mode:smoke" in "test-mode:smoke"` is True for a substring check, and
    a policy inferred from a stray string would silently switch the headless
    Test stage off for an issue."""
    from coord.models import test_mode_from_labels

    assert test_mode_from_labels(labels) is None


# ── #2234: the policy-refusal park marker ────────────────────────────────────


def test_is_policy_refusal_reason_matches_the_marker():
    from coord.models import POLICY_REFUSAL_MARKER, is_policy_refusal_reason

    reason = (
        f"drive exited for api#1 (exit_code=1): refused on a standing "
        f"repo-rule prohibition. {POLICY_REFUSAL_MARKER}"
    )
    assert is_policy_refusal_reason(reason) is True


@pytest.mark.parametrize("text", [None, "", "drive session died", "a Gate A refusal"])
def test_is_policy_refusal_reason_fails_open_on_anything_else(text):
    from coord.models import is_policy_refusal_reason

    assert is_policy_refusal_reason(text) is False


# ── #2966: coordinator_owned_docs — "only the coordinator writes docs" ──────


def test_coordinator_owned_docs_defaults_to_claude_md_when_unconfigured():
    """coordinator_only_files was set by zero repos fleet-wide — the fleet
    default must not depend on it."""
    from coord.models import coordinator_owned_docs

    repo = Repo(name="api", github="acme/api")
    assert coordinator_owned_docs(repo) == ["CLAUDE.md"]


def test_coordinator_owned_docs_unions_configured_files_after_default():
    from coord.models import coordinator_owned_docs

    repo = Repo(
        name="api", github="acme/api",
        coordinator_only_files=["README.md", "CLAUDE.md", "CHANGELOG.md"],
    )
    # Dedupe: CLAUDE.md is already in the default, so it must not repeat.
    assert coordinator_owned_docs(repo) == ["CLAUDE.md", "README.md", "CHANGELOG.md"]


def test_coordinator_owned_docs_handles_none_repo():
    from coord.models import coordinator_owned_docs

    assert coordinator_owned_docs(None) == ["CLAUDE.md"]


def test_coordinator_owned_docs_fails_open_on_repo_stand_in_missing_attribute():
    """#1388-style stand-in: a Repo-shaped object predating this field must
    not raise AttributeError — same fail-open discipline as develop_branch."""
    from coord.models import coordinator_owned_docs

    class _StandInRepo:
        name = "api"
        github = "acme/api"

    assert coordinator_owned_docs(_StandInRepo()) == ["CLAUDE.md"]
