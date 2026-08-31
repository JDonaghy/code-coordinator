"""Tests for adversarial code review dispatch (coord/review.py)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from coord.config import (
    Config,
    PipelineConfig,
    ProviderDef,
    ProvidersConfig,
    ReviewsConfig,
    load,
)
from coord.merge_queue import QueuedMerge
from coord.models import Assignment, Board, Machine, Repo
from coord.review import (
    REVIEWER_SYSTEM_PROMPT,
    ReviewFindings,
    build_review_briefing,
    build_scoped_review_briefing,
    compute_resolution_delta,
    dispatch_pending_reviews,
    dispatch_review,
    dispatch_scoped_review,
    dispatch_scoped_reviews_for_queue,
    parse_review_from_log,
    pick_reviewer_machine,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


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
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


@pytest.fixture
def one_machine_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/work/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


def _completed_assignment(machine: str = "laptop", branch: str = "issue-1-fix") -> Assignment:
    return Assignment(
        machine_name=machine,
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        briefing="Worker briefing",
        assignment_id="abc123",
        status="done",
        branch=branch,
        dispatched_at=0.0,
        finished_at=1.0,
        type="work",
    )


# ── Config parsing ──────────────────────────────────────────────────────────


def test_reviews_config_defaults_to_enabled(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.reviews.enabled is True   # enabled by default; set enabled: false to opt out
    assert cfg.reviews.auto_dispatch is True
    assert cfg.reviews.checklist == ["Check for platform-specific code in shared/cross-platform paths"]
    assert cfg.reviews.repo_overrides == {}


def test_reviews_config_can_be_disabled(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  enabled: false\n"
    )
    cfg = load(p)
    assert cfg.reviews.enabled is False


def test_reviews_config_parses_all_fields(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tail
    repos: [api]
reviews:
  enabled: true
  auto_dispatch: false
  require_approval: true
  reviewer_prompt: |
    Focus on correctness.
  checklist:
    - "Did tests get added?"
    - "Stay in scope?"
  repo_overrides:
    api:
      - "Check no SQL injection."
"""
    )
    cfg = load(p)
    assert cfg.reviews.enabled is True
    assert cfg.reviews.auto_dispatch is False
    assert cfg.reviews.require_approval is True
    assert "Focus on correctness." in cfg.reviews.reviewer_prompt
    assert cfg.reviews.checklist == ["Did tests get added?", "Stay in scope?"]
    assert cfg.reviews.repo_overrides == {"api": ["Check no SQL injection."]}


def test_reviews_config_rejects_unknown_repo_override(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tail
    repos: [api]
reviews:
  enabled: true
  repo_overrides:
    ghost:
      - "this repo does not exist"
"""
    )
    from coord.config import ConfigError
    with pytest.raises(ConfigError, match="unknown repo: 'ghost'"):
        load(p)


def test_reviews_config_provider_defaults_to_none(tmp_path: Path) -> None:
    """#1811 regression: no ``reviews.provider`` in coordinator.yml must leave
    ``cfg.reviews.provider`` as ``None`` — the review dispatch path then
    inherits ``repo.provider`` exactly as it did before this field existed.
    No existing deployment (none of which set this brand-new key) may see
    ANY behavior change."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.reviews.provider is None


def test_reviews_config_parses_provider(tmp_path: Path) -> None:
    """A ``reviews.provider`` naming a provider actually registered under
    ``providers.definitions`` parses cleanly and is recorded verbatim."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        """\
repos:
  - name: api
    github: acme/api
    provider: opencode
machines:
  - name: laptop
    host: laptop.tail
    repos: [api]
providers:
  definitions:
    opencode:
      type: opencode
reviews:
  provider: claude
"""
    )
    cfg = load(p)
    assert cfg.reviews.provider == "claude"
    assert cfg.repos[0].provider == "opencode"


def test_reviews_config_rejects_unknown_provider(tmp_path: Path) -> None:
    """#1811: an unknown ``reviews.provider`` name must be a config error at
    PARSE time — not a silent dispatch-time fallback (mirrors how #1796
    treats an unresolvable named provider as a hard refusal rather than a
    quiet default)."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  provider: nonexistent-provider\n"
    )
    from coord.config import ConfigError
    with pytest.raises(ConfigError, match="unknown provider: 'nonexistent-provider'"):
        load(p)


# ── Machine selection ───────────────────────────────────────────────────────


def test_pick_reviewer_prefers_different_machine(two_machine_config: Config) -> None:
    board = Board()
    choice = pick_reviewer_machine("laptop", "api", board, two_machine_config)
    assert choice is not None
    assert choice.machine.name == "server"
    assert choice.same_as_worker is False


def test_pick_reviewer_falls_back_to_same_machine_when_only_one(
    one_machine_config: Config,
) -> None:
    board = Board()
    choice = pick_reviewer_machine("laptop", "api", board, one_machine_config)
    assert choice is not None
    assert choice.machine.name == "laptop"
    assert choice.same_as_worker is True
    assert "fresh but not on separate hardware" in choice.rationale


def test_pick_reviewer_returns_none_when_no_machine_handles_repo(
    repo: Repo,
) -> None:
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["other"], repo_paths={}),
        ],
        reviews=ReviewsConfig(enabled=True),
    )
    board = Board()
    assert pick_reviewer_machine("laptop", "api", board, cfg) is None


def test_pick_reviewer_picks_busy_different_machine_over_same_idle(
    two_machine_config: Config,
) -> None:
    # Both machines handle api; server is busy. We still prefer server (the
    # different machine) — independence outweighs queuing delay.
    board = Board(
        active=[
            Assignment(
                machine_name="server", repo_name="api", issue_number=99,
                issue_title="busy work", status="running",
                assignment_id="other",
            )
        ]
    )
    choice = pick_reviewer_machine("laptop", "api", board, two_machine_config)
    assert choice is not None
    assert choice.machine.name == "server"
    assert "currently busy" in choice.rationale


# ── Briefing construction ───────────────────────────────────────────────────


def test_briefing_includes_claude_md_and_checklist() -> None:
    cfg = ReviewsConfig(
        enabled=True,
        checklist=["Did tests get added?", "Any security issues?"],
        repo_overrides={"api": ["No SQL injection."]},
    )
    briefing = build_review_briefing(
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="Login is broken on Firefox.",
        branch="issue-7-fix-login",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=cfg,
        repo_claude_md="# CLAUDE.md\nDo not use raw SQL.",
    )
    assert "acme/api PR #42" in briefing
    assert "Fix login" in briefing
    assert "Login is broken on Firefox." in briefing
    assert "Do not use raw SQL." in briefing
    assert "Did tests get added?" in briefing
    assert "Any security issues?" in briefing
    assert "No SQL injection." in briefing
    # Reviewer must output structured verdict; coordinator posts the PR review.
    assert "REVIEW_VERDICT:" in briefing
    assert "gh pr review" not in briefing
    # No same-machine warning when the reviewer is on a different machine.
    assert "running on the same machine as the worker" not in briefing


def test_briefing_fetches_claude_md_by_sha_instead_of_embedding() -> None:
    """#2818: when a review_head_sha is available, the briefing points the
    reviewer at `git show <sha>:CLAUDE.md` instead of embedding the full
    text — killing the unclamped second copy that used to sit in every
    review briefing's immutable prefix.
    """
    big_claude_md = "# CLAUDE.md\n" + ("Do not use raw SQL.\n" * 1000)
    briefing = build_review_briefing(
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="Login is broken on Firefox.",
        branch="issue-7-fix-login",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True),
        repo_claude_md=big_claude_md,
        review_head_sha="deadbeef1234",
    )
    assert "git show deadbeef1234:CLAUDE.md" in briefing
    # The full text must NOT be embedded — that's the whole point of #2818.
    assert "Do not use raw SQL." not in briefing
    assert len(briefing) < len(big_claude_md)


def test_briefing_falls_back_to_clamped_embed_without_sha() -> None:
    """#2818 fallback: when no review_head_sha is available (SHA fetch
    failed), the briefing still embeds CLAUDE.md directly — clamped to
    MAX_CLAUDE_MD_CHARS instead of the old unbounded embed.
    """
    from coord.refine_chat import MAX_CLAUDE_MD_CHARS

    big_claude_md = "# CLAUDE.md\n" + ("Do not use raw SQL.\n" * 1000)
    assert len(big_claude_md) > MAX_CLAUDE_MD_CHARS

    briefing = build_review_briefing(
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="Login is broken on Firefox.",
        branch="issue-7-fix-login",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True),
        repo_claude_md=big_claude_md,
        review_head_sha=None,
    )
    assert "git show" not in briefing
    assert "…[truncated]" in briefing
    assert "Do not use raw SQL." in briefing  # head of the file survives


def test_briefing_warns_when_same_machine() -> None:
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="Fix login", issue_body="",
        branch="issue-7", worker_machine="laptop", same_as_worker=True,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    assert "running on the same machine as the worker" in briefing


def test_briefing_warns_when_provider_same_as_worker() -> None:
    """#1811: when the resolved review provider matches the worker's own,
    the reviewer's own prompt must say so — mirroring the same_as_worker
    machine-co-location note. Provider co-location is a LARGER loss of
    independence than machine co-location (a fresh session removes shared
    context, but not shared blind spots)."""
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="Fix login", issue_body="",
        branch="issue-7", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        provider_same_as_worker=True, review_provider="opencode",
    )
    assert "same provider" in briefing
    assert "opencode" in briefing
    assert "shared model family" in briefing
    # Machine co-location note must stay independent of the provider one.
    assert "running on the same machine as the worker" not in briefing


def test_briefing_no_provider_note_when_providers_differ() -> None:
    """Sanity companion to the above: no co-location note when the reviewer's
    provider is NOT the same as the worker's."""
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="Fix login", issue_body="",
        branch="issue-7", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        provider_same_as_worker=False, review_provider="claude",
    )
    assert "same provider" not in briefing


def test_briefing_requires_the_three_finding_sections() -> None:
    """#1456: the #476 gate now fails closed, so an advisory-only
    request-changes is only recognisable from an explicitly-empty blocking
    section.  Both the briefing and REVIEWER_SYSTEM_PROMPT must ask for the
    headings — without them the gate is unreachable and every advisory review
    costs a full fix+re-review round."""
    from coord.review import REVIEWER_SYSTEM_PROMPT

    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="Fix login", issue_body="",
        branch="issue-7", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    for text in (briefing, REVIEWER_SYSTEM_PROMPT):
        assert "## Blocking findings" in text
        assert "## Non-blocking concerns" in text
        assert "## Nits" in text
    assert "None." in briefing


def test_briefing_instructs_report_result_before_the_printed_block() -> None:
    """#1457: `build_review_briefing`'s own tail (shared by headless and
    interactive dispatch alike) must also ask for `coord report-result`
    ahead of the printed REVIEW_VERDICT block, gated on `$COORD_ASSIGNMENT_ID`
    so a headless reviewer — which never learns its own assignment id before
    dispatch — safely skips it and falls straight through to the always-
    required printed block."""
    from coord.review import REVIEWER_SYSTEM_PROMPT

    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="Fix login", issue_body="",
        branch="issue-7", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    for text in (briefing, REVIEWER_SYSTEM_PROMPT):
        assert "coord report-result" in text
        assert "COORD_ASSIGNMENT_ID" in text
        assert "belt and braces" in text


def test_briefing_uses_generic_checklist_when_none_configured() -> None:
    briefing = build_review_briefing(
        pr_number=1, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="b", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True, checklist=[]), repo_claude_md=None,
    )
    assert "Do tests pass?" in briefing
    assert "Did the worker stay within the assigned file scope?" in briefing


def test_briefing_falls_back_to_branch_diff_when_no_pr() -> None:
    briefing = build_review_briefing(
        pr_number=None, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    # Must fetch first and diff against origin/<base> — diffing bare local
    # `main` against a recently-cut branch on a stale agent checkout sweeps in
    # every PR merged since the local ref last moved (the #563 "bundled 5
    # issues" false positive). Always fetch + origin/<base>...origin/<branch>.
    assert "git fetch origin && git diff origin/main...origin/my-branch" in briefing
    assert "git diff main...my-branch" not in briefing
    assert "gh pr review" not in briefing


def test_briefing_no_pr_diff_uses_default_branch_not_hardcoded_main() -> None:
    """The no-PR diff base must follow ``default_branch`` (e.g. ``develop``),
    not a hardcoded ``main`` — otherwise develop-default repos diff against the
    wrong base."""
    briefing = build_review_briefing(
        pr_number=None, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        default_branch="develop",
    )
    assert "git diff origin/develop...origin/my-branch" in briefing
    assert "origin/main" not in briefing


def test_briefing_first_review_is_full_scope() -> None:
    """review_iteration=0 (default) reviews the whole PR — no incremental
    language."""
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    assert "re-review iteration" not in briefing
    assert "Run the project's test suite." in briefing


def test_briefing_re_review_is_incremental_and_nit_suppressing() -> None:
    """review_iteration>0 scopes the review to the fix delta and tells the
    reviewer not to raise new non-blocking nits on already-reviewed code
    (#476)."""
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        default_branch="main", review_iteration=3,
    )
    assert "re-review iteration 3" in briefing
    assert "do NOT re-review" in briefing.lower() or "not re-review" in briefing.lower()
    assert "Do NOT raise new non-blocking nits" in briefing
    # Points the reviewer at the fix delta, not the full PR diff.
    assert "git log --oneline origin/main...origin/my-branch" in briefing


