"""Tests for #241: type="conflict-fix" auto-rebase on merge conflict.

Covers:
- Conflict classification (rebaseable / human / unknown)
- Briefing assembly
- Machine selection (prefer worker's machine, fallback to any idle)
- Dispatcher integration with the board
- Reconcile hook: conflict-fix done → merge entry re-enqueued
- Reconcile hook: conflict-fix failed → merge entry HUMAN_REQUIRED
- #2555: sealed-author (test-author/mock-author) conflict resolution — a
  manifest.yml-only collision auto-heals; a conflict reaching outside
  manifest.yml refuses and escalates to a human.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from coord.config import Config, PipelineConfig, ReviewsConfig
from coord.conflict_fix import (
    CONFLICT_FIX_SYSTEM_PROMPT,
    SEALED_CONFLICT_FIX_TITLE_PREFIX,
    SEALED_MANIFEST_CONFLICT_SYSTEM_PROMPT,
    SEALED_SCOPE_STUCK_MARKER,
    build_conflict_fix_briefing,
    build_sealed_manifest_conflict_briefing,
    dispatch_conflict_fix,
    pick_conflict_fix_machine,
    sealed_conflict_could_touch_manifest,
    sealed_conflict_is_manifest_only,
    sealed_scope_verdict_in_text,
)
from coord.merge_queue import (
    CONFLICT,
    HUMAN_REQUIRED,
    MERGED,
    PENDING,
    QueuedMerge,
    classify_conflict,
    is_rebase_refusal,
)
from coord.models import Assignment, Board, Machine, Repo


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", default_branch="main", test_command="pytest")


@pytest.fixture
def two_machine_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server", host="server.tail",
                repos=["api"], repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=False),
    )


def _entry(
    *, error: str | None = "Merge conflict in foo.py", assignment_type: str = "work",
) -> QueuedMerge:
    return QueuedMerge(
        assignment_id="abc123",
        repo_name="api",
        repo_github="acme/api",
        branch="issue-1-fix",
        target_branch="main",
        issue_number=1,
        issue_title="Fix the thing",
        state=CONFLICT,
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        error=error,
        assignment_type=assignment_type,
    )


# ── Classification ──────────────────────────────────────────────────────────


class TestClassifyConflict:
    @pytest.mark.parametrize("msg", [
        "Merge conflict in src/foo.py",
        "merge conflict",
        "could not be rebased",
        "branch is not up to date with the base branch",
        "non-fast-forward update rejected",
        "PR is behind the base branch",
        # #276: the actual phrasing gh pr merge returns when base has moved.
        "Pull request #273 is not mergeable: the merge commit cannot be cleanly created.",
        "X PR is not mergeable",
        # #1467: GitHub's actual wording for a branch carrying a merge
        # commit — previously unmatched by any signal (only "could not be
        # rebased" existed), so this fell through to "unknown" and #241's
        # conflict-fix worker was never dispatched.
        "GraphQL: This branch can't be rebased (mergePullRequest)",
        "This branch cannot be rebased due to conflicts",
    ])
    def test_rebaseable(self, msg: str) -> None:
        assert classify_conflict(msg) == "rebaseable"

    @pytest.mark.parametrize("msg", [
        "required status check 'ci' has not passed",
        "Required review required",
        "permission denied",
        "Pushes to this protected branch are restricted",
        "branch protection rule blocks force-push",
    ])
    def test_human(self, msg: str) -> None:
        assert classify_conflict(msg) == "human"

    def test_branch_policy_block_is_human_not_rebaseable(self) -> None:
        """#2475: GitHub's wording when a required status check can never
        report (its source CI job was deleted while still required) also
        contains "not mergeable" — a _REBASEABLE_SIGNALS entry — so without
        a specific _HUMAN_SIGNALS match this fell through and was
        misclassified "rebaseable", dispatching a conflict-fix worker that
        could never succeed (#2009's 38-turn thrash: 38 dispatches over ~7
        hours before a human intervened).
        """
        msg = (
            "Pull request #2471 is not mergeable: the base branch policy "
            "prohibits the merge."
        )
        assert classify_conflict(msg) == "human"

    def test_unknown(self) -> None:
        assert classify_conflict("some other error") == "unknown"
        assert classify_conflict("") == "unknown"
        assert classify_conflict(None) == "unknown"


class TestIsRebaseRefusal:
    """#1467: is_rebase_refusal() isolates the "branch can't be rebased"
    wording from the broader "rebaseable" classification — the one failure
    mode where GitHub's own mergeable field lies (reports MERGEABLE even
    though a --rebase merge is refused), so reconcile_conflict_entries needs
    a narrower signal than classify_conflict() to avoid the #1467 park/
    unpark loop."""

    @pytest.mark.parametrize("msg", [
        "GraphQL: This branch can't be rebased (mergePullRequest)",
        "This branch cannot be rebased due to conflicts",
        "THIS BRANCH CAN'T BE REBASED",
    ])
    def test_true_for_rebase_refusal_wording(self, msg: str) -> None:
        assert is_rebase_refusal(msg) is True

    @pytest.mark.parametrize("msg", [
        "Merge conflict in src/foo.py",
        "could not be rebased",
        "Pull request #273 is not mergeable",
        "required status check 'ci' has not passed",
        "some other error",
        "",
        None,
    ])
    def test_false_for_everything_else(self, msg) -> None:
        assert is_rebase_refusal(msg) is False


# ── Briefing ────────────────────────────────────────────────────────────────


class TestBuildBriefing:
    def test_contains_steps(self) -> None:
        briefing = build_conflict_fix_briefing(
            entry=_entry(), repo_path="/work/api", test_command="pytest -x",
        )
        assert "git fetch origin" in briefing
        # #1694: NO `git checkout` step.  The agent dispatches this worker
        # with `target_branch=entry.branch`, so its worktree already comes up
        # on `issue-1-fix`; the old step 3 only ever ran in the shared base
        # checkout (step 1 was `cd <repo_path>`) and left it parked there,
        # which is the state #1693 has to refuse and #1694 has to clear.
        # See tests/test_base_checkout_restore.py for the guard.
        assert "git pull --rebase origin main" in briefing
        assert "git push --force-with-lease origin issue-1-fix" in briefing
        assert "pytest -x" in briefing

    def test_includes_error_context(self) -> None:
        briefing = build_conflict_fix_briefing(
            entry=_entry(error="Merge conflict in api/models.py"),
            repo_path="/work/api",
            test_command=None,
        )
        assert "Merge conflict in api/models.py" in briefing

    def test_warns_against_semantic_conflicts(self) -> None:
        briefing = build_conflict_fix_briefing(
            entry=_entry(), repo_path="/work/api", test_command="pytest",
        )
        assert "semantic" in briefing.lower()
        assert "DO NOT" in briefing or "do not" in briefing.lower()

    def test_no_test_command_falls_back(self) -> None:
        briefing = build_conflict_fix_briefing(
            entry=_entry(), repo_path="/work/api", test_command=None,
        )
        assert "no test command configured" in briefing


# ── #2555: sealed-author (test-author/mock-author) conflict resolution ─────


class TestBuildSealedManifestBriefing:
    def test_authorizes_manifest_yml_only(self) -> None:
        briefing = build_sealed_manifest_conflict_briefing(
            entry=_entry(assignment_type="test-author"),
            repo_path="/work/api",
            test_command="pytest -x",
        )
        assert "manifest.yml" in briefing
        assert "additively" in briefing.lower() or "additive" in briefing.lower()
        assert "git fetch origin" in briefing
        assert "git pull --rebase origin main" in briefing
        assert "git push --force-with-lease origin issue-1-fix" in briefing
        assert "pytest -x" in briefing

    def test_forbids_everything_else_under_sealed_tree(self) -> None:
        briefing = build_sealed_manifest_conflict_briefing(
            entry=_entry(assignment_type="mock-author"),
            repo_path="/work/api",
            test_command="pytest",
        )
        assert "test body" in briefing.lower()
        assert "contract.md" in briefing

    def test_names_the_sealed_scope_marker(self) -> None:
        briefing = build_sealed_manifest_conflict_briefing(
            entry=_entry(assignment_type="test-author"),
            repo_path="/work/api",
            test_command="pytest",
        )
        assert SEALED_SCOPE_STUCK_MARKER in briefing

    def test_no_test_command_falls_back(self) -> None:
        briefing = build_sealed_manifest_conflict_briefing(
            entry=_entry(assignment_type="test-author"),
            repo_path="/work/api",
            test_command=None,
        )
        assert "no test command configured" in briefing


class TestSealedScopeVerdictInText:
    def test_true_when_marker_present(self) -> None:
        assert sealed_scope_verdict_in_text(
            f"STATUS: rebasing\nSTUCK: {SEALED_SCOPE_STUCK_MARKER} "
            "tests/acceptance/ms-4/audit.rs:1-9 — test body conflict"
        ) is True

    def test_false_when_absent(self) -> None:
        assert sealed_scope_verdict_in_text("STATUS: pushed\n") is False
        assert sealed_scope_verdict_in_text(None) is False
        assert sealed_scope_verdict_in_text("") is False

    def test_false_for_ordinary_semantic_marker(self) -> None:
        """A plain `coord:conflict=semantic` verdict must NOT also read as a
        sealed-scope verdict — the two markers are deliberately distinct so
        the (not sealed-aware) semantic-escalation path never fires for a
        sealed-author entry's refusal."""
        assert sealed_scope_verdict_in_text(
            "STUCK: coord:conflict=semantic src/foo.py:1-9 — contradictory"
        ) is False


