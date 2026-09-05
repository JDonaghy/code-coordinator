"""Tests for coord.models — Board state operations."""

from __future__ import annotations

import pytest

from coord.models import (
    CLOSES_ISSUE_TYPES,
    EPIC_DECOMPOSE_TYPE,
    SEALED_PATH_AUTHOR_TYPES,
    WORK_LIKE_TYPES,
    Assignment,
    Board,
    Machine,
    Repo,
    trust_issue_closed_for,
)


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


# ── Repo.uat_preview / uat_live_preview (#2687, #2948) ──────────────────────
#
# #2948: `{pr_branch_slug}` — a substitution meant to reconstruct a
# Cloudflare Pages branch-alias subdomain from the branch name — was removed.
# Confirmed live (2026-08-29, against JDonaghy/natal-chart) that Cloudflare
# Pages publishes NO branch aliases at all (`main` itself 404s), so no
# function of the branch name alone could ever have produced a working URL.
# `uat_preview` is now an optional override template for a repo whose preview
# host genuinely IS templatable; `uat_live_preview` opts a repo into the
# primary resolution path instead — the live GitHub-Deployment lookup in
# `coord.merge_queue.evaluate_uat_verdict` / `coord.github_ops.
# get_pr_deployment_url` (outside this module's scope; not re-tested here).


def test_uat_live_preview_defaults_false() -> None:
    repo = Repo(name="api", github="acme/api")
    assert repo.uat_live_preview is False


def test_uat_preview_default_none_and_resolve_returns_none() -> None:
    repo = Repo(name="api", github="acme/api")
    assert repo.uat_preview is None
    assert repo.resolve_uat_preview_url(branch="issue-1-x") is None


def test_resolve_uat_preview_url_substitutes_supported_variables() -> None:
    repo = Repo(
        name="api", github="acme/api",
        uat_preview="https://preview/{repo}/{issue_number}/{pr_number}/{branch}",
    )
    url = repo.resolve_uat_preview_url(branch="b1", issue_number=42, pr_number=7)
    assert url == "https://preview/api/42/7/b1"


def test_resolve_uat_preview_url_pr_branch_slug_is_no_longer_special() -> None:
    """#2948: `{pr_branch_slug}` is just an unknown placeholder now — left
    verbatim, exactly like a typo'd variable name, never silently rendering
    a plausible-but-dead Cloudflare Pages URL."""
    repo = Repo(
        name="natal-chart", github="acme/natal-chart",
        uat_preview="https://{pr_branch_slug}.natal-chart-3ew.pages.dev/",
    )
    url = repo.resolve_uat_preview_url(branch="issue-42-fix-chart-colors")
    assert url == "https://{pr_branch_slug}.natal-chart-3ew.pages.dev/"


def test_resolve_uat_preview_url_unknown_placeholder_left_verbatim() -> None:
    repo = Repo(
        name="api", github="acme/api",
        uat_preview="https://{typo_field}.example.pages.dev/",
    )
    assert repo.resolve_uat_preview_url(branch="b1") == (
        "https://{typo_field}.example.pages.dev/"
    )


def test_resolve_uat_preview_url_malformed_format_spec_falls_back_to_raw() -> None:
    # `str.format_map` can still raise on a stray "{}" that `__missing__`
    # can't intercept — never crash the merge gate over a coordinator.yml typo.
    repo = Repo(name="api", github="acme/api", uat_preview="https://example/{}/")
    assert repo.resolve_uat_preview_url(branch="b1") == "https://example/{}/"


# ── #3132: epic-decompose type membership ────────────────────────────────────


def test_epic_decompose_is_work_like() -> None:
    """It must flow through the normal Work → Test → Review → Merge pipeline
    like any other work-like type — the whole point of #3132 is that its
    first-slice PR gets reviewed and merged normally."""
    assert EPIC_DECOMPOSE_TYPE in WORK_LIKE_TYPES


def test_epic_decompose_never_closes_its_issue() -> None:
    """The entire point of the type: its `issue_number` IS the epic, and
    merging its PR must never auto-close it (that's `CLOSES_ISSUE_TYPES`'
    job, and epic-decompose must stay out of it — see #1077/#1314)."""
    assert EPIC_DECOMPOSE_TYPE not in CLOSES_ISSUE_TYPES


def test_epic_decompose_is_not_a_sealed_path_author() -> None:
    """Unlike mock-author/test-author, epic-decompose writes ordinary code
    (the epic's first slice) plus files new issues — it does NOT author
    under `tests/acceptance/`, so the oracle-tamper inversion rule in
    coord.review must not apply to it."""
    assert EPIC_DECOMPOSE_TYPE not in SEALED_PATH_AUTHOR_TYPES


@pytest.mark.parametrize(
    ("assignment_type", "expected"),
    [
        ("work", True),
        ("mock-author", False),
        ("test-author", False),
        (EPIC_DECOMPOSE_TYPE, False),
        ("conflict-fix", True),
        ("review", True),
        ("smoke", True),
        (None, True),
    ],
)
def test_trust_issue_closed_for(assignment_type: str | None, expected: bool) -> None:
    """#2639/#3132: only CLOSES_ISSUE_TYPES members get their `issue_number`
    trusted as their own deliverable. `epic-decompose`'s `issue_number` is
    the epic itself — never closed by its own merge — so it must read
    exactly like mock-author/test-author here (False), even though it isn't
    in SEALED_PATH_AUTHOR_TYPES. Every non-epic-decompose case pins down
    pre-#3132 behaviour is unchanged."""
    assert trust_issue_closed_for(assignment_type) is expected