def test_briefing_embeds_diff_text_when_supplied() -> None:
    """#612: a supplied merge-base diff is embedded verbatim and the reviewer
    is told NOT to compute its own diff (a stale-base diff false-flags
    already-merged commits as deletions)."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+added_line = 1\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
    )
    assert "## Diff to review (authoritative)" in briefing
    assert "added_line = 1" in briefing
    assert "Do NOT compute your own diff" in briefing
    # The "What to do" step 1 points at the embedded section, not a git command.
    assert "already fetched for you" in briefing


def test_briefing_no_diff_text_keeps_three_dot_fallback() -> None:
    """#612: with diff_text=None the existing three-dot ``git diff origin/``
    fallback instructions stand (no embedded diff section)."""
    briefing = build_review_briefing(
        pr_number=None, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=None,
    )
    assert "## Diff to review (authoritative)" not in briefing
    assert "git diff origin/main...origin/my-branch" in briefing


def test_briefing_no_sealed_paths_by_default() -> None:
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    assert "SEALED" not in briefing
    assert "Sealed paths" not in briefing


def test_briefing_sealed_paths_reminder_when_diff_untouched() -> None:
    """#944 sealing v1: when a repo has an acceptance driver, the reviewer
    always gets told the oracle is sealed — even if this diff doesn't touch
    it — so REQUEST-changes is the default reflex, not something it has to
    infer from the checklist alone."""
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+added_line = 1\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=944, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
    )
    assert "## Sealed paths (do not touch)" in briefing
    assert "tests/acceptance/" in briefing
    assert "TAMPER DETECTED" not in briefing


def test_briefing_flags_tamper_when_diff_touches_sealed_path() -> None:
    diff = (
        "diff --git a/tests/acceptance/ms01/foo.rs b/tests/acceptance/ms01/foo.rs\n"
        "--- a/tests/acceptance/ms01/foo.rs\n"
        "+++ b/tests/acceptance/ms01/foo.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+cheated = True\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=944, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
    )
    assert "SEALED ORACLE TAMPER DETECTED" in briefing
    assert "tests/acceptance/" in briefing
    assert "request-changes is mandatory" in briefing


def test_diff_touched_sealed_paths_matches_diff_git_header() -> None:
    from coord.review import _diff_touched_sealed_paths

    diff = "diff --git a/tests/acceptance/ms01/foo.rs b/tests/acceptance/ms01/foo.rs\n"
    assert _diff_touched_sealed_paths(diff, ["tests/acceptance/"]) == ["tests/acceptance/"]


def test_diff_touched_sealed_paths_no_match() -> None:
    from coord.review import _diff_touched_sealed_paths

    diff = "diff --git a/src/foo.py b/src/foo.py\n"
    assert _diff_touched_sealed_paths(diff, ["tests/acceptance/"]) == []


# ── #2966: coordinator-owned doc tamper check ───────────────────────────────
#
# "Only the coordinator writes docs" was prose-only — repo.coordinator_only_
# files was set by zero repos fleet-wide, so nothing structurally backstopped
# a worker rewriting the repo's own CLAUDE.md. Mirrors the #944 sealed-paths
# tamper checks above, but the rule never inverts by assignment_type: no
# dispatched type's job is ever editing the rulebook.

def test_briefing_no_coordinator_doc_paths_by_default() -> None:
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    assert "COORDINATOR-OWNED" not in briefing
    assert "Coordinator-owned docs" not in briefing


def test_briefing_coordinator_doc_reminder_when_diff_untouched() -> None:
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+added_line = 1\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=2966, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        coordinator_doc_paths=["CLAUDE.md"],
    )
    assert "## Coordinator-owned docs (do not touch)" in briefing
    assert "CLAUDE.md" in briefing
    assert "COORDINATOR-OWNED DOC EDITED" not in briefing


def test_briefing_flags_tamper_when_diff_touches_coordinator_doc() -> None:
    diff = (
        "diff --git a/CLAUDE.md b/CLAUDE.md\n"
        "--- a/CLAUDE.md\n"
        "+++ b/CLAUDE.md\n"
        "@@ -1,2 +1,3 @@\n"
        "+some rewritten rule\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=2966, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        coordinator_doc_paths=["CLAUDE.md"],
    )
    assert "COORDINATOR-OWNED DOC EDITED" in briefing
    assert "CLAUDE.md" in briefing
    assert "request-changes is mandatory" in briefing


def test_briefing_coordinator_doc_tamper_does_not_invert_for_test_author() -> None:
    """Unlike sealed_paths, this rule never flips for test-author/mock-author
    — no dispatched type's job is ever editing the repo's own rulebook."""
    diff = (
        "diff --git a/CLAUDE.md b/CLAUDE.md\n"
        "--- a/CLAUDE.md\n"
        "+++ b/CLAUDE.md\n"
        "@@ -1,2 +1,3 @@\n"
        "+some rewritten rule\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=2966, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        coordinator_doc_paths=["CLAUDE.md"],
        assignment_type="test-author",
    )
    assert "COORDINATOR-OWNED DOC EDITED" in briefing
    assert "request-changes is mandatory" in briefing


# ── #1175: test-author/mock-author inverse tamper check ────────────────────


def test_diff_paths_outside_sealed_returns_non_sealed_files() -> None:
    from coord.review import _diff_paths_outside_sealed

    diff = (
        "diff --git a/tests/acceptance/ms01/foo.rs b/tests/acceptance/ms01/foo.rs\n"
        "diff --git a/coord/review.py b/coord/review.py\n"
    )
    assert _diff_paths_outside_sealed(diff, ["tests/acceptance/"]) == ["coord/review.py"]


def test_diff_paths_outside_sealed_empty_when_fully_contained() -> None:
    from coord.review import _diff_paths_outside_sealed

    diff = "diff --git a/tests/acceptance/ms01/foo.rs b/tests/acceptance/ms01/foo.rs\n"
    assert _diff_paths_outside_sealed(diff, ["tests/acceptance/"]) == []


# ── #2192: free pre-review "missing test" nudge ─────────────────────────────


def test_diff_missing_test_coverage_flags_user_visible_diff_with_no_tests() -> None:
    """Target pattern (#2132: 5/27 blocking reviews, 18.5%): a diff that
    changes user-visible source but ships zero test files must flag."""
    from coord.review import diff_missing_test_coverage

    diff = (
        "diff --git a/coord/dashboard/server.py b/coord/dashboard/server.py\n"
        "--- a/coord/dashboard/server.py\n"
        "+++ b/coord/dashboard/server.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+def new_endpoint():\n"
        "+    return 'ok'\n"
    )
    assert diff_missing_test_coverage(diff) is True


def test_diff_missing_test_coverage_no_flag_when_test_file_added() -> None:
    """Same diff, plus a test file — must not flag."""
    from coord.review import diff_missing_test_coverage

    diff = (
        "diff --git a/coord/dashboard/server.py b/coord/dashboard/server.py\n"
        "--- a/coord/dashboard/server.py\n"
        "+++ b/coord/dashboard/server.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+def new_endpoint():\n"
        "+    return 'ok'\n"
        "diff --git a/tests/test_server.py b/tests/test_server.py\n"
        "--- a/tests/test_server.py\n"
        "+++ b/tests/test_server.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+def test_new_endpoint():\n"
        "+    assert True\n"
    )
    assert diff_missing_test_coverage(diff) is False


def test_diff_missing_test_coverage_no_flag_for_internal_only_diff() -> None:
    """Pure-refactor/internal-only diff (CLAUDE.md's existing exemption): a
    diff touching only docs/scripts/deploy — never shipped user-visible
    source — must not flag, honoring the exemption a pure path check can see."""
    from coord.review import diff_missing_test_coverage

    diff = (
        "diff --git a/docs/ARCHITECTURE.md b/docs/ARCHITECTURE.md\n"
        "--- a/docs/ARCHITECTURE.md\n"
        "+++ b/docs/ARCHITECTURE.md\n"
        "@@ -1,2 +1,3 @@\n"
        "+Some clarifying prose.\n"
        "diff --git a/scripts/drive-batch.sh b/scripts/drive-batch.sh\n"
        "--- a/scripts/drive-batch.sh\n"
        "+++ b/scripts/drive-batch.sh\n"
        "@@ -1,2 +1,3 @@\n"
        "+echo tidy\n"
    )
    assert diff_missing_test_coverage(diff) is False


def test_diff_missing_test_coverage_no_flag_for_empty_diff() -> None:
    from coord.review import diff_missing_test_coverage

    assert diff_missing_test_coverage(None) is False
    assert diff_missing_test_coverage("") is False


def test_diff_missing_test_coverage_recognizes_in_crate_rust_test_dir() -> None:
    """Nested test dirs (e.g. tui/tests/) must count as a test file, not
    just a repo-root tests/ prefix."""
    from coord.review import diff_missing_test_coverage

    diff = (
        "diff --git a/tui/src/app.rs b/tui/src/app.rs\n"
        "--- a/tui/src/app.rs\n"
        "+++ b/tui/src/app.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+pub fn new_thing() {}\n"
        "diff --git a/tui/tests/acceptance.rs b/tui/tests/acceptance.rs\n"
        "--- a/tui/tests/acceptance.rs\n"
        "+++ b/tui/tests/acceptance.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+mod new_thing_test;\n"
    )
    assert diff_missing_test_coverage(diff) is False


def test_briefing_test_author_touching_only_sealed_path_no_tamper() -> None:
    """#1175 acceptance criterion 1: a type="test-author" PR that touches only
    tests/acceptance/ms-NN/ must NOT trip mandatory request-changes — writing
    there is the assignment's entire job."""
    diff = (
        "diff --git a/tests/acceptance/ms37/foo.rs b/tests/acceptance/ms37/foo.rs\n"
        "--- a/tests/acceptance/ms37/foo.rs\n"
        "+++ b/tests/acceptance/ms37/foo.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+added_case = 1\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=1175, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
        assignment_type="test-author",
    )
    assert "SCOPE VIOLATION" not in briefing
    assert "TAMPER DETECTED" not in briefing
    assert "request-changes is mandatory" not in briefing
    assert "expected writes for type='test-author'" in briefing


def test_briefing_test_author_touching_outside_sealed_path_trips_tamper() -> None:
    """#1175 acceptance criterion 2: a type="test-author" PR that touches a
    file OUTSIDE tests/acceptance/ms-NN/ must trip mandatory request-changes —
    the inverse of the normal rule."""
    diff = (
        "diff --git a/tests/acceptance/ms37/foo.rs b/tests/acceptance/ms37/foo.rs\n"
        "diff --git a/coord/review.py b/coord/review.py\n"
        "--- a/coord/review.py\n"
        "+++ b/coord/review.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+cheated = True\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=1175, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
        assignment_type="test-author",
    )
    assert "SEALED ORACLE SCOPE VIOLATION" in briefing
    assert "coord/review.py" in briefing
    assert "request-changes is mandatory" in briefing


def test_briefing_mock_author_touching_only_sealed_path_no_tamper() -> None:
    """#1175: mock-author gets the same exemption as test-author (contract.md
    + mocks under the same sealed tree)."""
    diff = "diff --git a/tests/acceptance/ms05/contract.md b/tests/acceptance/ms05/contract.md\n"
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=1175, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
        assignment_type="mock-author",
    )
    assert "SCOPE VIOLATION" not in briefing
    assert "TAMPER DETECTED" not in briefing


def test_briefing_mock_author_touching_outside_sealed_path_trips_tamper() -> None:
    diff = (
        "diff --git a/tests/acceptance/ms05/contract.md b/tests/acceptance/ms05/contract.md\n"
        "diff --git a/coord/agent.py b/coord/agent.py\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=1175, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
        assignment_type="mock-author",
    )
    assert "SEALED ORACLE SCOPE VIOLATION" in briefing
    assert "coord/agent.py" in briefing


def test_briefing_work_type_still_trips_tamper_on_sealed_touch() -> None:
    """#1175 acceptance criterion 3 (regression guard): a type="work" PR
    touching tests/acceptance/** must still trip the original mandatory
    request-changes rule, unaffected by the test-author/mock-author
    exemption. Same fixture as
    test_briefing_flags_tamper_when_diff_touches_sealed_path but explicit
    about assignment_type (defaults to "work")."""
    diff = (
        "diff --git a/tests/acceptance/ms01/foo.rs b/tests/acceptance/ms01/foo.rs\n"
        "--- a/tests/acceptance/ms01/foo.rs\n"
        "+++ b/tests/acceptance/ms01/foo.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+cheated = True\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=944, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
        assignment_type="work",
    )
    assert "SEALED ORACLE TAMPER DETECTED" in briefing
    assert "request-changes is mandatory" in briefing
    assert "SCOPE VIOLATION" not in briefing


# ── #1552: the driver entry point is part of the sealed oracle ─────────────
#
# Since #1175 a `test-author` on the `tui-tuidriver` route could not author a
# runnable slice AT ALL: `cargo test --test acceptance` cannot see
# `tests/acceptance/ms-38/slice.rs` until `tui/tests/acceptance.rs`
# `include!`s it, and that file sits outside the only sealed path there was,
# so wiring the slice in was a mandatory request-changes and not wiring it in
# shipped dead code. The real branch is `test-author-ms-38-slice-1124`
# (PR #1536): commit 91d5b42 is the shape these tests say must be ALLOWED,
# 7f48bcf ("delete the include! line to pass review") is the degenerate
# outcome that must no longer be the only legal option.

_TUI_SEALED = ["tests/acceptance/", "tui/tests/acceptance.rs"]


def _author_briefing(diff: str, **kwargs) -> str:
    return build_review_briefing(
        pr_number=1536, pr_url=None, repo_github="acme/claude-coordinator",
        repo_name="claude-coordinator",
        issue_number=1124, issue_title="X", issue_body="",
        branch="test-author-ms-38-slice-1124", worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=_TUI_SEALED,
        sealed_entrypoints=["tui/tests/acceptance.rs"],
        assignment_type="test-author",
        **kwargs,
    )


def test_briefing_test_author_may_wire_slice_into_declared_entrypoint() -> None:
    """#1552 primary goal: slice files + the declared driver entry point is a
    LEGAL test-author diff — no mandatory request-changes."""
    diff = (
        "diff --git a/tests/acceptance/ms-38/plans_help_1124.rs "
        "b/tests/acceptance/ms-38/plans_help_1124.rs\n"
        "--- /dev/null\n"
        "+++ b/tests/acceptance/ms-38/plans_help_1124.rs\n"
        "@@ -0,0 +1,3 @@\n"
        "+fn plans_help_overlay() {}\n"
        "diff --git a/tui/tests/acceptance.rs b/tui/tests/acceptance.rs\n"
        "--- a/tui/tests/acceptance.rs\n"
        "+++ b/tui/tests/acceptance.rs\n"
        "@@ -1,2 +1,3 @@\n"
        '+include!("../../tests/acceptance/ms-38/plans_help_1124.rs");\n'
    )
    briefing = _author_briefing(diff)
    assert "SEALED ORACLE SCOPE VIOLATION" not in briefing
    assert "TAMPER DETECTED" not in briefing
    assert "request-changes is mandatory" not in briefing
    assert "expected writes for type='test-author'" in briefing
    # The narrower entry-point rule is stated, not just silence.
    assert "Driver entry point — additive registration only" in briefing


def test_briefing_test_author_entrypoint_allowance_is_narrow() -> None:
    """#1552: the allowance is 'add a registration line', not 'rewrite the
    crate root' — and deleting it to narrow the diff is called out as the
    non-fix it is (the 7f48bcf outcome)."""
    diff = (
        "diff --git a/tests/acceptance/ms-38/plans_help_1124.rs "
        "b/tests/acceptance/ms-38/plans_help_1124.rs\n"
        "diff --git a/tui/tests/acceptance.rs b/tui/tests/acceptance.rs\n"
    )
    briefing = _author_briefing(diff)
    assert "rewriting, reordering, or deleting" in briefing
    assert "Deleting the registration line to make a diff look" in briefing


def test_briefing_test_author_other_paths_still_refused_with_entrypoint() -> None:
    """#1552: widening the sealed set to the entry point must NOT weaken the
    rule for anything else — an implementation file still trips it."""
    diff = (
        "diff --git a/tests/acceptance/ms-38/plans_help_1124.rs "
        "b/tests/acceptance/ms-38/plans_help_1124.rs\n"
        "diff --git a/tui/tests/acceptance.rs b/tui/tests/acceptance.rs\n"
        "diff --git a/tui/src/app.rs b/tui/src/app.rs\n"
        "--- a/tui/src/app.rs\n"
        "+++ b/tui/src/app.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+let cheated = true;\n"
    )
    briefing = _author_briefing(diff)
    assert "SEALED ORACLE SCOPE VIOLATION" in briefing
    assert "tui/src/app.rs" in briefing
    assert "request-changes is mandatory" in briefing


def test_briefing_entrypoint_is_exact_match_not_a_prefix() -> None:
    """#1552: an entrypoint names one FILE. A near-miss sibling
    (`...rs.bak`) must not slip through on a `startswith` match."""
    diff = "diff --git a/tui/tests/acceptance.rs.bak b/tui/tests/acceptance.rs.bak\n"
    briefing = _author_briefing(diff)
    assert "SEALED ORACLE SCOPE VIOLATION" in briefing
    assert "tui/tests/acceptance.rs.bak" in briefing


def test_briefing_work_type_trips_tamper_on_entrypoint() -> None:
    """#1552, the other direction: a `type="work"` worker editing the oracle's
    crate root is tamper — it can unwire the slice it is graded against
    without ever touching `tests/acceptance/**`."""
    diff = (
        "diff --git a/tui/tests/acceptance.rs b/tui/tests/acceptance.rs\n"
        "--- a/tui/tests/acceptance.rs\n"
        "+++ b/tui/tests/acceptance.rs\n"
        "@@ -1,3 +1,2 @@\n"
        '-include!("../../tests/acceptance/ms-38/plans_help_1124.rs");\n'
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/claude-coordinator",
        repo_name="claude-coordinator",
        issue_number=1124, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=_TUI_SEALED,
        sealed_entrypoints=["tui/tests/acceptance.rs"],
        assignment_type="work",
    )
    assert "SEALED ORACLE TAMPER DETECTED" in briefing
    assert "tui/tests/acceptance.rs" in briefing
    assert "request-changes is mandatory" in briefing


def test_briefing_work_type_trips_tamper_on_relocated_slice() -> None:
    """#2896: the ms-33/38/65/67 slices moved from the repo-root
    `tests/acceptance/` into `tui/tests/acceptance/` — a `type="work"` diff
    touching one at its NEW location must still trip mandatory
    request-changes, exactly as it did at the old one."""
    diff = (
        "diff --git a/tui/tests/acceptance/ms-65/board_tabs_2282.rs "
        "b/tui/tests/acceptance/ms-65/board_tabs_2282.rs\n"
        "--- a/tui/tests/acceptance/ms-65/board_tabs_2282.rs\n"
        "+++ b/tui/tests/acceptance/ms-65/board_tabs_2282.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+// cheated\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/claude-coordinator",
        repo_name="claude-coordinator",
        issue_number=2282, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/", "tui/tests/acceptance.rs", "tui/tests/acceptance/"],
        sealed_entrypoints=["tui/tests/acceptance.rs"],
        assignment_type="work",
    )
    assert "SEALED ORACLE TAMPER DETECTED" in briefing
    assert "tui/tests/acceptance/ms-65/board_tabs_2282.rs" in briefing
    assert "request-changes is mandatory" in briefing


def test_briefing_pure_rename_between_two_sealed_paths_is_not_tamper() -> None:
    """#2896 review: relocating a sealed slice from one already-sealed
    location to another (both declared by AcceptanceConfig.sealed_paths — a
    byte-identical `git mv`, git's own `similarity index 100%`) is not
    tampering. Only the rename hunks are present here — no content-changing
    hunk touches either path — mirroring the real #2896 PR's relocated
    slices."""
    diff = (
        "diff --git a/tests/acceptance/ms-65/board_tabs_2282.rs "
        "b/tui/tests/acceptance/ms-65/board_tabs_2282.rs\n"
        "similarity index 100%\n"
        "rename from tests/acceptance/ms-65/board_tabs_2282.rs\n"
        "rename to tui/tests/acceptance/ms-65/board_tabs_2282.rs\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/claude-coordinator",
        repo_name="claude-coordinator",
        issue_number=2282, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/", "tui/tests/acceptance.rs", "tui/tests/acceptance/"],
        sealed_entrypoints=["tui/tests/acceptance.rs"],
        assignment_type="work",
    )
    assert "SEALED ORACLE TAMPER DETECTED" not in briefing


def test_briefing_rename_out_of_the_sealed_tree_still_trips_tamper() -> None:
    """The narrow carve-out only covers a move between two ALREADY-sealed
    locations — a rename that moves sealed content somewhere unsealed is
    exactly the exfiltration case the tamper check exists to catch, so it
    must still trip."""
    diff = (
        "diff --git a/tests/acceptance/ms-65/board_tabs_2282.rs "
        "b/src/board_tabs_2282.rs\n"
        "similarity index 100%\n"
        "rename from tests/acceptance/ms-65/board_tabs_2282.rs\n"
        "rename to src/board_tabs_2282.rs\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/claude-coordinator",
        repo_name="claude-coordinator",
        issue_number=2282, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/", "tui/tests/acceptance.rs", "tui/tests/acceptance/"],
        sealed_entrypoints=["tui/tests/acceptance.rs"],
        assignment_type="work",
    )
    assert "SEALED ORACLE TAMPER DETECTED" in briefing
    assert "tests/acceptance/ms-65/board_tabs_2282.rs" in briefing


def test_diff_touched_sealed_paths_exempts_sealed_to_sealed_pure_rename() -> None:
    from coord.review import _diff_touched_sealed_paths

    diff = (
        "diff --git a/tests/acceptance/ms-65/foo.rs b/tui/tests/acceptance/ms-65/foo.rs\n"
        "similarity index 100%\n"
        "rename from tests/acceptance/ms-65/foo.rs\n"
        "rename to tui/tests/acceptance/ms-65/foo.rs\n"
    )
    sealed = ["tests/acceptance/", "tui/tests/acceptance/"]
    assert _diff_touched_sealed_paths(diff, sealed) == []


def test_diff_touched_sealed_paths_still_flags_a_content_edit_at_the_same_path() -> None:
    """Sanity: the exemption is keyed to git's own rename markers, not to
    "old path == new path pattern" — a plain content edit (no rename lines)
    at a sealed path is untouched by the carve-out."""
    from coord.review import _diff_touched_sealed_paths

    diff = "diff --git a/tests/acceptance/ms01/foo.rs b/tests/acceptance/ms01/foo.rs\n"
    assert _diff_touched_sealed_paths(diff, ["tests/acceptance/"]) == ["tests/acceptance/"]


def test_briefing_pytest_route_has_no_entrypoint_section() -> None:
    """#1552: the pytest route legitimately declares no entry point (pytest
    discovers by directory), so nothing extra is said and #1175's rule is
    completely unchanged for it."""
    diff = (
        "diff --git a/tests/acceptance/ms-37/test_usage_cli_1115.py "
        "b/tests/acceptance/ms-37/test_usage_cli_1115.py\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/claude-coordinator",
        repo_name="claude-coordinator",
        issue_number=1115, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
        sealed_entrypoints=[],
        assignment_type="test-author",
    )
    assert "Driver entry point" not in briefing
    assert "SCOPE VIOLATION" not in briefing


def test_pr_diff_truncates_at_max_chars(monkeypatch) -> None:
    """#612: github_ops.pr_diff caps a huge diff and appends a truncation note."""
    from coord import github_ops

    big = "x" * 10_000
    monkeypatch.setattr(github_ops, "_gh", lambda *args, **kwargs: big)
    out = github_ops.pr_diff("acme/api", 42, max_chars=100)
    assert out is not None
    assert out.startswith("x" * 100)
    assert "[diff truncated at 100 chars]" in out
    assert len(out) < len(big)


def test_pr_diff_max_chars_none_returns_full_diff(monkeypatch) -> None:
    """#1475: max_chars=None must return the diff byte-for-byte, with no
    truncation and no trailing note — needed so `compute_patch_id` hashes
    exactly what the merge-time `get_branch_patch_id` compare-API fetch
    hashes, whatever the diff's size."""
    from coord import github_ops

    big = "x" * 100_000
    monkeypatch.setattr(github_ops, "_gh", lambda *args, **kwargs: big)
    out = github_ops.pr_diff("acme/api", 42, max_chars=None)
    assert out == big


def test_truncate_diff_text_matches_pr_diff_truncation(monkeypatch) -> None:
    """#1475: truncate_diff_text is the extracted helper pr_diff uses
    internally — calling it directly on a full diff must produce the exact
    same display copy pr_diff would have returned for that max_chars."""
    from coord import github_ops

    big = "x" * 10_000
    monkeypatch.setattr(github_ops, "_gh", lambda *args, **kwargs: big)
    via_pr_diff = github_ops.pr_diff("acme/api", 42, max_chars=100)
    via_helper = github_ops.truncate_diff_text(big, max_chars=100)
    assert via_pr_diff == via_helper


def _diff_for(path: str, n_lines: int) -> str:
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"]
    lines += [f"+line {i}" for i in range(n_lines)]
    return "\n".join(lines) + "\n"


def test_truncate_diff_text_cuts_on_file_boundary_not_mid_hunk() -> None:
    """#2819: truncation must never hand the reviewer a partial hunk — the
    cut lands on the last `diff --git` boundary that fits, so every file
    kept in the output is complete."""
    from coord import github_ops

    diff = _diff_for("a.py", 20) + _diff_for("b.py", 20) + _diff_for("c.py", 2000)
    out = github_ops.truncate_diff_text(diff, max_chars=1000)

    # Both kept files are present in full (their own last line included);
    # nothing stops mid-hunk.
    assert "+line 19\ndiff --git a/b.py" in out
    assert "+line 19\n... [diff truncated" in out
    assert "diff --git a/c.py" not in out


def test_truncate_diff_text_lists_omitted_files() -> None:
    """#2819: a reviewer handed a truncated diff must be told which files
    it did not see, so it can inspect them directly instead of silently
    missing them."""
    from coord import github_ops

    diff = _diff_for("a.py", 20) + _diff_for("b.py", 20) + _diff_for("c.py", 2000)
    out = github_ops.truncate_diff_text(diff, max_chars=1000)

    assert "1 file(s) omitted" in out
    assert "Files omitted by truncation" in out
    assert "  - c.py" in out
    # Only the dropped file is listed — the kept files aren't flagged as omitted.
    assert "  - a.py" not in out
    assert "  - b.py" not in out


def test_truncate_diff_text_falls_back_to_char_slice_when_no_boundary_fits() -> None:
    """#2819: when even the first file's own diff exceeds max_chars (or the
    input has no `diff --git` boundaries at all), fall back to the old raw
    character slice rather than emitting an empty string."""
    from coord import github_ops

    diff = _diff_for("huge.py", 5000)
    out = github_ops.truncate_diff_text(diff, max_chars=1000)

    assert len(out) > 1000  # head (1000 chars) + trailing note
    assert out.startswith(diff[:1000])
    assert "omitted" not in out  # no boundary found -> no file list to report


def test_truncate_diff_text_flags_huge_first_file_as_incomplete() -> None:
    """#2819 follow-up: when the *first* file's own diff already exceeds
    max_chars, the char-slice fallback used to let it ride into `head`
    looking complete (its header made it in, so it never appeared on the
    "omitted" list) while its body was silently cut mid-hunk. A reviewer
    trusting the omitted-files list would wrongly conclude the huge first
    file was shown in full. It must now be called out explicitly, distinct
    from files that were dropped entirely."""
    from coord import github_ops

    diff = _diff_for("huge.py", 5000) + _diff_for("small.py", 5)
    out = github_ops.truncate_diff_text(diff, max_chars=1000)

    # The huge first file is cut off mid-hunk, not omitted entirely — it
    # must be flagged as incomplete rather than silently passing review.
    assert "huge.py" in out
    assert "cut off mid-diff" in out
    assert "INCOMPLETE" in out
    # The trailing file is genuinely never shown at all — still on the
    # separate "omitted" list.
    assert "1 file(s) omitted" in out
    assert "Files omitted by truncation" in out
    assert "  - small.py" in out


def test_pr_diff_returns_none_on_gh_error(monkeypatch) -> None:
    """#612: pr_diff is best-effort — a gh failure yields None, not a raise."""
    from coord import github_ops

    def _boom(*args, **kwargs):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(github_ops, "_gh", _boom)
    assert github_ops.pr_diff("acme/api", 42) is None


# ── dispatch_review (integration with mocked agent HTTP) ────────────────────


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeHTTPResponse:
        self.calls.append((url, json))
        return _FakeHTTPResponse(self._payload)


class _BadRequestResponse:
    """Simulates an agent 400 'does not handle repo' response (#904)."""

    def raise_for_status(self) -> None:
        import httpx
        raise httpx.HTTPStatusError(
            "400 Bad Request",
            request=httpx.Request("POST", "http://test/assign"),
            response=httpx.Response(
                400,
                text='{"error": "this agent does not handle repo"}',
            ),
        )

    def json(self) -> dict:
        return {"error": "this agent does not handle repo"}


class _FallThroughClient:
    """HTTP client that rejects one URL with 400, succeeds for all others (#904)."""

    def __init__(self, reject_fragment: str, success_payload: dict) -> None:
        self._reject_fragment = reject_fragment
        self._success_payload = success_payload
        self.calls: list[str] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append(url)
        if self._reject_fragment in url:
            return _BadRequestResponse()
        return _FakeHTTPResponse(self._success_payload)


class _AllRejectingClient:
    """HTTP client that always returns 400 (#904 exhaustion test)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append(url)
        return _BadRequestResponse()


class _ServerErrorResponse:
    """Simulates a transient agent 500 (mid-restart, unhandled exception, #904
    fix #2) — NOT a definitive "this agent doesn't handle this repo" rejection."""

    def raise_for_status(self) -> None:
        import httpx
        raise httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", "http://test/assign"),
            response=httpx.Response(500, text='{"error": "internal error"}'),
        )

    def json(self) -> dict:
        return {"error": "internal error"}


class _AllServerErrorClient:
    """HTTP client that always returns 500 (#904 fix #2 transient-5xx test)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append(url)
        return _ServerErrorResponse()


def test_dispatch_review_skipped_when_disabled(two_machine_config: Config) -> None:
    cfg = replace(two_machine_config, reviews=ReviewsConfig(enabled=False))
    board = Board()
    result = dispatch_review(
        _completed_assignment(), board, cfg,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    assert board.active == []


def test_dispatch_review_skipped_for_failed_assignment(
    two_machine_config: Config,
) -> None:
    failed = replace(_completed_assignment(), status="failed")
    board = Board()
    result = dispatch_review(
        failed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None


def test_dispatch_review_skipped_when_no_branch(two_machine_config: Config) -> None:
    no_branch = replace(_completed_assignment(), branch=None)
    board = Board()
    result = dispatch_review(
        no_branch, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None


def test_dispatch_review_skipped_for_review_type(two_machine_config: Config) -> None:
    """Reviews don't trigger reviews-of-reviews — avoid infinite loops."""
    review = replace(_completed_assignment(), type="review")
    board = Board()
    result = dispatch_review(
        review, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    # #1627: passing a non-work id (e.g. a "smoke" assignment mistaken for
    # the work row — the vimcode #613 incident) used to be a bare
    # `return None` with zero diagnostic trail. It now names itself and
    # points at the escape hatch for "what IS the work row for this issue".
    assert review.review_dispatch_reason is not None
    assert "not reviewable work" in review.review_dispatch_reason
    assert "coord diagnose" in review.review_dispatch_reason


def test_dispatch_review_dispatches_for_mock_author_type(
    two_machine_config: Config,
) -> None:
    """#930 fix: a completed ``type="mock-author"`` (Gate A) assignment must
    be eligible for review dispatch, not just ``type="work"`` — otherwise a
    Gate A branch can never reach a review through any `coord` command
    (`coord pr`, `coord notify`, the daemon tick), contradicting the type's
    own docstring/system-prompt promise that it flows through the same
    Work -> Test -> Review -> Merge pipeline as ordinary work."""
    board = Board()
    completed = replace(
        _completed_assignment(),
        type="mock-author",
        assignment_id="ma-1",
        branch="ms-5-gate-a",
    )
    client = _FakeHTTPClient({"id": "review-id-ma"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 43,
            "url": "https://github.com/acme/api/pull/43",
            "existed": True,
        },
        claude_md_reader=lambda p: "# Project rules\n",
        issue_body_fetcher=lambda repo, num: "issue body text",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    assert result.type == "review"
    assert result.review_of_assignment_id == "ma-1"
    assert board.active == [result]


def test_dispatch_review_dispatches_for_test_author_type(
    two_machine_config: Config,
) -> None:
    """#1141 fix: a completed ``type="test-author"`` (#931, per-issue JIT
    acceptance-slice authoring) assignment must be eligible for review
    dispatch, not just ``type="work"``/``"mock-author"`` — otherwise a
    test-author branch can never reach a review through any `coord` command
    (`coord pr`, `coord notify`, the daemon tick), the exact silent stall
    confirmed live on PR #1139 (epic #1117/ms-37 retrofit)."""
    board = Board()
    completed = replace(
        _completed_assignment(),
        type="test-author",
        assignment_id="ta-1",
        branch="ms-37-test-author",
    )
    client = _FakeHTTPClient({"id": "review-id-ta"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 44,
            "url": "https://github.com/acme/api/pull/44",
            "existed": True,
        },
        claude_md_reader=lambda p: "# Project rules\n",
        issue_body_fetcher=lambda repo, num: "issue body text",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    assert result.type == "review"
    assert result.review_of_assignment_id == "ta-1"
    assert board.active == [result]


def test_dispatch_review_skipped_when_work_terminal(
    two_machine_config: Config, monkeypatch
) -> None:
    """#522 chokepoint: a completed work whose issue is closed / PR merged must
    not be reviewed — the second flood vector (reviews of already-merged
    #349/#194). Short-circuits before opening a PR and marks the row done."""
    monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
    completed = _completed_assignment()
    board = Board()
    pr_calls = {"n": 0}

    def _pr_lookup(repo_github, **kw):
        pr_calls["n"] += 1
        return {"number": 1, "url": "u", "existed": True}

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=_pr_lookup,
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    assert board.active == []
    assert pr_calls["n"] == 0, "must short-circuit before opening a PR"
    assert completed.review_state == "done"


def test_dispatch_pending_reviews_skips_terminal_rows(
    two_machine_config: Config, monkeypatch
) -> None:
    """#522: the bulk pending-review loop never dispatches a review for an
    already-merged row — it marks it done so it drops out of `eligible`."""
    from coord.review import dispatch_pending_reviews

    monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
    # test_state="passed" so the row clears the Test-before-Review gate and the
    # bulk loop reaches the #522 terminal check (a merged row was smoke-tested
    # before it merged).
    completed = replace(
        _completed_assignment(), review_state="pending", test_state="passed"
    )
    board = Board(completed=[completed])

    dispatched = dispatch_pending_reviews(board, two_machine_config)

    assert dispatched == []
    assert completed.review_state == "done"


def test_dispatch_review_sends_to_different_machine_and_appends_to_board(
    two_machine_config: Config,
) -> None:
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42,
            "url": "https://github.com/acme/api/pull/42",
            "existed": True,
        },
        claude_md_reader=lambda p: "# Project rules\n",
        issue_body_fetcher=lambda repo, num: "issue body text",
        now=123.0,
        # Branch check mocked: this test covers routing, not remote-branch detection.
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    assert result.type == "review"
    assert result.machine_name == "server"  # different from worker (laptop)
    assert result.review_target == "42"
    assert result.review_of_assignment_id == "abc123"
    assert result.status == "running"
    assert result.assignment_id == "review-id-1"
    assert result.dispatched_at == 123.0
    assert board.active == [result]

    # Verify the HTTP payload went to the reviewer machine with the review
    # type and the reviewer system prompt.
    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert "server.tail" in url
    assert payload["type"] == "review"
    assert payload["system_prompt"] == REVIEWER_SYSTEM_PROMPT
    assert payload["review_target"] == "42"
    assert payload["repo_path"] == "/srv/api"  # reviewer's local path
    assert "# Project rules" in payload["briefing"]


def _opencode_repo_config(*, reviews_provider: str | None) -> Config:
    """A repo pinned to `provider: opencode`, with a distinctly-named
    claude-type provider (`review-claude`) also registered, so a test can
    tell "the review's own provider" apart from both `repo.provider` and
    the bare implicit `"claude"` default by name alone."""
    repo = Repo(
        name="api", github="acme/api", depends_on=[], default_branch="main",
        provider="opencode",
    )
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                capabilities=["python", "provider:opencode"], repos=["api"],
                repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server", host="server.tail",
                capabilities=["python", "provider:opencode"], repos=["api"],
                repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(
            enabled=True, auto_dispatch=True, provider=reviews_provider,
        ),
        providers=ProvidersConfig(
            definitions={
                "claude": ProviderDef(type="claude"),
                "opencode": ProviderDef(type="opencode"),
                "review-claude": ProviderDef(type="claude"),
            },
        ),
    )


def test_dispatch_review_uses_reviews_provider_over_repo_provider(
) -> None:
    """#1811 acceptance: `reviews.provider` set to a claude-type provider on
    a repo pinned to an opencode-type provider (`repo.provider="opencode"`)
    dispatches the review through the named claude-type provider, NOT
    through opencode — the whole point of #1811 (worker on opencode,
    reviewer on claude, zero shared model family). Must fail against
    today's code, where the review guard always resolves `repo.provider`
    (there is no way to override it) and the wire payload never even
    carries a "provider" key for reviews at all."""
    cfg = _opencode_repo_config(reviews_provider="review-claude")
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-claude-1"})

    result = dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42",
            "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    # Recorded on the Assignment exactly like coord.dispatch.dispatch()
    # records the work provider — not silently dropped.
    assert result.provider_name == "review-claude"

    assert len(client.calls) == 1
    _url, payload = client.calls[0]
    assert payload["provider"] == "review-claude"


def test_dispatch_review_unset_provider_inherits_repo_provider(
) -> None:
    """#1811 regression: leaving `reviews.provider` unset must inherit
    `repo.provider` exactly as it did before this field existed — the
    behavior of every existing deployment (none of which set the brand-new
    key) must not change."""
    cfg = _opencode_repo_config(reviews_provider=None)
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-opencode-1"})

    result = dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42",
            "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    assert result.provider_name == "opencode"
    assert len(client.calls) == 1
    _url, payload = client.calls[0]
    assert payload["provider"] == "opencode"


def test_dispatch_review_uses_feature_branch_for_opted_in_milestone(
    two_machine_config: Config,
) -> None:
    """#934: a repo that opted into the git model (develop_branch set)
    diffs/opens the PR against `feature/ms-NN`, not `default_branch`, when
    the completed work's issue belongs to that milestone."""
    from dataclasses import replace as _replace

    cfg = _replace(
        two_machine_config,
        repos=[_replace(two_machine_config.repos[0], develop_branch="develop")],
    )
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})
    pr_lookup_calls = []

    def _pr_lookup(repo_github, **kw):
        pr_lookup_calls.append(kw)
        return {"number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True}

    dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=_pr_lookup,
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
        milestone_fetcher=lambda repo_github, issue_number: 42,
    )

    assert pr_lookup_calls[0]["default_branch"] == "feature/ms-42"
    _, payload = client.calls[0]
    assert payload["branch"] == "feature/ms-42"