class TestSealedConflictIsManifestOnly:
    def test_true_for_single_manifest_file(self) -> None:
        assert sealed_conflict_is_manifest_only(
            ["tests/acceptance/ms-4/manifest.yml"]
        ) is True

    def test_true_for_multiple_milestone_manifests(self) -> None:
        assert sealed_conflict_is_manifest_only([
            "tests/acceptance/ms-4/manifest.yml",
            "tests/acceptance/ms-5/manifest.yml",
        ]) is True

    def test_false_when_any_file_is_not_manifest(self) -> None:
        assert sealed_conflict_is_manifest_only([
            "tests/acceptance/ms-4/manifest.yml",
            "tests/acceptance/ms-4/audit_test.rs",
        ]) is False

    def test_false_for_empty_list(self) -> None:
        assert sealed_conflict_is_manifest_only([]) is False

    def test_false_for_non_manifest_filename_ending_similarly(self) -> None:
        assert sealed_conflict_is_manifest_only(
            ["tests/acceptance/ms-4/other_manifest.yml"]
        ) is False

    def test_true_for_a_per_issue_fragment(self) -> None:
        """#2543: a manifest.d/<issue>.yml fragment is also a "manifest"
        file the sealed conflict-fix branch is authorized to touch."""
        assert sealed_conflict_is_manifest_only(
            ["tests/acceptance/ms-4/manifest.d/944.yml"]
        ) is True

    def test_true_for_fragment_alongside_legacy_manifest(self) -> None:
        assert sealed_conflict_is_manifest_only([
            "tests/acceptance/ms-4/manifest.yml",
            "tests/acceptance/ms-4/manifest.d/944.yml",
        ]) is True

    def test_false_for_a_non_manifest_file_inside_manifest_d(self) -> None:
        assert sealed_conflict_is_manifest_only(
            ["tests/acceptance/ms-4/manifest.d/README.md"]
        ) is False