def test_dispatch_review_milestone_fetcher_not_consulted_when_not_opted_in(
    two_machine_config: Config,
) -> None:
    """#934: a repo without develop_branch stays on default_branch even if
    the milestone_fetcher would return one — it must not even be called,
    since a non-opted-in repo pays zero extra `gh` calls."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    def _boom_fetcher(repo_github, issue_number):
        raise AssertionError("milestone_fetcher must not be called for a non-opted-in repo")

    dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
        milestone_fetcher=_boom_fetcher,
    )

    _, payload = client.calls[0]
    assert payload["branch"] == "main"


def test_dispatch_review_tolerates_repo_stand_in_without_develop_branch(
    two_machine_config: Config,
) -> None:
    """#1388: some Repo-shaped stand-ins (a wire-reconstructed object, or a
    stale install predating #934) lack ``develop_branch`` entirely — direct
    attribute access raised ``AttributeError`` and 500'd the dashboard's
    dispatch-review action. Must fail open to `default_branch`, exactly like
    a repo that has not opted into the develop/feature-branch model."""

    class _StandInRepo:
        """Repo-shaped stand-in predating ``develop_branch`` (#934) but
        carrying the older #323 ``provider`` field, so this isolates the
        regression to the one attribute review.py failed to guard."""

        name = "api"
        github = "acme/api"
        default_branch = "main"
        provider = None

    cfg = replace(two_machine_config, repos=[_StandInRepo()])
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    result = dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    _, payload = client.calls[0]
    assert payload["branch"] == "main"


def test_dispatch_review_flags_sealed_acceptance_dir_when_driver_configured(
    two_machine_config: Config,
) -> None:
    """#944 sealing v1: dispatch_review must thread sealed_paths through to
    the briefing for any repo with an oracle-loop acceptance driver."""
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig
    from dataclasses import replace as _replace

    cfg = _replace(
        two_machine_config,
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
        }),
    )
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert "Sealed paths (do not touch)" in payload["briefing"]
    assert "tests/acceptance/" in payload["briefing"]


def test_dispatch_review_flags_coordinator_owned_docs_without_config(
    two_machine_config: Config,
) -> None:
    """#2966: dispatch_review must thread coordinator_doc_paths through to
    the briefing for EVERY repo, not just ones that configure
    coordinator_only_files — two_machine_config's "api" repo sets neither."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert "Coordinator-owned docs (do not touch)" in payload["briefing"]
    assert "CLAUDE.md" in payload["briefing"]


def test_dispatch_review_threads_assignment_type_for_test_author_exemption(
    two_machine_config: Config,
) -> None:
    """#1175: dispatch_review must pass the completed assignment's own type
    (not a hardcoded "work") into build_review_briefing, or a real
    type="test-author" completion would still get the blanket tamper rule
    even though the fix above exists."""
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig
    from dataclasses import replace as _replace

    cfg = _replace(
        two_machine_config,
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
        }),
    )
    board = Board()
    completed = replace(
        _completed_assignment(machine="laptop"),
        type="test-author",
        assignment_id="ta-1",
        branch="ms-37-test-author",
    )
    client = _FakeHTTPClient({"id": "review-id-1"})

    dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert "expected writes for type='test-author'" in payload["briefing"]
    assert "TAMPER DETECTED" not in payload["briefing"]


def test_dispatch_review_flags_sealed_acceptance_dir_when_driver_is_routed(
    two_machine_config: Config,
) -> None:
    """#1125 review finding 1: same as
    test_dispatch_review_flags_sealed_acceptance_dir_when_driver_configured,
    but the repo's driver is routed rather than flat — sealing must still
    trigger since `driver_for(repo_name)` (no path) can't select a route and
    would otherwise silently return None here."""
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig
    from dataclasses import replace as _replace

    cfg = _replace(
        two_machine_config,
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(routes=[
                AcceptanceDriverConfig(match="**", kind="cli-pytest", run="pytest"),
            ]),
        }),
    )
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert "Sealed paths (do not touch)" in payload["briefing"]
    assert "tests/acceptance/" in payload["briefing"]


def test_dispatch_review_captures_branch_sha(
    two_machine_config: Config,
) -> None:
    """#821: dispatch_review must set review_head_sha on the returned Assignment.

    When the branch SHA can be fetched, the review Assignment carries it so
    has_approved_review can later reject the approval if new commits are pushed
    onto the branch after the review ran (stale-SHA check).
    """
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "sha-review-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 7, "url": "https://github.com/acme/api/pull/7", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        branch_sha_fetcher=lambda repo, branch: "deadbeef1234",  # injected for test
        diff_fetcher=lambda repo, num, **kw: None,  # #1484: no live `gh pr diff`
    )

    assert result is not None
    assert result.review_head_sha == "deadbeef1234", (
        "review_head_sha must be captured from branch tip at dispatch time"
    )


def test_dispatch_review_threads_branch_sha_into_claude_md_instruction(
    two_machine_config: Config,
) -> None:
    """#2818: dispatch_review's own captured review_head_sha must reach the
    dispatched briefing's CLAUDE.md section, not just the returned
    Assignment — this is the end-to-end path that kills the unclamped
    second copy of CLAUDE.md sitting in the immutable prefix.
    """
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "sha-review-2"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 9, "url": "https://github.com/acme/api/pull/9", "existed": True,
        },
        claude_md_reader=lambda p: "# CLAUDE.md\nDo not use raw SQL.",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        branch_sha_fetcher=lambda repo, branch: "deadbeef1234",
        diff_fetcher=lambda repo, num, **kw: None,
    )

    assert result is not None
    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert "git show deadbeef1234:CLAUDE.md" in payload["briefing"]
    assert "Do not use raw SQL." not in payload["briefing"]


def test_dispatch_review_tolerates_sha_fetch_failure(
    two_machine_config: Config,
) -> None:
    """#821: dispatch_review must not fail when the SHA fetcher raises."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "sha-fail-1"})

    def _failing_sha(repo, branch):
        raise RuntimeError("GitHub unavailable")

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 8, "url": "https://github.com/acme/api/pull/8", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        branch_sha_fetcher=_failing_sha,
    )

    # Dispatch must succeed; review_head_sha is None (unavailable is not blocking).
    assert result is not None
    assert result.review_head_sha is None


def test_dispatch_review_captures_patch_id(
    two_machine_config: Config,
) -> None:
    """#1475: dispatch_review must set review_patch_id on the returned
    Assignment, alongside review_head_sha, so has_approved_review can later
    carry the approval across a content-identical rebase."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "patchid-review-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 9, "url": "https://github.com/acme/api/pull/9", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        branch_sha_fetcher=lambda repo, branch: "deadbeef1234",
        patch_id_computer=lambda diff_text: "patchid-xyz",  # injected for test
    )

    assert result is not None
    assert result.review_patch_id == "patchid-xyz"


def test_dispatch_review_tolerates_patch_id_fetch_failure(
    two_machine_config: Config,
) -> None:
    """#1475: dispatch_review must not fail when the patch-id computer raises."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "patchid-fail-1"})

    def _failing_patch_id(diff_text):
        raise RuntimeError("git patch-id unavailable")

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 10, "url": "https://github.com/acme/api/pull/10", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        patch_id_computer=_failing_patch_id,
    )

    # Dispatch must succeed; review_patch_id is None (unavailable is not blocking).
    assert result is not None
    assert result.review_patch_id is None


def test_dispatch_review_patch_id_hashes_untruncated_diff(
    two_machine_config: Config,
) -> None:
    """#1475 blocking finding: review_patch_id must be computed from the full,
    untruncated diff — not the display-truncated text with a trailing
    "[diff truncated...]" note — so it matches the merge-time
    ``branch_patch_id`` (computed from an uncapped compare-API diff) for any
    PR whose diff exceeds the 60000-char display cap. The briefing shown to
    the reviewer must still get the truncated copy."""
    # A diff comfortably over the 60000-char display truncation threshold.
    big_diff = "diff --git a/f.py b/f.py\n" + ("+x\n" * 30000)
    assert len(big_diff) > 60000

    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "patchid-full-1"})
    captured: dict[str, str | None] = {}

    def _capture_patch_id(diff_text: str | None) -> str | None:
        captured["diff_text"] = diff_text
        return "patchid-full"

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 11, "url": "https://github.com/acme/api/pull/11", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        branch_sha_fetcher=lambda repo, branch: "deadbeef1234",
        patch_id_computer=_capture_patch_id,
        # #1484: inject the diff directly rather than monkeypatching the
        # `_gh` subprocess boundary — closes the live `gh pr diff` seam this
        # test used to reach through `github_ops.pr_diff`.
        diff_fetcher=lambda repo, num, **kw: big_diff,
    )

    assert result is not None
    assert result.review_patch_id == "patchid-full"
    # The hash input must be the *full* diff, byte-for-byte — no truncation.
    assert captured["diff_text"] == big_diff
    assert "[diff truncated" not in captured["diff_text"]

    # But the briefing embedded in the dispatched payload must be the
    # display-truncated copy, so a huge diff still can't blow the briefing.
    assert client.calls, "expected a dispatch POST"
    _, payload = client.calls[0]
    # #2819 follow-up: f.py is the *only* file and it alone exceeds the cap,
    # so the char-slice fallback fires and the note now also flags f.py
    # itself as cut off mid-diff (not just "truncated at N chars") — the
    # exact fix this test's own issue asked for.
    assert "[diff truncated at 60000 chars" in payload["briefing"]
    assert "cut off mid-diff (f.py)" in payload["briefing"]
    assert len(payload["briefing"]) < len(big_diff)


def test_dispatch_review_logs_missing_test_coverage_but_still_dispatches(
    two_machine_config: Config, caplog: pytest.LogCaptureFixture,
) -> None:
    """#2192: the free pre-review nudge must fire (logged) for a diff that
    touches user-visible source with zero test files, AND dispatch must
    proceed exactly as it would otherwise — the flag never blocks. A false
    positive here must never cost a round trip, so nothing about the
    dispatch outcome may depend on it.

    Deliberately does NOT use ``caplog.at_level("INFO", ...)`` — that would
    force-lower the logger's level for the duration of the test, which is
    exactly the kind of reconfiguration that doesn't reflect any real code
    path (#2192 review follow-up: the original version of this test passed
    for a `log.info` call that was a guaranteed no-op under this repo's real
    zero-config logging, because `caplog.at_level` masked it). The nudge is
    logged at `log.warning`, which pytest's caplog captures under its
    *default* level with no override needed — see
    ``test_missing_test_coverage_nudge_reaches_stderr_with_zero_logging_config``
    below for proof this also surfaces outside pytest entirely."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "notest-review-1"})
    diff = (
        "diff --git a/coord/dashboard/server.py b/coord/dashboard/server.py\n"
        "--- a/coord/dashboard/server.py\n"
        "+++ b/coord/dashboard/server.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+def new_endpoint():\n"
        "+    return 'ok'\n"
    )

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 21, "url": "https://github.com/acme/api/pull/21", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        diff_fetcher=lambda repo, num, **kw: diff,
    )

    # Dispatch proceeds exactly as normal: a review assignment is returned
    # and exactly one review POST goes out.
    assert result is not None
    assert len(client.calls) == 1

    # The nudge fired at WARNING — visible in the coordinator's own log, not
    # the reviewer's briefing (the reviewer's prompt is untouched by this
    # check).
    matching = [rec for rec in caplog.records if "missing test only" in rec.message]
    assert matching, caplog.text
    assert matching[0].levelname == "WARNING"
    _, payload = client.calls[0]
    assert "missing test only" not in payload["briefing"]


def test_dispatch_review_no_missing_test_log_when_test_file_included(
    two_machine_config: Config, caplog: pytest.LogCaptureFixture,
) -> None:
    """Same shape diff, but with a test file included — the nudge must stay
    silent, and dispatch must proceed identically either way."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "hastest-review-1"})
    diff = (
        "diff --git a/coord/dashboard/server.py b/coord/dashboard/server.py\n"
        "--- a/coord/dashboard/server.py\n"
        "+++ b/coord/dashboard/server.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+def new_endpoint():\n"
        "+    return 'ok'\n"
        "diff --git a/tests/test_server.py b/tests/test_server.py\n"
        "--- a/tests/test_server.py\n"
        "+++ b/tests/test_server.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+def test_new_endpoint():\n"
        "+    assert True\n"
    )

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 22, "url": "https://github.com/acme/api/pull/22", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        diff_fetcher=lambda repo, num, **kw: diff,
    )

    assert result is not None
    assert len(client.calls) == 1
    assert not any("missing test only" in rec.message for rec in caplog.records)


def test_missing_test_coverage_nudge_reaches_stderr_with_zero_logging_config() -> None:
    """#2192 review follow-up: proves the nudge is observable OUTSIDE pytest
    entirely, not just via `caplog` (which attaches its own handler and can
    mask a level choice that's actually a no-op in production — this repo
    has zero `logging.basicConfig`/`setLevel`/`addHandler`/`dictConfig`
    calls anywhere outside tests, so the root logger sits at Python's
    default WARNING floor with no handler attached; see
    coord/interactive.py:888-898's #865 note on the identical trap).

    Spawns a bare subprocess — no test harness, no fixtures, exactly the
    coordinator's real startup state — and logs on the same logger name
    `dispatch_review` uses (`coord.review`, i.e. `logging.getLogger(__name__)`
    in that module). `log.warning` must land on stderr via Python's
    handler-of-last-resort; `log.info` must produce nothing, confirming the
    level choice — not just the message text — is what makes this visible.
    """
    script = textwrap.dedent(
        """
        import logging
        log = logging.getLogger("coord.review")
        log.info("nudge-as-info-should-not-appear")
        log.warning("nudge-as-warning-should-appear")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
    )
    assert "nudge-as-warning-should-appear" in result.stderr
    assert "nudge-as-info-should-not-appear" not in result.stderr


def test_dispatch_review_handles_http_failure_gracefully(
    two_machine_config: Config,
) -> None:
    import httpx

    class _FailingClient:
        def post(self, url, *, json, timeout):
            raise httpx.ConnectError("agent unreachable")

    board = Board()
    result = dispatch_review(
        _completed_assignment(), board, two_machine_config,
        http_client=_FailingClient(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    assert board.active == []


def test_dispatch_review_falls_back_when_no_pr_can_be_opened(
    two_machine_config: Config,
) -> None:
    board = Board()
    completed = _completed_assignment()
    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "rev1"}),
        pr_lookup=lambda repo_github, **kw: None,  # PR open failed
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is not None
    # With no PR, the review_target is the branch name.
    assert result.review_target == "issue-1-fix"
    assert result.pr_url is None


def test_dispatch_review_records_to_dispatched_ledger(
    two_machine_config: Config, coord_db,
) -> None:
    from coord import state as state_mod

    board = Board()
    completed = _completed_assignment(machine="laptop")

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "review-ledger-1"}),
        pr_lookup=lambda repo_github, **kw: {
            "number": 99, "url": "https://github.com/acme/api/pull/99", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        # Branch check mocked: this test covers DB recording, not remote-branch detection.
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    records = state_mod.load_dispatched()
    assert len(records) == 1
    assert records[0]["assignment_id"] == "review-ledger-1"
    assert records[0]["repo_github"] == "acme/api"
    assert records[0]["machine_name"] == "server"


# ── #1476: scoped re-review ──────────────────────────────────────────────────


def test_compute_resolution_delta_shows_only_the_changed_hunk() -> None:
    old = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n+++ b/f.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-old line\n+kept line\n context\n"
    )
    new = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n+++ b/f.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-old line\n+resolved line\n context\n"
    )
    delta = compute_resolution_delta(old, new)
    assert delta is not None
    assert "resolved line" in delta
    assert "kept line" in delta


def test_compute_resolution_delta_none_when_old_missing() -> None:
    assert compute_resolution_delta(None, "some diff") is None
    assert compute_resolution_delta("", "some diff") is None
    assert compute_resolution_delta("   ", "some diff") is None


def test_compute_resolution_delta_none_when_new_missing() -> None:
    assert compute_resolution_delta("some diff", None) is None
    assert compute_resolution_delta("some diff", "") is None


def test_compute_resolution_delta_none_when_identical() -> None:
    text = "identical diff text\nline two\n"
    assert compute_resolution_delta(text, text) is None


def test_scoped_briefing_contains_delta_and_established_context_framing() -> None:
    briefing = build_scoped_review_briefing(
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Add feature",
        branch="issue-7-fix",
        resolution_delta="-old resolution\n+new resolution\n",
        default_branch="main",
    )
    assert "**already approved**" in briefing
    assert "do NOT need to re-review the whole PR" in briefing
    assert "-old resolution" in briefing
    assert "+new resolution" in briefing
    assert "REVIEW_VERDICT: approve" in briefing
    assert "END_REVIEW" in briefing
    assert "## Blocking findings" in briefing
    assert "## Non-blocking concerns" in briefing
    assert "## Nits" in briefing
    # #1457: same report-result-before-the-block contract as a full review.
    assert "coord report-result" in briefing
    assert "COORD_ASSIGNMENT_ID" in briefing
    assert "belt and braces" in briefing


def _scoped_entry(**overrides) -> QueuedMerge:
    defaults = dict(
        assignment_id="w1",
        repo_name="api",
        repo_github="acme/api",
        branch="issue-1-fix",
        target_branch="main",
        issue_number=1,
        issue_title="Fix the thing",
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
    )
    defaults.update(overrides)
    return QueuedMerge(**defaults)


def _scoped_prior_review(**overrides) -> Assignment:
    defaults = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix the thing",
        assignment_id="rev1",
        type="review",
        status="done",
        review_of_assignment_id="w1",
        review_verdict="approve",
        review_head_sha="oldsha",
        review_patch_id="patchid-old",
        dispatched_at=100.0,
    )
    defaults.update(overrides)
    return Assignment(**defaults)


def _scoped_diff_fetcher(repo: str, base: str, ref: str) -> str:
    if ref == "oldsha":
        return (
            "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-old\n+kept-from-before\n"
        )
    return "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-old\n+conflict-fix-resolution\n"


def test_dispatch_scoped_review_dispatches_with_delta_in_briefing(
    two_machine_config: Config,
) -> None:
    board = Board()
    entry = _scoped_entry()
    prior = _scoped_prior_review()
    client = _FakeHTTPClient({"id": "scoped-1"})

    result = dispatch_scoped_review(
        entry, prior, board, two_machine_config,
        http_client=client, now=999.0, diff_fetcher=_scoped_diff_fetcher,
    )

    assert result is not None
    assert result.type == "review"
    assert result.status == "running"
    assert result.assignment_id == "scoped-1"
    assert result.dispatched_at == 999.0
    assert board.active == [result]

    # #1476 audit trail.
    assert result.review_scoped is True
    assert result.review_scope_base_sha == "oldsha"
    # Same parent as the review being superseded — the existing fix/re-review
    # auto-loop keys off this exactly like a full review.
    assert result.review_of_assignment_id == "w1"

    _, payload = client.calls[0]
    assert payload["type"] == "review"
    assert "conflict-fix-resolution" in payload["briefing"]
    assert "kept-from-before" in payload["briefing"]


def test_dispatch_scoped_review_returns_none_when_delta_unavailable(
    two_machine_config: Config,
) -> None:
    board = Board()
    entry = _scoped_entry()
    prior = _scoped_prior_review()

    result = dispatch_scoped_review(
        entry, prior, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "scoped-1"}),
        diff_fetcher=lambda repo, base, ref: None,  # gh fetch failed both sides
    )
    assert result is None
    assert board.active == []


def test_dispatch_scoped_review_returns_none_when_reviews_disabled(repo: Repo) -> None:
    cfg = Config(
        repos=[repo],
        machines=[Machine(name="laptop", host="laptop.tail", capabilities=["python"],
                           repos=["api"], repo_paths={"api": "/work/api"})],
        reviews=ReviewsConfig(enabled=False, auto_dispatch=True),
    )
    board = Board()
    result = dispatch_scoped_review(
        _scoped_entry(), _scoped_prior_review(), board, cfg,
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert result is None


def test_dispatch_scoped_review_returns_none_without_prior_head_sha(
    two_machine_config: Config,
) -> None:
    """Fail closed — no SHA to diff from means no scope can be computed."""
    board = Board()
    prior = _scoped_prior_review(review_head_sha=None)
    result = dispatch_scoped_review(
        _scoped_entry(), prior, board, two_machine_config,
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert result is None


def test_dispatch_scoped_reviews_for_queue_dispatches_when_eligible(
    two_machine_config: Config,
) -> None:
    from coord import merge_queue as mq

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
    )
    prior = _scoped_prior_review()
    cf = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1,
        issue_title="[conflict-fix] t", assignment_id="cf1", type="conflict-fix",
        status="done", review_of_assignment_id="w1", dispatched_at=200.0,
    )
    board = Board(completed=[work, prior, cf])
    entry = _scoped_entry(state=mq.PENDING)
    entry.branch_head_sha = "newsha"
    entry.branch_patch_id = "patchid-new"  # differs from prior.review_patch_id

    client = _FakeHTTPClient({"id": "scoped-2"})
    dispatched = dispatch_scoped_reviews_for_queue(
        board, two_machine_config,
        queue_items=[entry],
        http_client=client,
        diff_fetcher=_scoped_diff_fetcher,
    )

    assert len(dispatched) == 1
    assert dispatched[0].review_scoped is True
    assert dispatched[0].review_of_assignment_id == "w1"


def test_dispatch_scoped_reviews_for_queue_falls_back_when_fix_round_intervened(
    two_machine_config: Config,
) -> None:
    """Guardrail: a work/fix commit (not just a conflict-fix) intervened
    since the approval — this path must not fire; a full review is needed."""
    from coord import merge_queue as mq

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
    )
    prior = _scoped_prior_review()
    cf = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1,
        issue_title="[conflict-fix] t", assignment_id="cf1", type="conflict-fix",
        status="done", review_of_assignment_id="w1", dispatched_at=200.0,
    )
    fix_work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1,
        issue_title="[fix-1] t", assignment_id="fix1", type="work", status="done",
        branch="issue-1-fix", review_of_assignment_id="w1", dispatched_at=250.0,
    )
    board = Board(completed=[work, prior, cf, fix_work])
    entry = _scoped_entry(state=mq.PENDING)
    entry.branch_head_sha = "newsha"
    entry.branch_patch_id = "patchid-new"

    dispatched = dispatch_scoped_reviews_for_queue(
        board, two_machine_config,
        queue_items=[entry],
        http_client=_FakeHTTPClient({"id": "scoped-3"}),
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert dispatched == []


def test_dispatch_scoped_reviews_for_queue_skips_when_already_approved(
    two_machine_config: Config,
) -> None:
    """A content-identical rebase (#1475) already carries the approval
    forward — nothing for the scoped path to do."""
    from coord import merge_queue as mq

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
    )
    prior = _scoped_prior_review(review_patch_id="patchid-same")
    board = Board(completed=[work, prior])
    entry = _scoped_entry(state=mq.PENDING)
    entry.branch_head_sha = "newsha"
    entry.branch_patch_id = "patchid-same"

    dispatched = dispatch_scoped_reviews_for_queue(
        board, two_machine_config,
        queue_items=[entry],
        http_client=_FakeHTTPClient({"id": "scoped-4"}),
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert dispatched == []


def test_dispatch_scoped_reviews_for_queue_dedupes_already_handled_entry(
    two_machine_config: Config,
) -> None:
    """A review already dispatched after the prior approval — scoped or
    full — means don't fire a second one for the same voided approval."""
    from coord import merge_queue as mq

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
    )
    prior = _scoped_prior_review()
    cf = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1,
        issue_title="[conflict-fix] t", assignment_id="cf1", type="conflict-fix",
        status="done", review_of_assignment_id="w1", dispatched_at=200.0,
    )
    already_dispatched_review = Assignment(
        machine_name="server", repo_name="api", issue_number=1,
        issue_title="[scoped-review] t", assignment_id="rev2", type="review",
        status="running", review_of_assignment_id="w1", dispatched_at=250.0,
    )
    board = Board(
        active=[already_dispatched_review], completed=[work, prior, cf],
    )
    entry = _scoped_entry(state=mq.PENDING)
    entry.branch_head_sha = "newsha"
    entry.branch_patch_id = "patchid-new"

    dispatched = dispatch_scoped_reviews_for_queue(
        board, two_machine_config,
        queue_items=[entry],
        http_client=_FakeHTTPClient({"id": "scoped-5"}),
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert dispatched == []


def test_dispatch_scoped_reviews_for_queue_request_changes_drives_normal_fix_loop(
    two_machine_config: Config,
) -> None:
    """The scoped review's Assignment shape (type='review', same
    review_of_assignment_id chain, standard REVIEW_VERDICT parsing) must be
    indistinguishable from a full review to every downstream consumer — a
    request-changes verdict on it drives the exact same auto_loop fix path.
    This asserts the dispatched shape carries everything auto_loop's
    request-changes handling (coord.auto_loop._dispatch_fix and friends)
    keys off, without needing to re-run the whole auto-loop machinery here.
    """
    from coord import merge_queue as mq

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
    )
    prior = _scoped_prior_review()
    cf = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1,
        issue_title="[conflict-fix] t", assignment_id="cf1", type="conflict-fix",
        status="done", review_of_assignment_id="w1", dispatched_at=200.0,
    )
    board = Board(completed=[work, prior, cf])
    entry = _scoped_entry(state=mq.PENDING)
    entry.branch_head_sha = "newsha"
    entry.branch_patch_id = "patchid-new"

    dispatched = dispatch_scoped_reviews_for_queue(
        board, two_machine_config,
        queue_items=[entry],
        http_client=_FakeHTTPClient({"id": "scoped-6"}),
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert len(dispatched) == 1
    scoped_review = dispatched[0]

    # Simulate the reviewer coming back with request-changes, the same way
    # notify.py would record it on any ordinary review assignment.
    scoped_review.review_verdict = "request-changes"
    scoped_review.status = "done"

    # coord.merge_queue.has_approved_review must see this exactly like a
    # normal request-changes review — i.e. still blocked.
    board.active.remove(scoped_review)
    board.completed.append(scoped_review)
    assert mq.has_approved_review(entry, board) is False
    # And auto_loop's chain-resolution finds it under the same parent work
    # id a full review would have used.
    assert scoped_review.review_of_assignment_id == "w1"


def test_dispatch_scoped_review_stays_independent_of_the_worker_machine(
    two_machine_config: Config,
) -> None:
    """#1476 fix: candidate ranking must exclude the machine that authored
    the branch under review (looked up via ``entry.assignment_id``), not the
    *prior reviewer's* machine. Here the original worker ran on "laptop" and
    the prior (now-voided) review ran on "server" — the opposite machine.
    Under the bug, "server" (the reviewer) was treated as the machine to
    avoid, so the ranked candidate list put "laptop" — the actual worker —
    first, and the scoped re-review would land right back on the machine
    that authored the conflict-fix resolution being judged.
    """
    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
    )
    prior = _scoped_prior_review(machine_name="server")
    board = Board(completed=[work, prior])
    entry = _scoped_entry()
    client = _FakeHTTPClient({"id": "scoped-indep"})

    result = dispatch_scoped_review(
        entry, prior, board, two_machine_config,
        http_client=client, diff_fetcher=_scoped_diff_fetcher,
    )

    assert result is not None
    # Must be dispatched to "server" (different from the worker's "laptop"),
    # not back onto "laptop" where the code under review was written.
    assert result.machine_name == "server"