class TestSealedConflictCouldTouchManifest:
    """#2555 review fix: the notify.py gate must key off "does a manifest.yml
    appear anywhere in the branch's whole diff", not "is the whole diff
    nothing but manifest.yml" — the latter rejects the realistic mixed
    shape (manifest.yml + a newly authored spec file) outright."""

    def test_true_for_single_manifest_file(self) -> None:
        assert sealed_conflict_could_touch_manifest(
            ["tests/acceptance/ms-4/manifest.yml"]
        ) is True

    def test_true_when_manifest_accompanied_by_other_sealed_files(self) -> None:
        """The realistic #132/#2555 shape: the branch's own diff also
        contains the spec file it authored alongside its manifest.yml
        edit — this must NOT be rejected as "not manifest-only"."""
        assert sealed_conflict_could_touch_manifest([
            "tests/acceptance/ms-4/manifest.yml",
            "tests/acceptance/ms-4/new_spec.rs",
        ]) is True

    def test_false_when_no_manifest_present(self) -> None:
        assert sealed_conflict_could_touch_manifest(
            ["tests/acceptance/ms-4/audit_test.rs"]
        ) is False

    def test_false_for_empty_list(self) -> None:
        assert sealed_conflict_could_touch_manifest([]) is False

    def test_false_for_non_manifest_filename_ending_similarly(self) -> None:
        assert sealed_conflict_could_touch_manifest(
            ["tests/acceptance/ms-4/other_manifest.yml"]
        ) is False

    def test_true_when_a_per_issue_fragment_is_present(self) -> None:
        """#2543: the common case going forward — a JIT slice's own diff
        plus its manifest.d/<issue>.yml fragment, no shared manifest.yml
        at all."""
        assert sealed_conflict_could_touch_manifest([
            "tests/acceptance/ms-4/manifest.d/944.yml",
            "tests/acceptance/ms-4/new_spec.rs",
        ]) is True