def test_dispatch_scoped_review_falls_back_to_reviewer_machine_when_worker_not_found(
    two_machine_config: Config,
) -> None:
    """Defensive fallback: if the work assignment behind ``entry.assignment_id``
    can no longer be found on the board (e.g. pruned), ranking falls back to
    the prior reviewer's machine rather than crashing."""
    prior = _scoped_prior_review(machine_name="server")
    board = Board(completed=[prior])  # no "w1" work assignment on the board
    entry = _scoped_entry()
    client = _FakeHTTPClient({"id": "scoped-fallback"})

    result = dispatch_scoped_review(
        entry, prior, board, two_machine_config,
        http_client=client, diff_fetcher=_scoped_diff_fetcher,
    )

    assert result is not None  # still dispatches — doesn't crash or stall


def test_dispatch_scoped_review_respects_tos_gate(
    two_machine_config: Config,
) -> None:
    """#437 STRUCTURAL TOS-COMPLIANCE GATE: a repo/provider configured
    ``human_attended_only`` must never receive an auto-dispatched scoped
    review — mirrors the guard ``dispatch_review`` already applies."""
    def _raising_guard(**kwargs):
        raise ValueError("provider 'claude-pty' is human_attended_only")

    import coord.providers as providers_mod

    board = Board()
    entry = _scoped_entry()
    prior = _scoped_prior_review()

    saved = providers_mod.guard_unattended_dispatch
    providers_mod.guard_unattended_dispatch = _raising_guard
    try:
        result = dispatch_scoped_review(
            entry, prior, board, two_machine_config,
            http_client=_FakeHTTPClient({"id": "scoped-tos"}),
            diff_fetcher=_scoped_diff_fetcher,
        )
    finally:
        providers_mod.guard_unattended_dispatch = saved

    assert result is None
    assert board.active == []


def test_dispatch_scoped_review_respects_work_is_terminal(
    two_machine_config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#522 chokepoint: never dispatch a scoped review for an issue/PR
    that's already terminal (closed/merged) on GitHub."""
    monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)

    board = Board()
    entry = _scoped_entry()
    prior = _scoped_prior_review()

    result = dispatch_scoped_review(
        entry, prior, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "scoped-terminal"}),
        diff_fetcher=_scoped_diff_fetcher,
    )

    assert result is None
    assert board.active == []


def test_dispatch_scoped_reviews_for_queue_respects_flood_threshold(
    two_machine_config: Config,
) -> None:
    """Mirrors dispatch_pending_reviews's surge gate: more eligible entries
    than reviews.flood_threshold ⇒ refuse the whole pass, dispatch nothing."""
    from coord import merge_queue as mq

    cfg = replace(two_machine_config, reviews=ReviewsConfig(
        enabled=True, auto_dispatch=True, flood_threshold=1,
    ))

    entries = []
    board_completed = []
    for i in range(2):
        wid = f"w{i}"
        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=i, issue_title="t",
            assignment_id=wid, type="work", status="done", branch=f"issue-{i}-fix",
        )
        prior = _scoped_prior_review(
            assignment_id=f"rev{i}", review_of_assignment_id=wid, issue_number=i,
        )
        cf = Assignment(
            machine_name="laptop", repo_name="api", issue_number=i,
            issue_title="[conflict-fix] t", assignment_id=f"cf{i}", type="conflict-fix",
            status="done", review_of_assignment_id=wid, dispatched_at=200.0,
        )
        board_completed.extend([work, prior, cf])
        entry = _scoped_entry(
            assignment_id=wid, branch=f"issue-{i}-fix", issue_number=i,
        )
        entry.branch_head_sha = "newsha"
        entry.branch_patch_id = "patchid-new"
        entries.append(entry)

    board = Board(completed=board_completed)
    dispatched = dispatch_scoped_reviews_for_queue(
        board, cfg,
        queue_items=entries,
        http_client=_FakeHTTPClient({"id": "scoped-flood"}),
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert dispatched == []


def test_dispatch_scoped_reviews_for_queue_respects_per_pass_cap(
    two_machine_config: Config,
) -> None:
    """Mirrors dispatch_pending_reviews's per-pass cap: with more eligible
    entries than reviews.max_auto_dispatch_per_pass, only the cap's worth
    dispatch this pass — the rest stay pending for the next one."""
    from coord import merge_queue as mq

    cfg = replace(two_machine_config, reviews=ReviewsConfig(
        enabled=True, auto_dispatch=True, flood_threshold=0,
        max_auto_dispatch_per_pass=1,
    ))

    entries = []
    board_completed = []
    for i in range(2):
        wid = f"w{i}"
        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=i, issue_title="t",
            assignment_id=wid, type="work", status="done", branch=f"issue-{i}-fix",
        )
        prior = _scoped_prior_review(
            assignment_id=f"rev{i}", review_of_assignment_id=wid, issue_number=i,
        )
        cf = Assignment(
            machine_name="laptop", repo_name="api", issue_number=i,
            issue_title="[conflict-fix] t", assignment_id=f"cf{i}", type="conflict-fix",
            status="done", review_of_assignment_id=wid, dispatched_at=200.0,
        )
        board_completed.extend([work, prior, cf])
        entry = _scoped_entry(
            assignment_id=wid, branch=f"issue-{i}-fix", issue_number=i,
        )
        entry.branch_head_sha = "newsha"
        entry.branch_patch_id = "patchid-new"
        entries.append(entry)

    board = Board(completed=board_completed)
    dispatched = dispatch_scoped_reviews_for_queue(
        board, cfg,
        queue_items=entries,
        http_client=_FakeHTTPClient({"id": "scoped-cap"}),
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert len(dispatched) == 1


# ── #916: composed regression for the full rebase-bounce handoff ───────────
#
# Each piece below (has_approved_review/scan_approved_reviews's patch-id
# discriminator, evaluate_smoke_verdict's own separate patch-id discriminator,
# merge_gate_failures' reason wording, find_scoped_review_candidate /
# only_conflict_fix_since_review's eligibility walk, dispatch_scoped_reviews_
# for_queue's selection+dispatch, and process()'s merge decision) already has
# dedicated unit tests. Nothing until now drove all of them in sequence
# against the SAME evolving board — exactly the seam #2814 named as this
# repo's actual defect surface: every component behaves as designed and the
# COMPOSITION still misbehaves.

def test_rebase_bounce_composed_regression_non_trivial(
    two_machine_config: Config,
) -> None:
    """#916 non-trivial path: a rebase that resolves a real conflict changes
    the branch's patch-id. Walk the whole handoff in order — approval voided,
    smoke independently stale, merge gate refuses naming the staleness,
    scoped review selected + dispatched, then a fresh approval + fresh smoke
    verdict on the rebased patch-id clears the gate and merges.
    """
    from tests.test_merge_queue import FakeGh

    from coord import merge_queue as mq

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
        test_state="passed", test_head_sha="oldsha",
        test_patch_id="patchid-old", test_base_sha="main-sha",
    )
    prior_review = _scoped_prior_review()  # approve @ oldsha/patchid-old
    conflict_fix = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1,
        issue_title="[conflict-fix] t", assignment_id="cf1", type="conflict-fix",
        status="done", review_of_assignment_id="w1", dispatched_at=200.0,
    )
    board = Board(completed=[work, prior_review, conflict_fix])

    entry = _scoped_entry()
    entry.branch_head_sha = "newsha"           # rebase moved the head
    entry.branch_patch_id = "patchid-new"      # conflict resolution changed content
    entry.target_branch_head_sha = "main-sha"  # base unchanged — isolates the content check

    # 1. has_approved_review: the prior approval is voided by the
    #    content-changing rebase (patch-id discriminator #1).
    assert mq.has_approved_review(entry, board) is False

    # 2. evaluate_smoke_verdict: independently stale — a SECOND, separately
    #    implemented patch-id discriminator must agree.
    smoke = mq.evaluate_smoke_verdict(entry, board)
    assert smoke.ok is False
    assert smoke.kind == mq.SMOKE_STALE

    # 3. the merge gate refuses, and the reason names the staleness rather
    #    than reporting a generic block.
    failures = mq.merge_gate_failures(entry, two_machine_config, board)
    blocked_gates = {f.gate for f in failures}
    assert "review" in blocked_gates
    assert "smoke" in blocked_gates
    smoke_failure = next(f for f in failures if f.gate == "smoke")
    assert "stale" in smoke_failure.reason.lower()

    events = mq.process([entry], FakeGh(), config=two_machine_config, board=board)
    assert not any(e.kind == "merged" for e in events)
    assert entry.state != mq.MERGED

    # 4. the ordinary reconcile()/coord notify polling path selects this
    #    exact entry and dispatches a review scoped to the rebase delta.
    dispatched = dispatch_scoped_reviews_for_queue(
        board, two_machine_config,
        queue_items=[entry],
        http_client=_FakeHTTPClient({"id": "scoped-916"}),
        diff_fetcher=_scoped_diff_fetcher,
        branch_sha_fetcher=lambda repo, branch: "newsha",
        patch_id_computer=lambda diff_text: "patchid-new",
    )
    assert len(dispatched) == 1
    scoped_review = dispatched[0]
    assert scoped_review.review_scoped is True
    assert scoped_review.review_of_assignment_id == "w1"
    assert scoped_review.review_scope_base_sha == "oldsha"
    assert scoped_review in board.active

    # 5. a fresh approval lands on the rebased patch-id (coord report-result,
    #    simulated the same way the file's other scoped-review tests do)
    #    and a fresh smoke verdict is recorded against the same rebased
    #    content — the gate reads clear and the entry merges.
    scoped_review.status = "done"
    scoped_review.review_verdict = "approve"
    assert scoped_review.review_head_sha == "newsha"
    assert scoped_review.review_patch_id == "patchid-new"

    work.test_head_sha = "newsha"
    work.test_patch_id = "patchid-new"

    assert mq.has_approved_review(entry, board) is True
    assert mq.evaluate_smoke_verdict(entry, board).ok is True
    assert mq.merge_gate_failures(entry, two_machine_config, board) == []

    events = mq.process([entry], FakeGh(), config=two_machine_config, board=board)
    assert any(e.kind == "merged" for e in events)
    assert entry.state == mq.MERGED


def test_rebase_bounce_composed_regression_trivial(
    two_machine_config: Config,
) -> None:
    """#916 trivial-path companion: a clean replay moves the SHA but not the
    patch-id. The approval and smoke verdict both survive, no scoped review
    is ever dispatched, and the entry merges straight through."""
    from tests.test_merge_queue import FakeGh

    from coord import merge_queue as mq

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
        test_state="passed", test_head_sha="oldsha",
        test_patch_id="patchid-same", test_base_sha="main-sha",
    )
    review = _scoped_prior_review(review_patch_id="patchid-same")
    board = Board(completed=[work, review])

    entry = _scoped_entry()
    entry.branch_head_sha = "replayed-sha"     # SHA moved (clean rebase replay)
    entry.branch_patch_id = "patchid-same"     # content identical
    entry.target_branch_head_sha = "main-sha"  # base unchanged

    assert mq.has_approved_review(entry, board) is True
    assert mq.evaluate_smoke_verdict(entry, board).ok is True
    assert mq.merge_gate_failures(entry, two_machine_config, board) == []

    dispatched = dispatch_scoped_reviews_for_queue(
        board, two_machine_config,
        queue_items=[entry],
        http_client=_FakeHTTPClient({"id": "should-not-dispatch"}),
        diff_fetcher=_scoped_diff_fetcher,
    )
    assert dispatched == []  # nothing voided — nothing to scope a review around
    assert board.active == []

    events = mq.process([entry], FakeGh(), config=two_machine_config, board=board)
    assert any(e.kind == "merged" for e in events)
    assert entry.state == mq.MERGED


def test_find_scoped_review_candidate_picks_most_recently_dispatched_approval() -> None:
    """Non-blocking finding: when more than one approved review exists in
    the work chain, the most-recently-dispatched one should be picked as the
    diff base — not just the first one encountered while walking the
    (unsorted) completed+active pool."""
    from coord import merge_queue as mq

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done", branch="issue-1-fix",
    )
    older_review = _scoped_prior_review(
        assignment_id="rev-old", review_head_sha="sha-old",
        review_patch_id="patchid-old", dispatched_at=100.0,
    )
    newer_review = _scoped_prior_review(
        assignment_id="rev-new", review_head_sha="sha-new",
        review_patch_id="patchid-new", dispatched_at=300.0,
    )
    # Deliberately append the older one last, so a naive "first match while
    # walking the pool" bug would still (accidentally) get this right unless
    # we also test pool order — put newer_review first in the list to prove
    # the pick is by dispatched_at, not list position.
    board = Board(completed=[work, newer_review, older_review])
    entry = _scoped_entry()
    entry.branch_head_sha = "currentsha"
    entry.branch_patch_id = "patchid-current"

    candidate = mq.find_scoped_review_candidate(entry, board)
    assert candidate is not None
    assert candidate.assignment_id == "rev-new"


# ── _find_or_open_pr — PR body carries closing keyword (#287) ───────────────


def test_find_or_open_pr_body_includes_closes_keyword() -> None:
    """_find_or_open_pr must prepend 'Closes #{issue_number}' so GitHub
    auto-closes the linked issue when the PR is merged (#287).  Without
    it the issue stays stranded open and the coordinator brain keeps
    re-syncing it as state=open.
    """
    from coord.review import _find_or_open_pr
    import coord.github_ops as github_ops_mod

    captured: dict = {}

    def _fake_find_pr(repo_github, branch):
        return None  # no existing PR → trigger create_pr path

    def _fake_create_pr(repo_github, *, base, head, title, body):
        captured["body"] = body
        return {"number": 55, "url": "https://github.com/acme/api/pull/55", "existed": False}

    import unittest.mock as mock
    with (
        mock.patch.object(github_ops_mod, "find_pr_for_branch", _fake_find_pr),
        mock.patch.object(github_ops_mod, "create_pr", _fake_create_pr),
    ):
        result = _find_or_open_pr(
            "acme/api",
            branch="issue-42-fix",
            default_branch="main",
            issue_number=42,
            issue_title="Fix the login bug",
        )

    assert result is not None
    assert result["number"] == 55
    assert "Closes #42" in captured["body"]
    # The closing keyword must come at the very start so GitHub parses it.
    assert captured["body"].startswith("Closes #42\n\n")


def test_find_or_open_pr_uses_refs_for_mock_author() -> None:
    """#1077: a mock-author (Gate A) PR's issue_number is the milestone's
    tracking issue, not something this PR resolves — the body must use the
    non-closing 'Refs #N' so merging doesn't auto-close the tracking issue.
    """
    from coord.review import _find_or_open_pr
    import coord.github_ops as github_ops_mod

    captured: dict = {}

    def _fake_find_pr(repo_github, branch):
        return None

    def _fake_create_pr(repo_github, *, base, head, title, body):
        captured["body"] = body
        return {"number": 56, "url": "https://github.com/acme/api/pull/56", "existed": False}

    import unittest.mock as mock
    with (
        mock.patch.object(github_ops_mod, "find_pr_for_branch", _fake_find_pr),
        mock.patch.object(github_ops_mod, "create_pr", _fake_create_pr),
    ):
        result = _find_or_open_pr(
            "acme/api",
            branch="ms-33-gate-a",
            default_branch="main",
            issue_number=1041,
            issue_title="Milestone #33 tracking issue",
            assignment_type="mock-author",
        )

    assert result is not None
    assert captured["body"].startswith("Refs #1041\n\n")
    assert "Closes #1041" not in captured["body"]


def test_dispatch_review_passes_assignment_type_to_pr_lookup(
    two_machine_config: Config,
) -> None:
    """#1077: dispatch_review must forward the completed assignment's type
    so pr_lookup (``_find_or_open_pr``) can decide the Closes-vs-Refs
    keyword — otherwise a mock-author PR's body would always default to the
    closing form and merging it would wrongly close the tracking issue."""
    board = Board()
    captured: dict = {}
    completed = replace(
        _completed_assignment(),
        type="mock-author",
        assignment_id="ga-1",
        branch="ms-33-gate-a",
        issue_number=1041,
    )
    client = _FakeHTTPClient({"id": "review-id-ga"})

    def _pr_lookup(repo_github, **kw):
        captured.update(kw)
        return {"number": 1, "url": "u", "existed": True}

    dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=_pr_lookup,
        claude_md_reader=lambda p: "# Project rules\n",
        issue_body_fetcher=lambda repo, num: "issue body text",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert captured.get("assignment_type") == "mock-author"


# ── open_pr_for_completed_work / dispatch_pending_pr_opens (#2844) ──────────
#
# #2844: the PR used to open at review-dispatch time — after the ~20-minute
# smoke leg finished — serialising CI onto the end of the pipeline instead of
# overlapping it. These cover the new early-open path that fires the moment
# a work leg pushes its branch.


def test_open_pr_for_completed_work_opens_immediately_after_work_done(
    two_machine_config: Config,
) -> None:
    """A `status="done"` work row with a branch gets its PR opened right
    away — not gated on any test/review state — and the URL is cached on
    the assignment."""
    from coord.review import open_pr_for_completed_work

    completed = _completed_assignment()
    calls: list[dict] = []

    def _pr_lookup(repo_github, **kw):
        calls.append(kw)
        return {"number": 7, "url": "https://github.com/acme/api/pull/7", "existed": False}

    pr = open_pr_for_completed_work(
        completed, two_machine_config,
        pr_lookup=_pr_lookup,
        commits_ahead_checker=lambda repo, base, branch: 3,
    )

    assert pr == {"number": 7, "url": "https://github.com/acme/api/pull/7", "existed": False}
    assert completed.pr_url == "https://github.com/acme/api/pull/7"
    assert len(calls) == 1
    assert calls[0]["branch"] == "issue-1-fix"
    assert calls[0]["assignment_type"] == "work"


def test_open_pr_for_completed_work_skips_zero_commit_branch(
    two_machine_config: Config,
) -> None:
    """#1534's zero-commit gate applies to the early open too — an empty
    branch must never get a PR, early or late."""
    from coord.review import open_pr_for_completed_work

    completed = _completed_assignment()
    calls: list[dict] = []

    def _pr_lookup(repo_github, **kw):
        calls.append(kw)
        return {"number": 7, "url": "u", "existed": False}

    pr = open_pr_for_completed_work(
        completed, two_machine_config,
        pr_lookup=_pr_lookup,
        commits_ahead_checker=lambda repo, base, branch: 0,
    )

    assert pr is None
    assert completed.pr_url is None
    assert calls == []


def test_dispatch_pending_pr_opens_opens_for_done_work_with_branch(
    two_machine_config: Config,
) -> None:
    """The bulk pass finds every eligible done/work-like/branched row and
    opens (or finds) a PR for each, recording pr_url on the row."""
    from coord.review import dispatch_pending_pr_opens

    completed = _completed_assignment()
    board = Board(completed=[completed])

    def _pr_lookup(repo_github, **kw):
        return {"number": 9, "url": "https://github.com/acme/api/pull/9", "existed": False}

    opened = dispatch_pending_pr_opens(
        board, two_machine_config,
        pr_lookup=_pr_lookup,
        commits_ahead_checker=lambda repo, base, branch: 1,
    )

    assert opened == [completed]
    assert completed.pr_url == "https://github.com/acme/api/pull/9"


def test_dispatch_pending_pr_opens_skips_rows_that_already_have_a_pr(
    two_machine_config: Config,
) -> None:
    """A row that already carries pr_url is skipped — no redundant lookup."""
    from coord.review import dispatch_pending_pr_opens

    completed = replace(_completed_assignment(), pr_url="https://github.com/acme/api/pull/1")
    board = Board(completed=[completed])
    calls = {"n": 0}

    def _pr_lookup(repo_github, **kw):
        calls["n"] += 1
        return {"number": 1, "url": "u", "existed": True}

    opened = dispatch_pending_pr_opens(board, two_machine_config, pr_lookup=_pr_lookup)

    assert opened == []
    assert calls["n"] == 0


def test_dispatch_pending_pr_opens_skips_failed_and_non_done_rows(
    two_machine_config: Config,
) -> None:
    """A failed work leg must never get an early-opened orphan PR (#2844's
    'failed work legs' requirement) — only status='done' rows qualify."""
    from coord.review import dispatch_pending_pr_opens

    failed = replace(_completed_assignment(), status="failed")
    running_like = replace(_completed_assignment(), status="running")
    no_branch = replace(_completed_assignment(), branch=None)
    board = Board(completed=[failed, running_like, no_branch])
    calls = {"n": 0}

    def _pr_lookup(repo_github, **kw):
        calls["n"] += 1
        return {"number": 1, "url": "u", "existed": True}

    opened = dispatch_pending_pr_opens(board, two_machine_config, pr_lookup=_pr_lookup)

    assert opened == []
    assert calls["n"] == 0


def test_dispatch_pending_pr_opens_skips_terminal_rows(
    two_machine_config: Config, monkeypatch,
) -> None:
    """#522-style chokepoint: never open a PR for work GitHub already
    considers finished (issue closed / PR merged)."""
    from coord.review import dispatch_pending_pr_opens

    monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
    completed = _completed_assignment()
    board = Board(completed=[completed])
    calls = {"n": 0}

    def _pr_lookup(repo_github, **kw):
        calls["n"] += 1
        return {"number": 1, "url": "u", "existed": True}

    opened = dispatch_pending_pr_opens(board, two_machine_config, pr_lookup=_pr_lookup)

    assert opened == []
    assert calls["n"] == 0


def test_dispatch_pending_pr_opens_off_when_reviews_disabled(repo: Repo) -> None:
    """Gated on reviews.enabled/auto_dispatch — this path exists purely to
    feed dispatch_review's own PR lookup earlier, so it's a no-op when
    reviews are off entirely."""
    from coord.review import dispatch_pending_pr_opens

    cfg = Config(
        repos=[repo],
        machines=[],
        reviews=ReviewsConfig(enabled=False, auto_dispatch=False),
    )
    completed = _completed_assignment()
    board = Board(completed=[completed])
    calls = {"n": 0}

    def _pr_lookup(repo_github, **kw):
        calls["n"] += 1
        return {"number": 1, "url": "u", "existed": True}

    opened = dispatch_pending_pr_opens(board, cfg, pr_lookup=_pr_lookup)

    assert opened == []
    assert calls["n"] == 0


def test_dispatch_pending_pr_opens_is_idempotent_with_dispatch_review(
    two_machine_config: Config,
) -> None:
    """Opening early and then reaching dispatch_review later must not create
    a second PR — both look the branch up fresh and get the same one back."""
    from coord.review import dispatch_pending_pr_opens

    completed = replace(_completed_assignment(), review_state="pending")
    board = Board(completed=[completed])
    create_calls = {"n": 0}

    def _find_or_create(repo_github, **kw):
        # Simulate GitHub: first call creates, every later call finds it.
        create_calls["n"] += 1
        existed = create_calls["n"] > 1
        return {"number": 123, "url": "https://github.com/acme/api/pull/123", "existed": existed}

    opened = dispatch_pending_pr_opens(
        board, two_machine_config, pr_lookup=_find_or_create,
    )
    assert opened == [completed]
    assert create_calls["n"] == 1

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "review-id-late"}),
        pr_lookup=_find_or_create,
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    assert create_calls["n"] == 2, "dispatch_review must reuse the same PR, not create a new one"


# ── Reviewer system prompt ──────────────────────────────────────────────────


def test_reviewer_system_prompt_does_not_allow_gh_commands() -> None:
    """Workers must not call gh — coordinator posts the review for them."""
    assert "gh pr review" not in REVIEWER_SYSTEM_PROMPT
    assert "NOT allowed to run any `gh` commands" in REVIEWER_SYSTEM_PROMPT


def test_reviewer_system_prompt_instructs_structured_output() -> None:
    assert "REVIEW_VERDICT:" in REVIEWER_SYSTEM_PROMPT
    assert "REVIEW_BODY:" in REVIEWER_SYSTEM_PROMPT
    assert "END_REVIEW" in REVIEWER_SYSTEM_PROMPT


def test_reviewer_system_prompt_instructs_report_result_as_required_primary() -> None:
    """#1457: `report-result` never appeared in REVIEWER_SYSTEM_PROMPT — the
    only verdict-recording instruction the agent actually reads was "print
    the REVIEW_VERDICT block", even though the #606 PATH-fix (coord/
    interactive.py:_with_coord_on_path) exists specifically to make `coord
    report-result` reachable and calls it the reviewer's "PREFERRED
    self-report path". The prompt must now instruct the agent to run
    `coord report-result` (keyed off `$COORD_ASSIGNMENT_ID`) BEFORE printing
    the REVIEW_VERDICT block, with the block kept as a REQUIRED backup, not
    an optional one."""
    assert "coord report-result" in REVIEWER_SYSTEM_PROMPT
    assert "COORD_ASSIGNMENT_ID" in REVIEWER_SYSTEM_PROMPT
    assert "belt and braces" in REVIEWER_SYSTEM_PROMPT
    # The block stays REQUIRED — it is not downgraded now that report-result
    # is also asked for.
    assert "REQUIRED" in REVIEWER_SYSTEM_PROMPT


def test_reviewer_system_prompt_states_end_review_as_hard_requirement() -> None:
    """#1427: 4% of emitted verdicts omitted `END_REVIEW` — a reviewer that
    writes a complete, correct review and simply stops has its verdict
    silently dropped (both measured occurrences were `approve`, the worst
    shape: nothing is wrong and nothing says so). The prompt previously
    showed `END_REVIEW` only inside a format example; it must also state,
    as an explicit instruction (not just example text), that the terminator
    is mandatory and that an otherwise-complete review without it is
    discarded — the actual failure mode being a model that finishes its
    prose and stops one line early."""
    assert "HARD REQUIREMENT" in REVIEWER_SYSTEM_PROMPT
    assert "discarded in its entirety" in REVIEWER_SYSTEM_PROMPT
    assert "last line" in REVIEWER_SYSTEM_PROMPT.lower()


def test_reviewer_system_prompt_forbids_running_the_test_suite() -> None:
    """A reviewer reads the diff; it must NOT run the test suite. Running it
    on a headless GUI project (e.g. vimcode) hangs the session, and build/test
    is the separate pre-merge smoke gate's job. Regression for that hang."""
    assert "DO NOT run the project's test suite" in REVIEWER_SYSTEM_PROMPT
    # the old mandate must be gone
    assert "Run the test suite" not in REVIEWER_SYSTEM_PROMPT
    assert "allowed to run the project's test suite" not in REVIEWER_SYSTEM_PROMPT


# ── Briefing: structured output instructions ────────────────────────────────


def test_briefing_does_not_contain_gh_pr_review_command() -> None:
    """The briefing must not tell the reviewer to call gh pr review."""
    briefing = build_review_briefing(
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="",
        branch="issue-7-fix-login",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True),
        repo_claude_md=None,
    )
    assert "gh pr review" not in briefing


def test_briefing_contains_structured_output_instructions() -> None:
    """The briefing must contain the REVIEW_VERDICT / REVIEW_BODY / END_REVIEW instructions."""
    briefing = build_review_briefing(
        pr_number=42,
        pr_url=None,
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="",
        branch="issue-7",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True),
        repo_claude_md=None,
    )
    assert "REVIEW_VERDICT: approve" in briefing
    assert "REVIEW_BODY:" in briefing
    assert "END_REVIEW" in briefing
    assert "do NOT run any `gh` commands" in briefing


def test_briefing_forbids_markdown_decoration_on_markers() -> None:
    """#1346: the briefing must state the marker lines are a machine contract.

    The block is shown inside a Markdown briefing whose body placeholder
    invites Markdown, and reviewers duly emitted
    `**REVIEW_VERDICT: request-changes**` — a complete review the parser
    rejected. PR #1347 made the parser tolerant; this is the other half, at
    the source. The negative example is the part that actually stops the
    drift, so assert it is present verbatim.
    """
    briefing = build_review_briefing(
        pr_number=42,
        pr_url=None,
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="",
        branch="issue-7",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True),
        repo_claude_md=None,
    )
    assert "`**REVIEW_VERDICT: request-changes**` is WRONG" in briefing
    assert "no `**bold**`" in briefing
    # The BODY must stay explicitly Markdown-friendly — the constraint is on
    # the marker lines only, and over-reading it would cost review quality.
    assert "may be Markdown" in briefing


# ── parse_review_from_log ───────────────────────────────────────────────────


def _write_plain_log(path: Path, content: str) -> Path:
    """Write a plain-text log file."""
    path.write_text(content, encoding="utf-8")
    return path