class TestDispatchSealedAuthorBranch:
    def test_test_author_entry_gets_sealed_briefing(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        client = _FakeHTTPClient({"id": "fix-sealed-1"})
        result = dispatch_conflict_fix(
            _entry(assignment_type="test-author"),
            Board(),
            two_machine_config,
            http_client=client,
            prefer_machine="laptop",
        )
        assert result is not None
        assert result.issue_title.startswith(SEALED_CONFLICT_FIX_TITLE_PREFIX)
        _, payload = client.calls[0]
        assert payload["system_prompt"] == SEALED_MANIFEST_CONFLICT_SYSTEM_PROMPT
        assert "manifest.yml" in payload["briefing"]
        assert payload["issue_title"].startswith(SEALED_CONFLICT_FIX_TITLE_PREFIX)

    def test_mock_author_entry_gets_sealed_briefing(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        client = _FakeHTTPClient({"id": "fix-sealed-2"})
        result = dispatch_conflict_fix(
            _entry(assignment_type="mock-author"),
            Board(),
            two_machine_config,
            http_client=client,
            prefer_machine="laptop",
        )
        assert result is not None
        _, payload = client.calls[0]
        assert payload["system_prompt"] == SEALED_MANIFEST_CONFLICT_SYSTEM_PROMPT

    def test_plain_work_entry_keeps_ordinary_briefing(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """A default `assignment_type="work"` entry must be unaffected by
        #2555 — it keeps the ordinary conflict-fix briefing, which is never
        authorized to touch tests/acceptance/**."""
        client = _FakeHTTPClient({"id": "fix-plain-1"})
        result = dispatch_conflict_fix(
            _entry(assignment_type="work"),
            Board(),
            two_machine_config,
            http_client=client,
            prefer_machine="laptop",
        )
        assert result is not None
        assert not result.issue_title.startswith(SEALED_CONFLICT_FIX_TITLE_PREFIX)
        _, payload = client.calls[0]
        assert payload["system_prompt"] == CONFLICT_FIX_SYSTEM_PROMPT

    def test_default_entry_assignment_type_is_work(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """`QueuedMerge.assignment_type` defaults to "work" for entries
        enqueued before #1077 added the field — must not be mistaken for a
        sealed-author entry."""
        client = _FakeHTTPClient({"id": "fix-plain-2"})
        dispatch_conflict_fix(
            _entry(), Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        _, payload = client.calls[0]
        assert payload["system_prompt"] == CONFLICT_FIX_SYSTEM_PROMPT

    def test_deny_commands_unchanged_for_sealed_branch(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """gh and force-push stay denied for the sealed resolver too — the
        narrow authorization is only for editing manifest.yml, not for the
        harness-level command denials every conflict-fix worker gets."""
        client = _FakeHTTPClient({"id": "fix-sealed-3"})
        dispatch_conflict_fix(
            _entry(assignment_type="test-author"), Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        _, payload = client.calls[0]
        deny = payload.get("deny_commands", [])
        assert "Bash(gh *)" in deny
        assert "Bash(git push --force *)" in deny

    def test_audit_row_flags_sealed_author(
        self, two_machine_config: Config, coord_db, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "nonexistent.yml"))
        client = _FakeHTTPClient({"id": "fix-sealed-4"})
        result = dispatch_conflict_fix(
            _entry(assignment_type="mock-author"), Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        assert result is not None
        row = coord_db.execute(
            "SELECT * FROM audit_log WHERE tier='operational'"
        ).fetchone()
        details = json.loads(row["details_json"])
        assert details["sealed_author"] is True


# ── Machine selection ───────────────────────────────────────────────────────


class TestPickMachine:
    def test_prefers_worker_machine_when_idle(self, two_machine_config: Config) -> None:
        board = Board()
        machine = pick_conflict_fix_machine(
            "api", board, two_machine_config, prefer_machine="laptop",
        )
        assert machine is not None
        assert machine.name == "laptop"

    def test_falls_back_to_idle_when_preferred_busy(
        self, two_machine_config: Config,
    ) -> None:
        board = Board()
        board.active.append(Assignment(
            machine_name="laptop", repo_name="api", issue_number=99, issue_title="x",
            status="running",
        ))
        machine = pick_conflict_fix_machine(
            "api", board, two_machine_config, prefer_machine="laptop",
        )
        assert machine is not None
        assert machine.name == "server"

    def test_returns_none_when_no_machine_handles_repo(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="a/b")],
            machines=[Machine(name="m", host="h", repos=["other"])],
        )
        assert pick_conflict_fix_machine("api", Board(), cfg) is None


# ── Dispatch ────────────────────────────────────────────────────────────────


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeHTTPResponse:
        self.calls.append((url, json))
        return _FakeHTTPResponse(self._payload)


class TestDispatch:
    def test_appends_to_board_and_sends_payload(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        client = _FakeHTTPClient({"id": "fix-id-1"})
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config,
            http_client=client, prefer_machine="laptop", now=99.0,
        )
        assert result is not None
        assert result.type == "conflict-fix"
        assert result.machine_name == "laptop"
        assert result.branch == "issue-1-fix"
        assert result.review_of_assignment_id == "abc123"
        assert result.dispatched_at == 99.0
        assert board.active == [result]

        assert len(client.calls) == 1
        url, payload = client.calls[0]
        assert "laptop.tail" in url
        assert payload["type"] == "conflict-fix"
        assert payload["system_prompt"] == CONFLICT_FIX_SYSTEM_PROMPT
        assert payload["review_target"] == "issue-1-fix"
        # #1694: `branch` must carry the repo's real default/merge-target
        # branch (`entry.target_branch`), NOT the work branch — otherwise it
        # is indistinguishable from `target_branch` below and Part A/B's
        # `branch == default_branch` short-circuit silently no-ops.
        assert payload["branch"] == "main"
        assert payload["target_branch"] == "issue-1-fix"
        assert payload["repo_path"] == "/work/api"

    def test_returns_none_if_already_in_flight(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        board.active.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="prev-fix", status="running",
            type="conflict-fix", review_of_assignment_id="abc123",
        ))
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config,
            http_client=_FakeHTTPClient({"id": "would-not-fire"}),
        )
        assert result is None

    def test_returns_none_when_http_fails(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        import httpx

        class _Failing:
            def post(self, url, *, json, timeout):
                raise httpx.ConnectError("offline")

        result = dispatch_conflict_fix(
            _entry(), Board(), two_machine_config, http_client=_Failing(),
        )
        assert result is None

    def test_payload_includes_deny_commands(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """gh and force-push must be in the dispatch payload's deny_commands.

        Regression test for review of #243: CLAUDE.md claims `gh` is denied
        for conflict-fix workers but the payload had no deny_commands key.
        Enforcement was prompt-only; now it's enforced by the agent harness.
        """
        client = _FakeHTTPClient({"id": "fix-id-deny"})
        result = dispatch_conflict_fix(
            _entry(), Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        assert result is not None
        _, payload = client.calls[0]
        deny = payload.get("deny_commands", [])
        assert "Bash(gh *)" in deny
        assert "Bash(git push --force *)" in deny
        assert "Bash(git push -f *)" in deny

    def test_payload_deny_commands_merge_with_repo_config(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """Repo-level worker_permissions.deny entries are preserved alongside
        the conflict-fix-specific deny patterns (no clobbering)."""
        from coord.models import WorkerPermissionsConfig
        cfg = two_machine_config
        cfg.repos[0].worker_permissions = WorkerPermissionsConfig(
            deny=["Bash(rm -rf *)", "Bash(curl *)"],
        )
        client = _FakeHTTPClient({"id": "fix-id-merge"})
        dispatch_conflict_fix(
            _entry(), Board(), cfg, http_client=client, prefer_machine="laptop",
        )
        _, payload = client.calls[0]
        deny = payload["deny_commands"]
        assert "Bash(rm -rf *)" in deny
        assert "Bash(curl *)" in deny
        assert "Bash(gh *)" in deny
        assert "Bash(git push --force *)" in deny
        # Dedup: no repeats even if repo config happened to include one.
        assert len(deny) == len(set(deny))

    def test_payload_pins_target_branch_to_original(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """#277: the dispatch payload must set ``target_branch`` to the
        original branch so the agent checks out that branch instead of
        deriving an orphan slug from ``[conflict-fix] <title>``."""
        client = _FakeHTTPClient({"id": "fix-id-tb"})
        dispatch_conflict_fix(
            _entry(), Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        _, payload = client.calls[0]
        assert payload.get("target_branch") == _entry().branch

    def test_retry_cap_blocks_second_dispatch_when_prior_failed(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """A conflict-fix in ``board.completed`` with status "failed" blocks a
        second dispatch — the retry cap is consumed by a genuine failure.

        (The original test used status="done", which #784 changed: a successful
        rebase should NOT consume the cap so a re-conflict gets another attempt.)
        """
        board = Board()
        board.completed.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="prev-fix", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        ))
        client = _FakeHTTPClient({"id": "would-not-fire"})
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config, http_client=client,
        )
        assert result is None
        assert client.calls == [], "HTTP should not be called when retry cap hit"

    def test_dispatch_writes_operational_audit_row(
        self, two_machine_config: Config, coord_db, monkeypatch, tmp_path,
    ) -> None:
        """#1038: a successful dispatch writes an operational-tier row
        (actor="daemon") alongside the business-tier "dispatched" row
        `record_dispatched_assignment` already writes regardless of caller."""
        monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "nonexistent.yml"))
        board = Board()
        client = _FakeHTTPClient({"id": "fix-id-audit"})
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        assert result is not None

        op_rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE tier='operational'"
        ).fetchall()
        assert len(op_rows) == 1
        assert op_rows[0]["category"] == "merge"
        assert op_rows[0]["event_type"] == "conflict_fix_dispatched"
        assert op_rows[0]["actor"] == "daemon"
        assert op_rows[0]["repo"] == "api"
        assert op_rows[0]["issue"] == 1
        assert op_rows[0]["assignment_id"] == result.assignment_id
        assert op_rows[0]["machine"] == "laptop"

        business_rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE tier='business' AND category='dispatch'"
        ).fetchall()
        assert len(business_rows) == 1
        assert business_rows[0]["actor"] == "coordinator"

    def test_retry_cap_not_consumed_by_successful_fix(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """A conflict-fix that completed successfully (status="done") must NOT
        block a second dispatch.

        #784: a successful rebase can be followed by a new conflict if other PRs
        merged in the meantime; that re-conflict deserves a fresh fix attempt.
        Only genuine failures (failed / advisory) consume the one-per-entry cap.
        """
        board = Board()
        board.completed.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="prev-fix", status="done",
            type="conflict-fix", review_of_assignment_id="abc123",
        ))
        client = _FakeHTTPClient({"id": "fresh-fix"})
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config, http_client=client,
            prefer_machine="laptop",
        )
        assert result is not None, (
            "a successful prior fix must NOT block a new dispatch on re-conflict"
        )
        assert len(client.calls) == 1, "HTTP should have been called"

    def test_retry_cap_blocks_when_done_fix_precedes_identical_error(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """#2475: a `done` conflict-fix that is immediately followed by the
        SAME merge failure text must consume the retry cap — the worker
        found nothing to rebase (no real content conflict), so a second
        dispatch would just repeat the same no-op forever (#2009's 38-turn
        thrash)."""
        board = Board()
        first_client = _FakeHTTPClient({"id": "prev-fix"})
        first = dispatch_conflict_fix(
            _entry(), board, two_machine_config,
            http_client=first_client, prefer_machine="laptop",
        )
        assert first is not None
        # Simulate the worker finishing successfully and the merge queue
        # retrying — same entry, same error (nothing was actually fixed).
        board.active.remove(first)
        first.status = "done"
        board.completed.append(first)

        second_client = _FakeHTTPClient({"id": "would-not-fire"})
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config,
            http_client=second_client, prefer_machine="laptop",
        )
        assert result is None
        assert second_client.calls == [], (
            "HTTP should not be called when the identical failure recurs "
            "after a done conflict-fix"
        )


class TestSemanticEscalationDisabled:
    """#2566: the predicate that lets HUMAN_REQUIRED messaging say *why*
    the tier-2 semantic escalation didn't run, rather than reading as
    though it ran and failed."""

    def test_true_when_config_is_none(self) -> None:
        """No config means no pipeline to check — treat escalation as
        unavailable, matching `_try_semantic_escalation`'s own treatment
        of a missing config/pipeline as "cannot escalate" (#2566 review:
        the two fallbacks must agree)."""
        from coord.conflict_fix import semantic_escalation_disabled
        assert semantic_escalation_disabled(None) is True

    def test_true_on_default_config(self, two_machine_config: Config) -> None:
        """`escalate_semantic_conflicts` defaults False (#1291 ships dark)."""
        from coord.conflict_fix import semantic_escalation_disabled
        assert semantic_escalation_disabled(two_machine_config) is True

    def test_false_when_flag_is_on(self, repo: Repo) -> None:
        from coord.conflict_fix import semantic_escalation_disabled
        cfg = Config(
            repos=[repo],
            machines=[Machine(
                name="laptop", host="laptop.tail",
                repos=["api"], repo_paths={"api": "/work/api"},
            )],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=False),
            pipeline=PipelineConfig(escalate_semantic_conflicts=True),
        )
        assert semantic_escalation_disabled(cfg) is False