def _write_stream_json_log(path: Path, assistant_texts: list[str]) -> Path:
    """Write a stream-json format log with assistant messages."""
    lines = []
    for text in assistant_texts:
        event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": text}]
            }
        }
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestParseReviewFromLog:
    def test_plain_text_approve(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
I reviewed the diff carefully.

REVIEW_VERDICT: approve
REVIEW_BODY:
The implementation looks correct. Tests pass.
No CLAUDE.md violations found.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"
        assert "Tests pass." in result.body

    def test_plain_text_request_changes(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: request-changes
REVIEW_BODY:
## Issues found

- `src/auth.py:42` — missing input validation
- Tests do not cover the error path
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "missing input validation" in result.body

    def test_plain_text_last_block_wins(self, tmp_path: Path) -> None:
        """When multiple blocks exist, the last one is used."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: approve
REVIEW_BODY:
First pass — looks OK.
END_REVIEW

Actually I missed something...

REVIEW_VERDICT: request-changes
REVIEW_BODY:
Found a critical bug at line 42.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "critical bug" in result.body

    def test_plain_text_no_review_body_marker(self, tmp_path: Path) -> None:
        """#608: reviewer omits the `REVIEW_BODY:` line and writes Markdown
        findings directly after the verdict. The body must still be captured
        (this is the exact shape that stranded the #607 review)."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: request-changes

#### 1. Out-of-scope removal

`tui/src/app.rs` deletes `session_pane_live`.

**Must be restored.**

### Fix instructions

Revert the out-of-scope removals.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "session_pane_live" in result.body
        assert "Fix instructions" in result.body
        # The optional marker must not leak into the captured body.
        assert "REVIEW_BODY:" not in result.body

    def test_stream_json_no_review_body_marker(self, tmp_path: Path) -> None:
        """#608: same markers-only shape, but in stream-json transcript form."""
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "Reviewing the diff...",
            "REVIEW_VERDICT: request-changes\n\n## Findings\n\nBug at auth.py:10.\nEND_REVIEW",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Bug at auth.py:10." in result.body

    def test_markdown_bolded_markers(self, tmp_path: Path) -> None:
        """#1346: the reviewer wraps the markers in Markdown emphasis.

        This is the exact shape that dropped the #873 review: a complete,
        correct block with a valid `END_REVIEW` terminator, but written as
        `**REVIEW_VERDICT: request-changes**` / `**REVIEW_BODY:**`. The single
        pair of trailing asterisks made the whole block unparseable, so the
        verdict never reached the board and the operator got a blank prompt.
        """
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
Now I have everything needed. Let me finalize the review.

**REVIEW_VERDICT: request-changes**

**REVIEW_BODY:**

## Summary

Solid implementation, but one blocking bug.

## Blocking

### 1. `repo_name` stored inconsistently

`coord/state.py:3290` uses the slug on one path and the config key on the
other, so the mirror is not queryable.

END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert result.body.startswith("## Summary")
        assert "repo_name` stored inconsistently" in result.body
        # Neither marker may leak into the captured body.
        assert "REVIEW_BODY" not in result.body
        assert "END_REVIEW" not in result.body

    def test_markdown_bolded_verdict_value_only(self, tmp_path: Path) -> None:
        """#1346: emphasis around the value alone, markers left bare."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: **approve**
REVIEW_BODY:
Clean diff, tests pass.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"
        assert result.body == "Clean diff, tests pass."

    def test_markdown_decorated_markers_stream_json(self, tmp_path: Path) -> None:
        """#1346: bolded markers in transcript (stream-json) form — the path the
        #606 transcript-floor actually reads for a human-attended review."""
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "Reading the diff...",
            "**REVIEW_VERDICT: request-changes**\n\n"
            "**REVIEW_BODY:**\n\n"
            "## Blocking\n\n- `auth.py:42` — missing validation\n\n"
            "**END_REVIEW**",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "missing validation" in result.body
        assert "REVIEW_VERDICT" not in result.body

    def test_markdown_heading_and_code_span_markers(self, tmp_path: Path) -> None:
        """#1346: heading / code-span decoration is tolerated too."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
## REVIEW_VERDICT: request-changes
`REVIEW_BODY:`
Blocker at `cli.py:10`.
## END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert result.body == "Blocker at `cli.py:10`."

    def test_decoration_tolerance_still_requires_terminator(
        self, tmp_path: Path
    ) -> None:
        """#1346 must not widen what counts as a review: a decorated verdict
        line in prose with no `END_REVIEW` terminator is still not a review."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
I'll finish by emitting **REVIEW_VERDICT: approve** once I'm done reading,
but I still have three files to go.
""")
        assert parse_review_from_log(log) is None

    def test_complete_body_without_terminator_returns_none(
        self, tmp_path: Path
    ) -> None:
        """#1427 canonical specimen (efc198d6475a.log): a COMPLETE, correct
        `REVIEW_VERDICT: approve` + `REVIEW_BODY:` that ends its prose
        naturally and simply stops — no `END_REVIEW` anywhere. This is a
        different shape from `test_decoration_tolerance_still_requires_terminator`
        above (mid-sentence prose that never reached a verdict format at all):
        here the reviewer wrote a full, well-formed review and only omitted
        the four-character terminator. Both must be rejected identically —
        refusing an unterminated block is correct, because there is no way to
        distinguish "finished, forgot the terminator" from "truncated
        mid-body" without it. Recording either as authoritative would be
        worse than recording neither."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: approve
REVIEW_BODY:

Reviewed the diff against the checklist. Tests cover the new code path,
error handling matches project conventions, and the change stays within
the files listed in the issue.

No test-coverage gaps, scope violations, or security issues found. Approving.
""")
        assert parse_review_from_log(log) is None

    def test_last_block_wins_without_marker(self, tmp_path: Path) -> None:
        """The optional-marker change must not break 'last block wins' when
        neither block uses the `REVIEW_BODY:` header."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: approve
First pass looks fine.
END_REVIEW

On reflection:

REVIEW_VERDICT: request-changes
Found a blocker at line 42.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "blocker at line 42" in result.body
        assert "First pass" not in result.body

    def test_stream_json_approve(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "I'm reading the diff now...",
            "The tests look good.\n\nREVIEW_VERDICT: approve\nREVIEW_BODY:\nLGTM — clean diff, tests pass.\nEND_REVIEW",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"
        assert "LGTM" in result.body

    def test_stream_json_request_changes(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "Let me check the diff...",
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nSecurity issue at auth.py:10.\nEND_REVIEW",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Security issue" in result.body

    def test_stream_json_last_assistant_message_wins(self, tmp_path: Path) -> None:
        """The last assistant message containing the block is used."""
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "REVIEW_VERDICT: approve\nREVIEW_BODY:\nInitially approved.\nEND_REVIEW",
            "Wait, I found a bug.\nREVIEW_VERDICT: request-changes\nREVIEW_BODY:\nBug at line 7.\nEND_REVIEW",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Bug at line 7" in result.body

    def test_strict_parse_decodes_stream_json_when_heuristic_misses(
        self, tmp_path: Path
    ) -> None:
        """#1348 round 3 regression, shaped after the real failing log
        (``~/.coord/logs/efc198d6475a.log``).

        Round 2 (a3f9454) fixed ``detect_unparsed_review_marker`` — the
        DIAGNOSTIC — to decode NDJSON before matching. But that diagnostic
        only ever runs after the STRICT parser (``parse_review_from_log`` /
        ``_parse_review_text``) has already returned ``None``, and on the
        real log the strict parser was the one dropping a perfectly
        well-formed ``REVIEW_VERDICT: approve ... END_REVIEW`` block.

        Root cause: a real reviewer log can carry a first physical line that
        is neither blank, nor a ``#``-prefixed agent header, nor JSON (e.g.
        CLI startup banner text) before the actual
        ``--output-format stream-json`` NDJSON stream begins.
        ``is_stream_json`` only inspects that first non-blank/non-comment
        line, so it reports ``False`` for a log that is otherwise entirely
        legitimate NDJSON — and ``parse_review_from_log`` then fell back to
        matching ``_REVIEW_BLOCK_RE`` against the RAW, undecoded file text,
        where every real newline inside the reviewer's message is stored as
        the literal two-character escape ``\\n`` (correct JSON encoding),
        which ``[\\r\\n]+`` can never match unescaped.

        The fix (this commit) makes ``_parse_review_text`` run
        ``_decode_transcript_for_diagnostic`` before ``_REVIEW_BLOCK_RE``
        regardless of which branch called it, so this no longer depends on
        ``is_stream_json`` guessing right. The grammar itself is unchanged:
        ``END_REVIEW`` is still mandatory and the LAST match still wins — a
        non-JSON ``# argv=...`` header line embedding the reviewer's own
        system-prompt TEMPLATE (which contains the literal placeholder
        ``REVIEW_VERDICT: approve ... <your full review text in markdown>
        ... END_REVIEW``) must never leak through as the reported verdict.
        """
        from coord.worker_events import is_stream_json

        real_review_text = (
            "Reviewing the diff now.\n\n"
            "REVIEW_VERDICT: approve\n"
            "REVIEW_BODY:\n"
            "Clean diff, tests pass, no CLAUDE.md violations. This closes #1400.\n"
            "END_REVIEW"
        )
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": real_review_text}]},
        }
        header_argv = (
            "claude -p --output-format stream-json --system-prompt "
            + REVIEWER_SYSTEM_PROMPT.replace("\n", "\\n")
            + " --model sonnet"
        )
        log_text = (
            "Claude Code v1.2.3 starting up...\n"  # non-JSON, non-comment preamble
            f"# agent=elitebook repo=coord issue=#1400 argv={header_argv}\n"
            + json.dumps(event)
            + "\n"
        )
        log = tmp_path / "review.log"
        log.write_text(log_text, encoding="utf-8")

        # Confirm the premise: is_stream_json's heuristic really does miss
        # this shape, so this test fails loudly (not silently) if that
        # heuristic is ever changed to no longer reproduce the bug.
        assert is_stream_json(log) is False

        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"
        assert "This closes #1400" in result.body
        assert "<your full review text in markdown>" not in result.body

    def test_not_found_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_plain_log(log, "I reviewed the diff. It looks fine.\n")
        result = parse_review_from_log(log)
        assert result is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = parse_review_from_log(tmp_path / "nonexistent.log")
        assert result is None

    def test_stream_json_no_review_block_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "I read the diff.",
            "The code looks okay but I forgot to output my verdict.",
        ])
        result = parse_review_from_log(log)
        assert result is None

    def test_case_insensitive_verdict(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: Approve
REVIEW_BODY:
Looks good to me.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"  # normalised to lowercase

    def test_multiline_body_preserved(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        body_text = "## Summary\n\nLine 1.\nLine 2.\n\n### Details\n\n- Point A\n- Point B"
        _write_plain_log(log, f"REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n{body_text}\nEND_REVIEW\n")
        result = parse_review_from_log(log)
        assert result is not None
        assert "Line 1." in result.body
        assert "Point B" in result.body

    def test_pass_alias_maps_to_approve(self, tmp_path: Path) -> None:
        """PASS is accepted as an alias for approve."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: PASS
REVIEW_BODY:
All checks pass. Clean diff.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"
        assert "All checks pass." in result.body

    def test_fail_alias_maps_to_request_changes(self, tmp_path: Path) -> None:
        """FAIL is accepted as an alias for request-changes."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: FAIL
REVIEW_BODY:
Security issue at auth.py:10.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Security issue" in result.body

    def test_pass_alias_case_insensitive(self, tmp_path: Path) -> None:
        """'pass' in any case is normalized to 'approve'."""
        log = tmp_path / "review.log"
        _write_plain_log(log, "REVIEW_VERDICT: Pass\nREVIEW_BODY:\nOK.\nEND_REVIEW\n")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"

    def test_fail_alias_case_insensitive(self, tmp_path: Path) -> None:
        """'fail' in any case is normalized to 'request-changes'."""
        log = tmp_path / "review.log"
        _write_plain_log(log, "REVIEW_VERDICT: Fail\nREVIEW_BODY:\nProblems found.\nEND_REVIEW\n")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"


class TestParseReviewFromAgent:
    """Cover the HTTP-fetch path used when the worker's log file lives on a
    remote agent and notify can't open it directly.
    """

    def test_fetches_log_via_agent_and_parses_verdict(self, monkeypatch) -> None:
        """Plain-text log served by the agent → verdict extracted."""
        from coord.review import parse_review_from_agent

        body = (
            "REVIEW_VERDICT: approve\n"
            "REVIEW_BODY:\n"
            "Diff looks clean.\n"
            "END_REVIEW\n"
        )

        class FakeResponse:
            text = body
            def raise_for_status(self): pass

        def fake_get(url, timeout):
            assert url == "http://elitebook:7433/logs/abc123"
            return FakeResponse()

        monkeypatch.setattr("coord.review.httpx.get", fake_get)
        result = parse_review_from_agent("elitebook", "abc123")
        assert result is not None
        assert result.verdict == "approve"
        assert "Diff looks clean" in result.body

    def test_stream_json_log_from_agent(self, monkeypatch) -> None:
        """Stream-json log fetched over HTTP → verdict still extracted."""
        from coord.review import parse_review_from_agent
        import json

        assistant_text = (
            "Reviewing...\n\n"
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "Missing test coverage on the auth path.\n"
            "END_REVIEW"
        )
        body = (
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": assistant_text}]},
            }) + "\n"
        )

        class FakeResponse:
            text = body
            def raise_for_status(self): pass

        def fake_get(url, timeout):
            return FakeResponse()

        monkeypatch.setattr("coord.review.httpx.get", fake_get)
        result = parse_review_from_agent("dellserver", "xyz789")
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Missing test coverage" in result.body

    def test_returns_none_on_http_error(self, monkeypatch) -> None:
        """Agent unreachable → None (caller falls back gracefully)."""
        from coord.review import parse_review_from_agent
        import httpx

        def fake_get(url, timeout):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr("coord.review.httpx.get", fake_get)
        assert parse_review_from_agent("offline-host", "any") is None

    def test_returns_none_on_empty_log(self, monkeypatch) -> None:
        """Agent returns an empty body → None."""
        from coord.review import parse_review_from_agent

        class FakeResponse:
            text = ""
            def raise_for_status(self): pass

        monkeypatch.setattr("coord.review.httpx.get", lambda *a, **kw: FakeResponse())
        assert parse_review_from_agent("any-host", "any") is None


# ── #248: machine-readable review header ────────────────────────────────────


class TestReviewHeader:
    """Coverage for format_review_header / parse_review_header /
    estimate_review_counts — the helpers that let the coordinator embed
    a verdict + counts in posted review bodies so the TUI / coordinator
    session can surface them without re-ingesting prose."""

    def test_format_header_carries_verdict_only_by_default(self) -> None:
        from coord.review import format_review_header
        out = format_review_header(verdict="approve")
        assert out == "<!-- coord:review verdict=approve -->"

    def test_format_header_emits_all_provided_tokens(self) -> None:
        from coord.review import format_review_header
        out = format_review_header(
            verdict="request-changes",
            reviewer_machine="elitebook",
            assignment_id="144ffa027a31",
            blocking=2,
            nonblocking=5,
            nits=2,
        )
        # Order is stable and counts come before identity fields, matching
        # the example in #248's issue body.
        assert (
            out
            == "<!-- coord:review verdict=request-changes blocking=2 "
            "nonblocking=5 nits=2 reviewer=elitebook "
            "assignment=144ffa027a31 -->"
        )

    def test_parse_header_round_trips(self) -> None:
        from coord.review import format_review_header, parse_review_header
        header = format_review_header(
            verdict="approve",
            reviewer_machine="precision",
            assignment_id="abc123",
            blocking=0,
            nonblocking=3,
            nits=1,
        )
        parsed = parse_review_header(header)
        assert parsed == {
            "verdict": "approve",
            "blocking": 0,
            "nonblocking": 3,
            "nits": 1,
            "reviewer": "precision",
            "assignment": "abc123",
        }

    def test_parse_header_from_full_body(self) -> None:
        """The parser must find the header even when it's followed by
        a full prose body — that's the normal case after the coordinator
        prepends it to findings.body."""
        from coord.review import parse_review_header
        body = (
            "<!-- coord:review verdict=approve blocking=0 reviewer=dellserver -->\n"
            "\n"
            "## Review Complete — ✅ Approved\n"
            "\n"
            "Looks good — all tests pass and the diff stays in scope.\n"
        )
        parsed = parse_review_header(body)
        assert parsed is not None
        assert parsed["verdict"] == "approve"
        assert parsed["blocking"] == 0
        assert parsed["reviewer"] == "dellserver"

    def test_parse_returns_none_when_header_missing(self) -> None:
        from coord.review import parse_review_header
        assert parse_review_header("## Review\n\nLooks fine.") is None

    def test_parse_returns_none_when_verdict_missing(self) -> None:
        """A coord:review HTML comment without a verdict is invalid —
        the parser refuses to return a partial result."""
        from coord.review import parse_review_header
        assert parse_review_header("<!-- coord:review reviewer=x -->") is None

    def test_parse_ignores_unknown_tokens(self) -> None:
        from coord.review import parse_review_header
        parsed = parse_review_header(
            "<!-- coord:review verdict=approve future-token=hello extra=42 -->"
        )
        assert parsed is not None
        assert parsed["verdict"] == "approve"
        # Unknown tokens land as strings; never raise.
        assert parsed["future-token"] == "hello"
        assert parsed["extra"] == "42"

    def test_estimate_counts_picks_up_section_bullets(self) -> None:
        from coord.review import estimate_review_counts
        body = (
            "## Required changes\n"
            "- HUMAN_REQUIRED never persists (coord/cli.py:2616-2663)\n"
            "- retry cap not enforced (coord/conflict_fix.py:161-167)\n"
            "\n"
            "## Non-blocking concerns\n"
            "- Consider extracting the helper into a shared module\n"
            "* And another point that's not blocking\n"
            "\n"
            "## Polish / nits\n"
            "- Trailing whitespace at coord/agent.py:42\n"
        )
        b, nb, nits = estimate_review_counts(body)
        assert (b, nb, nits) == (2, 2, 1)

    def test_estimate_counts_returns_none_when_no_recognised_sections(
        self,
    ) -> None:
        """When the prose doesn't use the conventional headings, the
        heuristic refuses to guess — better an absent count than a
        misleading one."""
        from coord.review import estimate_review_counts
        body = "Looks fine to me — approving.\n"
        assert estimate_review_counts(body) == (None, None, None)

    def test_estimate_counts_empty_section_records_zero(self) -> None:
        """Reaching a recognised heading sets the bucket to 0 even when
        no bullets follow — distinguishes 'no items found' from 'didn't
        check that section'."""
        from coord.review import estimate_review_counts
        body = (
            "## Blocking\n"
            "\n"
            "(none — diff is clean)\n"
            "\n"
            "## Nits\n"
            "- One trailing space at line 42\n"
        )
        b, nb, nits = estimate_review_counts(body)
        # blocking section was visited (set to 0); no Non-blocking
        # heading appears (stays None); nits has one bullet.
        assert b == 0
        assert nb is None
        assert nits == 1


class TestBlockingFindingsConfirmedAbsent:
    """#1456: the evidence standard for overriding a reviewer's verdict.

    Fail-closed by construction — everything the heuristic cannot read must
    return False, because a False only costs an extra fix round while a wrong
    True advances rejected code toward merge with no human in the loop.
    """

    def test_explicit_empty_blocking_section_is_confirmed(self) -> None:
        from coord.review import blocking_findings_confirmed_absent
        body = (
            "## Blocking findings\n"
            "None.\n"
            "## Nits\n"
            "- Trailing whitespace\n"
        )
        assert blocking_findings_confirmed_absent(body) is True

    def test_truly_empty_blocking_section_is_confirmed(self) -> None:
        from coord.review import blocking_findings_confirmed_absent
        body = "## Blocking findings\n\n## Nits\n- One nit\n"
        assert blocking_findings_confirmed_absent(body) is True

    def test_missing_blocking_section_is_not_confirmed(self) -> None:
        """The #1445 shape: nits parse as 0, blocking is unknown.  Unknown is
        NOT zero — this is the whole of #1456."""
        from coord.review import (
            blocking_findings_confirmed_absent,
            estimate_review_counts,
        )
        body = (
            "Two problems block this: the worktree leaks and the test is not\n"
            "hermetic. Requesting changes.\n"
            "#### Nits\n"
            "Nothing worth calling out.\n"
        )
        assert estimate_review_counts(body) == (None, None, 0)
        assert blocking_findings_confirmed_absent(body) is False

    def test_nonblocking_only_body_is_not_confirmed(self) -> None:
        """The pre-#1456 #476 fixture shape (#532 incident): a non-blocking
        section and no blocking heading.  Still not positive evidence."""
        from coord.review import blocking_findings_confirmed_absent
        body = "## Minor observations (not blocking)\n- nit one\n- nit two\n"
        assert blocking_findings_confirmed_absent(body) is False

    def test_bulleted_blocking_finding_is_not_confirmed(self) -> None:
        from coord.review import blocking_findings_confirmed_absent
        body = "## Blocking findings\n- Silent failure swallows the error\n"
        assert blocking_findings_confirmed_absent(body) is False

    def test_prose_blocking_finding_is_not_confirmed(self) -> None:
        """A finding written as a paragraph under a blocking heading counts as
        unreadable, not empty — the bullet counter alone would say 0."""
        from coord.review import blocking_findings_confirmed_absent
        body = (
            "## Blocking findings\n"
            "The worktree created on the early-exit path is never removed, so "
            "every failed dispatch leaks a directory until the disk fills.\n"
        )
        assert blocking_findings_confirmed_absent(body) is False

    def test_empty_body_is_not_confirmed(self) -> None:
        from coord.review import blocking_findings_confirmed_absent
        assert blocking_findings_confirmed_absent("") is False


class TestExtractBlockingSection:
    """#2466: the #603 per-issue context digest used to carry forward only
    `body[:240]` on a request-changes verdict, which silently dropped every
    blocking finding past the first sentence on any review with more than
    one. `extract_blocking_section` replaces that with a verbatim pull of
    just the "## Blocking findings" section, reusing the same
    `_iter_review_sections` parser `estimate_review_counts` and
    `blocking_findings_confirmed_absent` already rely on."""

    def test_extracts_full_multi_paragraph_section_past_240_chars(self) -> None:
        from coord.review import extract_blocking_section
        first = "A" * 200 + " first finding, paragraph one."
        second = "B" * 200 + " second finding, a completely different bug."
        body = (
            "## Blocking findings\n\n"
            f"- {first}\n\n"
            f"- {second}\n\n"
            "## Non-blocking concerns\n\n"
            "- some polish note that should NOT be carried forward\n\n"
            "## Nits\n\n"
            "- trailing whitespace\n"
        )
        out = extract_blocking_section(body)
        assert first in out
        assert second in out
        assert len(out) > 240, "regression guard: must not be truncated to the old 240-char cap"
        assert "should NOT be carried forward" not in out
        assert "trailing whitespace" not in out

    def test_returns_empty_when_no_blocking_heading(self) -> None:
        from coord.review import extract_blocking_section
        assert extract_blocking_section("Looks fine to me — approving.\n") == ""

    def test_returns_empty_when_blocking_section_explicitly_empty(self) -> None:
        from coord.review import extract_blocking_section
        body = "## Blocking findings\nNone.\n## Nits\n- Trailing whitespace\n"
        assert extract_blocking_section(body) == ""

    def test_preserves_bulleted_findings_verbatim(self) -> None:
        from coord.review import extract_blocking_section
        body = (
            "## Blocking findings\n"
            "- `coord/cli.py:2616` — HUMAN_REQUIRED never persists\n"
            "- retry cap not enforced (`coord/conflict_fix.py:161-167`)\n"
        )
        out = extract_blocking_section(body)
        assert "HUMAN_REQUIRED never persists" in out
        assert "retry cap not enforced" in out


# ── Flood guard: dispatch_pending_reviews (incident 2026-06-08) ──────────────


def _pending_work(n: int) -> list[Assignment]:
    """n completed work rows, all eligible for review (review_state=None)."""
    return [
        Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=i + 1,
            issue_title=f"work {i + 1}",
            assignment_id=f"w{i + 1}",
            status="done",
            branch=f"issue-{i + 1}-x",
            type="work",
            review_state=None,
            dispatched_at=0.0,
            finished_at=1.0,
        )
        for i in range(n)
    ]


def _flood_config(**review_kw) -> Config:
    # Pin a review-first gate order so the Test-before-Review gate is OFF for
    # these flood-guard tests — they exercise the cap / surge / #459 dedupe,
    # orthogonal to the test gate (which has its own test below, exercised via
    # the explicit test_gate_active=True parameter).
    return Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(**review_kw),
        pipeline=PipelineConfig(default_gates=["review", "test", "merge"]),
    )


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Replace the real (network) dispatch_review with a recording stub.

    Returns the list of assignment_ids that got a review dispatched.
    """
    calls: list[str] = []

    def _fake(completed, board, config, *, now=None, **kw):
        calls.append(completed.assignment_id)
        review = Assignment(
            machine_name="server",
            repo_name=completed.repo_name,
            issue_number=completed.issue_number,
            issue_title=f"[review] {completed.issue_title}",
            assignment_id=f"rev-{completed.assignment_id}",
            status="running",
            type="review",
            review_of_assignment_id=completed.assignment_id,
            dispatched_at=0.0,
        )
        board.active.append(review)
        return review

    monkeypatch.setattr("coord.review.dispatch_review", _fake)
    return calls


def test_flood_guard_dispatches_all_below_cap(fake_dispatch) -> None:
    board = Board(completed=_pending_work(3))
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 3
    assert len(fake_dispatch) == 3
    assert all(c.review_state == "dispatched" for c in board.completed)


def test_approved_review_does_not_redispatch_across_two_reconcile_ticks(
    fake_dispatch, tmp_path,
) -> None:
    """#1565 black-box acceptance test: dispatch a review, approve it through
    the real auto-loop verdict-processing path, then run the
    ``dispatch_pending_reviews`` "reconcile tick" twice more. Exactly zero
    additional review assignments must be created — the #1565 incident was
    an approved review getting re-dispatched (4 times, $5.36) because
    ``review_state`` regressed back to ``'pending'`` after the approval."""
    from coord.auto_loop import process_review_completion
    from coord.config import PipelineConfig as _PipelineConfig

    work = _pending_work(1)[0]
    board = Board(completed=[work])
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    cfg = replace(
        cfg,
        pipeline=_PipelineConfig(
            default_gates=["review", "test", "merge"], auto_loop=True,
        ),
    )

    # Tick 1: work is eligible, review gets dispatched (the fake stub moves
    # it onto board.active).
    first = dispatch_pending_reviews(board, cfg)
    assert len(first) == 1
    assert len(fake_dispatch) == 1
    assert work.review_state == "dispatched"

    review = board.active[0]
    assert review.type == "review" and review.review_of_assignment_id == work.assignment_id

    # The review "finishes" with an approve verdict — route it through the
    # real process_review_completion (same call reconcile()/notify() make).
    review.status = "done"
    log_file = tmp_path / "review.log"
    log_file.write_text(
        "REVIEW_VERDICT: approve\nREVIEW_BODY:\nLGTM.\nEND_REVIEW\n"
    )
    actions = process_review_completion(
        review, board, cfg, log_path=str(log_file),
    )
    assert actions[0].kind == "approved"
    assert work.review_state == "done"
    assert work.review_verdict == "approve"

    # Ticks 2 and 3: the "reconcile tick" (dispatch_pending_reviews) must
    # find nothing new to dispatch — the approved work row is no longer
    # eligible, and even if review_state had regressed to 'pending' the
    # #1565 dispatch-side guard would catch the recorded terminal verdict.
    second = dispatch_pending_reviews(board, cfg)
    third = dispatch_pending_reviews(board, cfg)
    assert second == []
    assert third == []
    assert len(fake_dispatch) == 1, (
        "dispatch_pending_reviews re-dispatched a review for already-"
        "approved work across reconcile ticks (#1565)"
    )


def test_redispatch_guard_self_heals_a_regressed_pending_review_state(
    fake_dispatch,
) -> None:
    """#1565 direct guard test: even if review_state regresses to 'pending'
    out from under a work row that already has a terminal verdict recorded
    on a completed review assignment, dispatch_pending_reviews must refuse
    to re-dispatch and must self-heal review_state back to 'done'."""
    work = _pending_work(1)[0]
    review = Assignment(
        machine_name="server",
        repo_name=work.repo_name,
        issue_number=work.issue_number,
        issue_title="[review] work",
        assignment_id="rev-w1",
        status="done",
        type="review",
        review_of_assignment_id=work.assignment_id,
        review_verdict="approve",
        dispatched_at=0.0,
        finished_at=1.0,
    )
    # Simulate the clobber shape directly: the work row's review_state is
    # back at 'pending' despite the review above already carrying a
    # terminal verdict.
    work.review_state = "pending"
    board = Board(completed=[work, review])
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)

    out = dispatch_pending_reviews(board, cfg)

    assert out == []
    assert fake_dispatch == []
    assert work.review_state == "done"
    assert work.review_verdict == "approve"


def test_dispatch_pending_reviews_includes_mock_author(fake_dispatch) -> None:
    """#930 fix: the bulk/auto dispatch path (`coord notify`, `reconcile()`)
    must pick up a completed `type="mock-author"` (Gate A) row the same as
    ordinary work — previously the ``eligible`` filter hard-required
    ``type == "work"`` so a Gate A branch could never get an automatic
    review."""
    mock_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=930,
        issue_title="Gate A mock",
        assignment_id="ma-2",
        status="done",
        branch="ms-5-gate-a",
        type="mock-author",
        review_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[mock_author])
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ma-2"]
    assert mock_author.review_state == "dispatched"


def test_dispatch_pending_reviews_includes_test_author(fake_dispatch) -> None:
    """#1141 fix: the bulk/auto dispatch path (`coord notify`, `reconcile()`)
    must pick up a completed `type="test-author"` (#931, per-issue JIT
    acceptance-slice authoring) row the same as ordinary work — previously
    the ``eligible`` filter didn't include ``test-author`` so a JIT slice
    could never get an automatic review, the silent stall confirmed live on
    PR #1139 (epic #1117/ms-37 retrofit)."""
    test_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1117,
        issue_title="ms-37 acceptance slice",
        assignment_id="ta-2",
        status="done",
        branch="ms-37-test-author",
        type="test-author",
        review_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[test_author])
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ta-2"]
    assert test_author.review_state == "dispatched"


def test_dispatch_pending_reviews_skips_interactive_work(fake_dispatch) -> None:
    """#555: an *interactive* (provider_name='claude-pty') work completion must
    NOT get a headless auto-review — its review is human-attended. An
    otherwise-identical non-interactive row still dispatches one."""
    interactive = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=541,
        issue_title="interactive work",
        assignment_id="w-interactive",
        status="done",
        branch="issue-541-x",
        type="work",
        review_state=None,
        provider_name="claude-pty",
        dispatched_at=0.0,
        finished_at=1.0,
    )
    headless = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=542,
        issue_title="headless work",
        assignment_id="w-headless",
        status="done",
        branch="issue-542-x",
        type="work",
        review_state=None,
        provider_name=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[interactive, headless])
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["w-headless"]  # only the non-interactive row
    assert interactive.review_state is None  # never eligible → untouched
    assert headless.review_state == "dispatched"


def test_dispatch_pending_reviews_enforces_max_review_iterations(fake_dispatch) -> None:
    """#1612 step 2: dispatch_pending_reviews must enforce
    ``max_review_iterations`` itself, not only rely on
    ``run_for_fix_transition`` having checked it upstream — defense in depth
    for the fix-loop cap now that a held fix row is routed through here (the
    #1612 test-gate deferral hands review_state back to "pending" for this
    function to pick up). A row at/over the cap must not get a review even
    though it is otherwise perfectly eligible; a row still under the cap
    dispatches normally."""
    capped = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1612,
        issue_title="[fix-2] capped fix round",
        assignment_id="fix-capped",
        status="done",
        branch="issue-1612-fix",
        type="work",
        review_state=None,
        review_iteration=2,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    under_cap = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1613,
        issue_title="[fix-1] under-cap fix round",
        assignment_id="fix-under-cap",
        status="done",
        branch="issue-1613-fix",
        type="work",
        review_state=None,
        review_iteration=1,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[capped, under_cap])
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    cfg.pipeline.max_review_iterations = 2

    out = dispatch_pending_reviews(board, cfg)

    assert fake_dispatch == ["fix-under-cap"]  # capped row never reaches dispatch_review
    assert len(out) == 1
    assert under_cap.review_state == "dispatched"
    assert capped.review_state == "cap_hit"  # not left "pending" forever


def test_flood_guard_caps_per_pass(fake_dispatch) -> None:
    board = Board(completed=_pending_work(10))
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 5  # capped this pass
    pending = [c for c in board.completed if c.review_state in (None, "pending")]
    assert len(pending) == 5  # remainder held for the next pass
    # A second pass drains the rest (still under threshold).
    out2 = dispatch_pending_reviews(board, cfg)
    assert len(out2) == 5
    assert all(c.review_state == "dispatched" for c in board.completed)


def test_flood_guard_surge_gate_refuses_all(fake_dispatch) -> None:
    board = Board(completed=_pending_work(20))  # > flood_threshold
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert out == []
    assert fake_dispatch == []  # nothing dispatched
    assert all(c.review_state is None for c in board.completed)  # board untouched


def test_flood_guard_surge_gate_config_override(fake_dispatch) -> None:
    board = Board(completed=_pending_work(20))
    cfg = _flood_config(
        max_auto_dispatch_per_pass=5, flood_threshold=12, allow_review_flood=True
    )
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 5  # surge gate overridden, per-pass cap still applies


def test_flood_guard_surge_gate_env_override(fake_dispatch, monkeypatch) -> None:
    monkeypatch.setenv("COORD_ALLOW_REVIEW_FLOOD", "1")
    board = Board(completed=_pending_work(20))
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 5


def test_flood_guard_threshold_zero_disables_surge_gate(fake_dispatch) -> None:
    board = Board(completed=_pending_work(50))
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=0)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 5  # no surge gate, but cap still bounds the pass


def test_flood_guard_skips_active_fix_followup(fake_dispatch) -> None:
    # #459: a row whose issue has a live work/conflict-fix is not eligible.
    rows = _pending_work(2)
    board = Board(
        completed=rows,
        active=[
            Assignment(
                machine_name="laptop",
                repo_name="api",
                issue_number=1,  # matches rows[0]
                issue_title="[fix-1] work 1",
                assignment_id="fix1",
                status="running",
                type="work",
            )
        ],
    )
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 1  # only issue #2 (issue #1 has an active fix)
    assert fake_dispatch == ["w2"]


def test_flood_guard_skips_active_fix_followup_keyed_on_the_child(
    fake_dispatch,
) -> None:
    """#1553 regression: the #459 guard must match a slice's retry.

    Both completed rows share the oracle-loop tracking issue's number
    (``issue_number=1120``) — that's how every JIT acceptance slice under one
    milestone is dispatched — but are attributed to different children via
    ``for_issue_number``. A generic ``coord retry`` of a *different* round for
    child #1124 lands on the board as ``type="work"`` carrying
    ``for_issue_number=1124`` (``coord.reconcile._reassign``), while
    ``issue_number`` stays the tracking issue. Before #1553's call-site fix,
    ``has_active_work_followup`` was invoked with the raw (tracking) issue
    number here, so it matched nothing (the active row's raw ``issue_number``
    also reads 1120, but effective-vs-raw comparisons no longer align) and
    silently let the review through against code that was actively being
    rewritten. Only #1124's round must be deferred; the sibling round for
    #1125, which has no active work, must still be dispatched.
    """
    rows = [
        Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1120,  # shared tracking issue
            issue_title="[test-author] ms-38 slice #1124",
            assignment_id="w1124",
            status="done",
            branch="ms-38-acceptance",
            type="test-author",
            for_issue_number=1124,
            review_state=None,
            dispatched_at=0.0,
            finished_at=1.0,
        ),
        Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1120,  # shared tracking issue
            issue_title="[test-author] ms-38 slice #1125",
            assignment_id="w1125",
            status="done",
            branch="ms-38-acceptance",
            type="test-author",
            for_issue_number=1125,
            review_state=None,
            dispatched_at=0.0,
            finished_at=1.0,
        ),
    ]
    board = Board(
        completed=rows,
        active=[
            Assignment(
                machine_name="laptop",
                repo_name="api",
                issue_number=1120,  # tracking issue, unchanged by the retry
                issue_title="[fix] retry of slice #1124",
                assignment_id="retry1124",
                status="running",
                type="work",
                for_issue_number=1124,  # attribution the retry inherits
            )
        ],
    )
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 1  # only #1125's round; #1124's is mid-rewrite
    assert fake_dispatch == ["w1125"]


def test_flood_guard_respects_test_gate(fake_dispatch) -> None:
    rows = _pending_work(4)
    rows[0].test_state = "passed"
    rows[1].test_state = "skipped"
    # rows[2], rows[3] have test_state=None → not eligible under an active gate
    board = Board(completed=rows)
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg, test_gate_active=True)
    assert len(out) == 2
    assert sorted(fake_dispatch) == ["w1", "w2"]


def test_bulk_review_gate_activates_from_test_first_default(fake_dispatch) -> None:
    """Test-before-Review reorder: when default_gates orders Test before Review,
    the bulk path holds review until the work has a passed/skipped test verdict
    — no explicit test_gate_active flag needed."""
    rows = _pending_work(3)
    rows[0].test_state = "passed"
    # rows[1], rows[2] untested → held by the config-driven gate.
    board = Board(completed=rows)
    cfg = Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(max_auto_dispatch_per_pass=5, flood_threshold=12),
        pipeline=PipelineConfig(default_gates=["test", "review", "merge"]),
    )
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 1
    assert fake_dispatch == ["w1"]
    # The untested rows stay pending for a later pass (after they're tested).
    assert rows[1].review_state in (None, "pending")
    assert rows[2].review_state in (None, "pending")


def test_mock_author_auto_skips_test_gate(fake_dispatch) -> None:
    """#1076: a completed `type="mock-author"` (Gate A) row never gets a real
    Test-gate verdict — a contract/fixture-only diff matches no smoke
    capability rule by construction — so under an active test-precedes-review
    gate it must be auto-backfilled to test_state="skipped" and dispatched,
    not silently excluded from `eligible` forever."""
    mock_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1076,
        issue_title="Gate A mock",
        assignment_id="ma-gate-a",
        status="done",
        branch="ms-9-gate-a",
        type="mock-author",
        review_state=None,
        test_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[mock_author])
    cfg = Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(max_auto_dispatch_per_pass=5, flood_threshold=12),
        pipeline=PipelineConfig(default_gates=["test", "review", "merge"]),
    )

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ma-gate-a"]
    assert mock_author.test_state == "skipped"
    assert mock_author.review_state == "dispatched"


def test_mock_author_auto_skip_does_not_weaken_work_gate(fake_dispatch) -> None:
    """#1076: the mock-author auto-skip must not leak onto `type="work"` rows
    — the test gate keeps holding untested real-code completions exactly as
    before."""
    mock_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1076,
        issue_title="Gate A mock",
        assignment_id="ma-gate-a",
        status="done",
        branch="ms-9-gate-a",
        type="mock-author",
        review_state=None,
        test_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    work = _pending_work(1)[0]  # type="work", test_state=None
    board = Board(completed=[mock_author, work])
    cfg = Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(max_auto_dispatch_per_pass=5, flood_threshold=12),
        pipeline=PipelineConfig(default_gates=["test", "review", "merge"]),
    )

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ma-gate-a"]
    assert mock_author.test_state == "skipped"
    assert work.test_state is None
    assert work.review_state in (None, "pending")


def test_test_author_auto_skips_test_gate(fake_dispatch) -> None:
    """#1152: a completed `type="test-author"` (per-issue JIT acceptance-slice
    authoring, #931) row is the same shape as a mock-author completion — a
    fixture/test-only diff that matches no smoke capability rule by
    construction — so it must be auto-backfilled to test_state="skipped" and
    dispatched too, the same as #1076 already does for mock-author."""
    test_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1152,
        issue_title="JIT acceptance slice",
        assignment_id="ta-jit",
        status="done",
        branch="issue-1152-jit-slice",
        type="test-author",
        review_state=None,
        test_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[test_author])
    cfg = Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(max_auto_dispatch_per_pass=5, flood_threshold=12),
        pipeline=PipelineConfig(default_gates=["test", "review", "merge"]),
    )

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ta-jit"]
    assert test_author.test_state == "skipped"
    assert test_author.review_state == "dispatched"


def test_test_author_auto_skip_does_not_weaken_work_gate(fake_dispatch) -> None:
    """#1152: the test-author auto-skip must not leak onto `type="work"` rows
    — the test gate keeps holding untested real-code completions exactly as
    before, same invariant #1076 established for mock-author."""
    test_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1152,
        issue_title="JIT acceptance slice",
        assignment_id="ta-jit",
        status="done",
        branch="issue-1152-jit-slice",
        type="test-author",
        review_state=None,
        test_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    work = _pending_work(1)[0]  # type="work", test_state=None
    board = Board(completed=[test_author, work])
    cfg = Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(max_auto_dispatch_per_pass=5, flood_threshold=12),
        pipeline=PipelineConfig(default_gates=["test", "review", "merge"]),
    )

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ta-jit"]
    assert test_author.test_state == "skipped"
    assert work.test_state is None
    assert work.review_state in (None, "pending")


# ── Flood guard: config parsing ──────────────────────────────────────────────


def test_reviews_config_flood_guard_defaults(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.reviews.max_auto_dispatch_per_pass == 5
    assert cfg.reviews.flood_threshold == 12
    assert cfg.reviews.allow_review_flood is False


def test_reviews_config_flood_guard_custom(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n"
        "  max_auto_dispatch_per_pass: 3\n"
        "  flood_threshold: 25\n"
        "  allow_review_flood: true\n"
    )
    cfg = load(p)
    assert cfg.reviews.max_auto_dispatch_per_pass == 3
    assert cfg.reviews.flood_threshold == 25
    assert cfg.reviews.allow_review_flood is True


def test_reviews_config_rejects_negative_flood_threshold(tmp_path: Path) -> None:
    from coord.config import ConfigError

    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  flood_threshold: -1\n"
    )
    with pytest.raises(ConfigError, match="flood_threshold must be a non-negative integer"):
        load(p)


def test_reviews_config_rejects_bool_for_int_field(tmp_path: Path) -> None:
    from coord.config import ConfigError

    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  max_auto_dispatch_per_pass: true\n"
    )
    with pytest.raises(ConfigError, match="max_auto_dispatch_per_pass must be a non-negative integer"):
        load(p)


# ── #1488: review-reaffirm sanity bound config ──────────────────────────────


def test_reviews_config_reaffirm_max_diff_lines_default(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.reviews.reaffirm_max_diff_lines == 300


def test_reviews_config_reaffirm_max_diff_lines_custom(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  reaffirm_max_diff_lines: 50\n"
    )
    cfg = load(p)
    assert cfg.reviews.reaffirm_max_diff_lines == 50


def test_reviews_config_rejects_negative_reaffirm_max_diff_lines(tmp_path: Path) -> None:
    from coord.config import ConfigError

    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  reaffirm_max_diff_lines: -1\n"
    )
    with pytest.raises(
        ConfigError, match="reaffirm_max_diff_lines must be a non-negative integer"
    ):
        load(p)


# ── #586: branch-not-on-remote guard in dispatch_review ─────────────────────


def test_dispatch_review_routes_back_to_worker_when_branch_not_on_remote(
    two_machine_config: Config,
) -> None:
    """When the branch isn't on the remote, review is routed back to the
    original worker machine (which has it locally) rather than dispatching
    to a different machine that would crash in 2 seconds."""
    board = Board()
    completed = _completed_assignment(machine="laptop", branch="issue-1-fix")
    client = _FakeHTTPClient({"id": "review-local-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 10, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        # Simulate branch absent on remote.
        remote_branch_checker=lambda repo, branch: False,
    )

    assert result is not None
    # Must have routed to the worker's own machine, not the other one.
    assert result.machine_name == "laptop"
    # Exactly one HTTP call, to laptop.tail (the worker machine).
    assert len(client.calls) == 1
    url, _ = client.calls[0]
    assert "laptop.tail" in url


def test_dispatch_review_blocks_and_sets_state_when_branch_not_on_remote_and_worker_unavailable(
    two_machine_config: Config,
) -> None:
    """When branch isn't on remote AND the original worker machine is absent
    from config, dispatch_review must return None and set
    review_state='branch_not_on_remote' so coord status surfaces a visible
    error instead of silently failing."""
    # Build a config where only one machine exists (NOT the original worker).
    from dataclasses import replace as dc_replace
    single_machine_cfg = dc_replace(
        two_machine_config,
        machines=[
            Machine(
                name="server", host="server.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/srv/api"},
            ),
        ],
    )
    board = Board()
    # Completed assignment was done on "laptop" which is no longer in config.
    completed = _completed_assignment(machine="laptop", branch="issue-1-fix")

    result = dispatch_review(
        completed, board, single_machine_cfg,
        http_client=_FakeHTTPClient({"id": "should-not-fire"}),
        pr_lookup=lambda repo_github, **kw: {"number": 10, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: False,
    )

    assert result is None
    assert board.active == []
    assert completed.review_state == "branch_not_on_remote"


def test_dispatch_review_passes_through_normally_when_branch_on_remote(
    two_machine_config: Config,
) -> None:
    """When branch IS on remote, the normal cross-machine dispatch path runs."""
    board = Board()
    completed = _completed_assignment(machine="laptop", branch="issue-1-fix")
    client = _FakeHTTPClient({"id": "review-remote-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 10, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        # Branch exists on remote — normal cross-machine routing.
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    assert result.machine_name == "server"  # different from worker (laptop)


# ── #904: fall-through loop + health-check pre-filter ───────────────────────


def test_dispatch_review_includes_machine_advertising_empty_repos_list(
    two_machine_config: Config,
) -> None:
    """#1485: an empty ``/health`` ``repos`` list means "no local
    coordinator.yml" (the expected state for a worker-only machine —
    coordinator.yml lives on dellserver only), NOT "handles nothing". This
    matches the agent's own semantics in ``AgentServer.assign``
    (``if self.repos and ...`` — empty is falsy, meaning unrestricted).

    When ``server`` (the preferred different-machine candidate) advertises an
    empty repo list, ``dispatch_review`` must still select it — not skip it
    and fall through to ``laptop``."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "empty-health-1"})

    def _health(host: str) -> list[str] | None:
        # server correctly has no local coordinator.yml — advertises [].
        if "server" in host:
            return []
        return ["api"]

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 7, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        health_checker=_health,
    )

    assert result is not None
    # server was NOT filtered — empty repos list means "unrestricted".
    assert result.machine_name == "server"
    assert result.assignment_id == "empty-health-1"
    assert len(client.calls) == 1
    url, _ = client.calls[0]
    assert "server.tail" in url


def test_dispatch_review_skips_machine_advertising_other_repo_in_health(
    two_machine_config: Config,
) -> None:
    """Fix #2 (PREVENTATIVE, #904), preserved by #1485: a candidate whose
    /health advertises a *non-empty* repos list that omits the target repo is
    still a genuine config-drift signal and is skipped before any POST
    attempt.

    When ``server`` (the preferred different-machine candidate) advertises
    ``["other-repo"]``, ``dispatch_review`` should skip it and fall through to
    ``laptop`` (the worker's own machine) rather than dispatching a
    guaranteed-400 POST."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "health-filter-1"})

    def _health(host: str) -> list[str] | None:
        # server is genuinely drifted: /health lists a different repo set.
        if "server" in host:
            return ["other-repo"]
        return ["api"]          # laptop advertises "api" correctly

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 7, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        health_checker=_health,
    )

    assert result is not None
    # server was filtered by health check; dispatch fell through to laptop.
    assert result.machine_name == "laptop"
    assert result.assignment_id == "health-filter-1"
    # Only ONE POST — to laptop; server was excluded before any network call.
    assert len(client.calls) == 1
    url, _ = client.calls[0]
    assert "laptop.tail" in url
    assert "server.tail" not in url


def test_dispatch_review_includes_machine_when_health_probe_fails(
    two_machine_config: Config,
) -> None:
    """Fail-open must not regress (#1485): when the health probe itself fails
    (returns ``None``, e.g. network error or timeout), the candidate is still
    included rather than excluded."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "probe-failed-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 7, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        health_checker=lambda host: None,
    )

    assert result is not None
    # server (the preferred candidate) was included despite the failed probe.
    assert result.machine_name == "server"
    assert result.assignment_id == "probe-failed-1"


def test_dispatch_review_falls_through_to_second_candidate_on_400(
    two_machine_config: Config,
) -> None:
    """Fix #1 (PRIMARY, #904): when the first reviewer candidate returns a 400
    'does not handle repo' response, ``dispatch_review`` retries with the next
    candidate instead of silently returning None and leaving review_state as
    'pending'.

    ``http_client=`` is the existing injection seam; the test stubs it so
    ``server.tail`` 400s and ``laptop.tail`` succeeds."""
    board = Board()
    completed = _completed_assignment(machine="laptop")

    client = _FallThroughClient(
        reject_fragment="server.tail",
        success_payload={"id": "fallthrough-review-1"},
    )

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 5, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        # Bypass health pre-filter so only the POST rejection drives fall-through.
        health_checker=lambda host: None,
    )

    assert result is not None
    assert result.machine_name == "laptop"
    assert result.assignment_id == "fallthrough-review-1"
    # Two POST calls — first to server (rejected), then to laptop (accepted).
    assert len(client.calls) == 2
    assert any("server.tail" in url for url in client.calls), (
        "expected a POST to server.tail (the first, rejected candidate)"
    )
    assert any("laptop.tail" in url for url in client.calls), (
        "expected a POST to laptop.tail (the fall-through candidate)"
    )
    # Review assignment is on the board.
    assert result in board.active
    assert result.review_of_assignment_id == completed.assignment_id


def test_dispatch_review_sets_stall_state_when_all_candidates_rejected(
    two_machine_config: Config,
) -> None:
    """Fix #1 + exhaustion (#904): when ALL reviewer candidates 400, the work
    row's ``review_state`` is set to ``'no_eligible_reviewer'`` (NOT left as
    ``'pending'``), so the pending-review loop stops silently retrying and
    ``coord status`` can surface an actionable error.

    Returns None (same contract as before) but the stall state is now visible."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _AllRejectingClient()

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 3, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        # Bypass health pre-filter so the POST 400 is the signal.
        health_checker=lambda host: None,
    )

    assert result is None
    assert board.active == []
    # Stall state set — NOT left as None/pending.
    assert completed.review_state == "no_eligible_reviewer", (
        f"expected 'no_eligible_reviewer', got {completed.review_state!r}"
    )
    # Both candidates were tried — not just the first.
    assert len(client.calls) == 2, (
        f"expected 2 POST attempts (one per candidate), got {len(client.calls)}"
    )


def test_dispatch_review_leaves_pending_when_all_candidates_5xx(
    two_machine_config: Config,
) -> None:
    """Fix #2 (#904): a 5xx from every candidate is a TRANSIENT failure (agent
    mid-restart, unhandled exception, etc.) — it says nothing about whether
    this agent/repo pairing is valid, unlike a 4xx.  ``dispatch_review`` must
    NOT set ``review_state='no_eligible_reviewer'`` in this case; the row
    should stay eligible (``review_state`` untouched / still pending) so the
    next reconcile/notify pass retries automatically, exactly like the
    existing network-unreachable branch."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _AllServerErrorClient()

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 3, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        health_checker=lambda host: None,
    )

    assert result is None
    assert board.active == []
    # NOT 'no_eligible_reviewer' — a 5xx is transient, not a definitive
    # rejection, so the caller (dispatch_pending_reviews) must retry it.
    assert completed.review_state != "no_eligible_reviewer", (
        f"5xx must not be treated as a definitive rejection, got "
        f"review_state={completed.review_state!r}"
    )
    assert len(client.calls) == 2, (
        f"expected 2 POST attempts (one per candidate), got {len(client.calls)}"
    )