class TestHasPriorConflictFix:
    """Cover the retry-cap predicate directly so the cli.py guard is exercised."""

    def test_false_on_empty_board(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        assert has_prior_conflict_fix(Board(), "abc123") is False

    def test_false_when_assignment_id_is_none(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        assert has_prior_conflict_fix(Board(), None) is False

    def test_true_when_active_has_matching_conflict_fix(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.active.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="abc123",
        ))
        assert has_prior_conflict_fix(board, "abc123") is True

    def test_true_when_completed_has_failed_conflict_fix(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="abc123",
            status="failed",
        ))
        assert has_prior_conflict_fix(board, "abc123") is True

    def test_false_when_completed_has_successful_conflict_fix(self) -> None:
        """#784: a done (successful) fix does not block a second attempt."""
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="abc123",
            status="done",
        ))
        assert has_prior_conflict_fix(board, "abc123") is False

    def test_false_when_successful_fix_precedes_a_different_error(self) -> None:
        """#2475: passing `current_error` doesn't change the #784 outcome
        when the new failure is genuinely different — only an IDENTICAL
        recurrence should consume the cap."""
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="abc123",
            status="done",
            briefing="# Conflict fix\nReason: Merge conflict in foo.py\n",
        ))
        assert has_prior_conflict_fix(
            board, "abc123", current_error="Merge conflict in bar.py",
        ) is False

    def test_true_when_successful_fix_precedes_the_identical_error(self) -> None:
        """#2475: a `done` conflict-fix whose worker found nothing to
        rebase (because the real blocker was never a content conflict —
        e.g. a permanent branch-policy block, #2009) is followed by the
        merge queue retrying into the SAME failure text. That is not a
        fresh conflict (#784's carve-out), so it now consumes the retry cap
        just like a genuine failure would — otherwise the queue loops
        forever dispatching conflict-fix workers that can never help.
        """
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="abc123",
            status="done",
            briefing=(
                "# Conflict fix: acme/api branch `issue-1-fix`\n\n"
                "The merge of `issue-1-fix` → `main` failed.\n"
                "Reason: Pull request #2471 is not mergeable: the base "
                "branch policy prohibits the merge.\n"
            ),
        ))
        assert has_prior_conflict_fix(
            board,
            "abc123",
            current_error=(
                "Pull request #2471 is not mergeable: the base branch "
                "policy prohibits the merge."
            ),
        ) is True

    def test_ignores_non_conflict_fix_types(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="review", review_of_assignment_id="abc123",
        ))
        assert has_prior_conflict_fix(board, "abc123") is False

    def test_ignores_other_merge_entries(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="other-entry",
        ))
        assert has_prior_conflict_fix(board, "abc123") is False


# ── Reconcile hook ──────────────────────────────────────────────────────────


class TestReconcileHook:
    """Cover the conflict-fix completion path in `coord.reconcile`."""

    def _populate_queue(self, error: str = "Merge conflict") -> None:
        from coord import merge_queue as mq
        mq.save_queue([_entry(error=error)])

    def test_success_resets_entry_to_pending(self, coord_db) -> None:
        from coord import merge_queue as mq
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue()
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="done",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        _on_conflict_fix_done(fix, succeeded=True)

        entry = mq.load_queue()[0]
        assert entry.state == PENDING
        assert entry.error is None

    def test_failure_marks_human_required(self, coord_db) -> None:
        from coord import merge_queue as mq
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue(error="Merge conflict")
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        _on_conflict_fix_done(fix, succeeded=False)

        entry = mq.load_queue()[0]
        assert entry.state == HUMAN_REQUIRED
        assert "Manual rebase required" in (entry.error or "")

    def test_usage_limit_kill_gets_accurate_message(self, coord_db) -> None:
        """#1461 review finding 2: a conflict-fix worker killed by the
        account's usage limit did not fail to resolve anything — the parked
        HUMAN_REQUIRED entry must say "wait for the reset", not send the
        operator chasing a "manual rebase required" defect that isn't
        there."""
        from coord import merge_queue as mq
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue(error="Merge conflict")
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        _on_conflict_fix_done(
            fix, succeeded=False,
            agent_entry={
                "usage_limit_reason": "usage limit — resets 8:30pm (America/Chicago)",
            },
        )

        entry = mq.load_queue()[0]
        assert entry.state == HUMAN_REQUIRED
        assert "Manual rebase required" not in (entry.error or "")
        assert "usage limit — resets 8:30pm (America/Chicago)" in (entry.error or "")
        assert "wait for the reset" in (entry.error or "").lower()

    def test_noop_when_no_parent(self, coord_db) -> None:
        from coord.reconcile import _on_conflict_fix_done

        # Should not raise even if review_of_assignment_id is missing.
        _on_conflict_fix_done(
            Assignment(
                machine_name="m", repo_name="api", issue_number=1, issue_title="x",
                type="conflict-fix",
            ),
            succeeded=True,
        )

    def test_failure_posts_issue_comment(self, coord_db) -> None:
        """The coordinator posts a HUMAN_REQUIRED comment on failure.

        Replaces the worker's previous "post a comment on the issue"
        instruction (which contradicted the "don't use gh" rule). The
        comment is best-effort: the test asserts the post is attempted
        with the right repo/issue and a non-empty body.
        """
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue(error="Merge conflict in foo.py")
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        with patch("coord.github_ops.post_issue_comment") as post:
            _on_conflict_fix_done(fix, succeeded=False)
        post.assert_called_once()
        repo_arg, issue_arg, body_arg = post.call_args[0]
        assert repo_arg == "acme/api"
        assert issue_arg == 1
        assert "HUMAN_REQUIRED" in body_arg
        assert "fix-id" in body_arg
        assert "laptop" in body_arg

    def test_failure_swallows_github_post_errors(self, coord_db) -> None:
        """A failing gh post must not raise out of the reconcile hook."""
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue(error="Merge conflict")
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        with patch(
            "coord.github_ops.post_issue_comment",
            side_effect=RuntimeError("gh unauthenticated"),
        ):
            # Must not raise — comment posting is best-effort.
            _on_conflict_fix_done(fix, succeeded=False)

    def test_success_does_not_post_comment(self, coord_db) -> None:
        """Only failure triggers the HUMAN_REQUIRED comment."""
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue()
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="done",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        with patch("coord.github_ops.post_issue_comment") as post:
            _on_conflict_fix_done(fix, succeeded=True)
        post.assert_not_called()

    def test_clean_exit_with_semantic_marker_is_not_treated_as_success(
        self, two_machine_config: Config, coord_db, tmp_path,
    ) -> None:
        """#2565: a `claude -p` conflict-fix worker ends its turn (exit 0,
        agent status "done") the same way whether it fixed the conflict or
        gave up — it has no way to set its own exit code. A worker that
        diagnosed the conflict as SEMANTIC and gave up with the
        `coord:conflict=semantic` STUCK marker must not be treated as a
        success just because the caller passed `succeeded=True`: the entry
        must NOT be reset to PENDING (which would silently re-attempt the
        identical, already-diagnosed conflict) — it must land exactly where
        a `succeeded=False` semantic give-up already lands.
        """
        from coord import merge_queue as mq
        from coord.conflict_fix import SEMANTIC_STUCK_MARKER
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue()
        log = tmp_path / "worker.log"
        log.write_text(
            "STATUS: rebase started\n"
            f"STUCK: {SEMANTIC_STUCK_MARKER} src/foo.py:1-9 — both sides "
            "rewrote parse_args() differently\n"
        )
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="done",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        # escalate_semantic_conflicts defaults off on two_machine_config, so
        # this lands on the same HUMAN_REQUIRED outcome a `succeeded=False`
        # semantic give-up would — never PENDING.
        _on_conflict_fix_done(
            fix, succeeded=True,
            agent_entry={"log_path": str(log)},
            board=Board(), config=two_machine_config,
        )

        entry = mq.load_queue()[0]
        assert entry.state != PENDING
        assert entry.state == HUMAN_REQUIRED
        assert "Manual rebase required" in (entry.error or "")

    def test_semantic_giveup_with_escalation_disabled_says_so(
        self, two_machine_config: Config, coord_db, tmp_path,
    ) -> None:
        """#2566: `pipeline.escalate_semantic_conflicts` ships dark (off by
        default) — #1291's tier-2 escalation is fully built but never
        fires on a stock config. When a conflict-fix worker gives up on a
        SEMANTIC conflict and there's nowhere to escalate to *because the
        flag is off*, both the parked entry's error and the GitHub comment
        must say so explicitly — not read as though escalation ran and
        failed, which is indistinguishable from every other conflict-fix
        give-up and sends operators re-deriving this exact gap.
        """
        from coord import merge_queue as mq
        from coord.conflict_fix import SEMANTIC_STUCK_MARKER
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue()
        log = tmp_path / "worker.log"
        log.write_text(
            f"STUCK: {SEMANTIC_STUCK_MARKER} src/foo.py:1-9 — both sides "
            "rewrote parse_args() differently\n"
        )
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        with patch("coord.github_ops.post_issue_comment") as post:
            _on_conflict_fix_done(
                fix, succeeded=False,
                agent_entry={"log_path": str(log)},
                board=Board(), config=two_machine_config,
            )

        entry = mq.load_queue()[0]
        assert entry.state == HUMAN_REQUIRED
        assert "pipeline.escalate_semantic_conflicts is disabled" in (
            entry.error or ""
        )
        post.assert_called_once()
        body = post.call_args[0][2]
        assert "escalate_semantic_conflicts" in body
        assert "No tier-2 attempt was made" in body

    def test_clean_exit_without_marker_still_resets_to_pending(
        self, two_machine_config: Config, coord_db, tmp_path,
    ) -> None:
        """The common case — a real fix, no marker in the log — is
        unaffected by #2565's check: it still resets the entry to
        PENDING."""
        from coord import merge_queue as mq
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue()
        log = tmp_path / "worker.log"
        log.write_text("STATUS: rebase started\nSTATUS: pushed\n")
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="done",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        _on_conflict_fix_done(
            fix, succeeded=True,
            agent_entry={"log_path": str(log)},
            board=Board(), config=two_machine_config,
        )

        entry = mq.load_queue()[0]
        assert entry.state == PENDING
        assert entry.error is None

    def test_notify_path_resets_entry_to_pending(self, coord_db) -> None:
        """coord notify must re-enqueue the parent merge entry when a
        conflict-fix worker completes — the bug that caused the Merge box
        to stay grey forever with no Go button (#291-area regression).

        post_transition is the notify code path; it must call
        on_conflict_fix_done so the queue state flips conflict → pending
        without needing a manual coord resume.
        """
        from coord import merge_queue as mq
        from coord.notify import post_transition, Transition, EVENT_COMPLETION

        self._populate_queue()

        transition = Transition(
            assignment_id="fix-id",
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            event=EVENT_COMPLETION,
            exit_code=0,
        )
        record = {
            "repo_github": "acme/api",
            "type": "conflict-fix",
            "review_of_assignment_id": "abc123",
        }
        entry = {
            "started_at": 1000.0,
            "finished_at": 1060.0,
            "branch": "issue-1-fix",
            "log_path": None,
        }
        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
        ):
            post_transition(transition, record, entry)

        queue = mq.load_queue()
        assert len(queue) == 1
        assert queue[0].state == PENDING, (
            "conflict-fix completion via notify should reset queue entry to PENDING"
        )
        assert queue[0].error is None

    def test_notify_path_semantic_marker_does_not_reset_to_pending(
        self, coord_db, tmp_path,
    ) -> None:
        """#2565: `coord notify` is the routinely-scheduled path (unlike
        the full `reconcile()`, which only `coord resume` calls), so its
        `post_transition` conflict-fix branch is where the bug actually
        bit: it called `on_conflict_fix_done(succeeded=True)` unconditionally
        on every clean exit, without ever reading the worker's own
        `coord:conflict=semantic` STUCK marker. Assert the marker now
        overrides the reported success here too.
        """
        from coord import merge_queue as mq
        from coord.conflict_fix import SEMANTIC_STUCK_MARKER
        from coord.notify import post_transition, Transition, EVENT_COMPLETION

        self._populate_queue()

        log = tmp_path / "worker.log"
        log.write_text(
            "STATUS: rebase started\n"
            f"STUCK: {SEMANTIC_STUCK_MARKER} src/foo.py:1-9 — both sides "
            "rewrote parse_args() differently\n"
        )

        transition = Transition(
            assignment_id="fix-id",
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            event=EVENT_COMPLETION,
            exit_code=0,
        )
        record = {
            "repo_github": "acme/api",
            "type": "conflict-fix",
            "review_of_assignment_id": "abc123",
        }
        entry = {
            "started_at": 1000.0,
            "finished_at": 1060.0,
            "branch": "issue-1-fix",
            "log_path": str(log),
        }
        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.github_ops.post_issue_comment"),
        ):
            post_transition(transition, record, entry)

        queue = mq.load_queue()
        assert len(queue) == 1
        assert queue[0].state == HUMAN_REQUIRED, (
            "a semantic give-up must never be reset to PENDING just because "
            "the worker exited cleanly"
        )
        assert "Manual rebase required" in (queue[0].error or "")


# ── #2555 end-to-end: additive manifest.yml collision merges with no human ─


class TestSealedConflictEndToEnd:
    """Drives the #2555 acceptance scenario: two acceptance-oracle slices
    (test-author/mock-author branches) collide on the SAME milestone
    `manifest.yml` — an additive, mechanical conflict per the file's own
    "one block per issue" rule. Covers both outcomes:

    - the sealed resolver lands the collision and the merge entry is ready
      to merge again with no operator involvement;
    - a conflict that reaches outside manifest.yml (a test body) is refused
      and the entry escalates to a human, exactly like any other
      conflict-fix failure.
    """

    def _populate_queue(self, entry: QueuedMerge) -> None:
        from coord import merge_queue as mq
        mq.save_queue([entry])

    def test_additive_manifest_collision_merges_without_a_human(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        # Slice B (this branch) collided with slice A's already-merged block
        # in the SAME milestone's manifest.yml — the exact coord-portal#132/
        # #129 shape described in #2555.
        entry = _entry(
            assignment_type="test-author",
            error="could not be rebased onto main",
        )
        self._populate_queue(entry)

        # 1. The stalled/#241 dispatch path picks the sealed-aware branch —
        #    the same `dispatch_conflict_fix` every caller (coord merge's
        #    #241 sweep, `coord notify`'s stalled-pipeline arm) already uses.
        client = _FakeHTTPClient({"id": "sealed-fix-1"})
        fix = dispatch_conflict_fix(
            entry, Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        assert fix is not None
        assert fix.issue_title.startswith(SEALED_CONFLICT_FIX_TITLE_PREFIX)
        _, payload = client.calls[0]
        assert payload["system_prompt"] == SEALED_MANIFEST_CONFLICT_SYSTEM_PROMPT
        assert "manifest.yml" in payload["briefing"]

        # 2. The dispatched worker rebases, resolves the manifest.yml
        #    conflict additively (keeping both issues' blocks), pushes, and
        #    exits 0 — simulated here as the reconcile hook's success path,
        #    the same hook every conflict-fix completion (real or test)
        #    goes through.
        from coord.reconcile import _on_conflict_fix_done
        from coord import merge_queue as mq

        fix.status = "done"
        _on_conflict_fix_done(fix, succeeded=True)

        # 3. No human touched anything: the merge entry is back to PENDING,
        #    exactly what `coord merge`'s next sweep needs to retry the
        #    merge — this IS "merges without a human" for the parts under
        #    this repo's control (the actual `gh pr merge` call is a wire
        #    call this test doesn't reach, same as every other reconcile-hook
        #    test in this file).
        landed = mq.load_queue()[0]
        assert landed.state == PENDING
        assert landed.error is None

    def test_conflict_reaching_a_test_body_refuses_and_escalates(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """The same dispatch is attempted, but this time the conflict also
        touches a test body — outside the sealed resolver's authority. The
        worker refuses (STUCK with the sealed-scope marker) instead of
        guessing, and the entry escalates to HUMAN_REQUIRED exactly like any
        other conflict-fix failure — no silent guess at a sealed file."""
        entry = _entry(
            assignment_type="mock-author",
            error="could not be rebased onto main",
        )
        self._populate_queue(entry)

        client = _FakeHTTPClient({"id": "sealed-fix-2"})
        fix = dispatch_conflict_fix(
            entry, Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        assert fix is not None

        # The worker's own log carries the sealed-scope STUCK marker — used
        # here only to confirm the marker this dispatch's briefing documents
        # is exactly what `sealed_scope_verdict_in_text` recognizes; the
        # reconcile hook's own conflict-fix failure path (shared with every
        # other class of conflict-fix failure) is what actually parks the
        # entry, exercised via `succeeded=False` below.
        worker_log = (
            "STATUS: rebase started\n"
            f"STUCK: {SEALED_SCOPE_STUCK_MARKER} "
            "tests/acceptance/ms-4/audit_test.rs:10-22 — conflict is in a "
            "test body, not manifest.yml"
        )
        assert sealed_scope_verdict_in_text(worker_log) is True

        from coord.reconcile import _on_conflict_fix_done
        from coord import merge_queue as mq

        fix.status = "failed"
        _on_conflict_fix_done(fix, succeeded=False)

        parked = mq.load_queue()[0]
        assert parked.state == HUMAN_REQUIRED
        assert "Manual rebase required" in (parked.error or "")
