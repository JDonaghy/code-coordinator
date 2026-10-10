"""Tests for coord/prereview_gate.py (#3674)."""

from __future__ import annotations

import coord.prereview_gate as prereview_gate
from coord.config import PrereviewGateRepoConfig
from coord.prereview_gate import (
    GateResult,
    _default_command_runner,
    find_changelog_violations,
    find_comment_lint_violations,
    find_feature_matrix_violations,
    find_semver_violations,
    find_smoke_spec_violations,
    run_prereview_gate,
)


# ── added-lines comment lint ────────────────────────────────────────────────


def test_comment_lint_flags_issue_ref_in_added_line() -> None:
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,2 +1,3 @@\n"
        " fn existing() {}\n"
        "+// Issue #123: this is why we do it this way\n"
    )
    violations = find_comment_lint_violations(diff)
    assert len(violations) == 1
    assert "src/lib.rs:2" in violations[0]
    assert "issue reference" in violations[0]


def test_comment_lint_does_not_flag_unchanged_context_line() -> None:
    """The acceptance bar from the issue: `// Issue #123:` in an added line
    is flagged, but the EXACT SAME text sitting as a context (unchanged)
    line in the same diff must not be."""
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,3 +1,4 @@\n"
        " // Issue #123: pre-existing, untouched by this diff\n"
        " fn existing() {}\n"
        "+fn new_fn() {}\n"
    )
    violations = find_comment_lint_violations(diff)
    assert violations == []


def test_comment_lint_does_not_flag_removed_line() -> None:
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,2 +1,1 @@\n"
        "-// Issue #123: being deleted, not added\n"
        " fn existing() {}\n"
    )
    violations = find_comment_lint_violations(diff)
    assert violations == []


def test_comment_lint_flags_history_phrase() -> None:
    diff = (
        "diff --git a/coord/foo.py b/coord/foo.py\n"
        "--- a/coord/foo.py\n"
        "+++ b/coord/foo.py\n"
        "@@ -1,1 +1,2 @@\n"
        " def f(): pass\n"
        "+# previously this used a different algorithm\n"
    )
    violations = find_comment_lint_violations(diff)
    assert len(violations) == 1
    assert "history phrase" in violations[0]


def test_comment_lint_ignores_added_non_comment_line_with_hash() -> None:
    diff = (
        "diff --git a/coord/foo.py b/coord/foo.py\n"
        "--- a/coord/foo.py\n"
        "+++ b/coord/foo.py\n"
        "@@ -1,1 +1,2 @@\n"
        " def f(): pass\n"
        "+title = \"Issue #123 tracker\"\n"
    )
    violations = find_comment_lint_violations(diff)
    assert violations == []


def test_comment_lint_empty_diff() -> None:
    assert find_comment_lint_violations(None) == []
    assert find_comment_lint_violations("") == []


def test_comment_lint_comment_prefixes_configurable() -> None:
    """#3674 review round 1 (nit): a repo can override `comment_prefixes`
    to drop a marker that collides with its own syntax (e.g. `#` in Rust
    being an attribute/macro, not a comment)."""
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,1 +1,2 @@\n"
        " fn existing() {}\n"
        "+#[allow(dead_code)] // see #42\n"
    )
    # Default prefixes: `#[allow...` starts with `#`, so it's treated as a
    # comment line and its trailing `// see #42` content trips the lint.
    assert find_comment_lint_violations(diff) != []
    # With `#` removed from the configured prefixes, the same line is no
    # longer classified as a comment at all.
    assert find_comment_lint_violations(diff, comment_prefixes=("//", "/*", "*")) == []


# ── CHANGELOG ────────────────────────────────────────────────────────────────


def test_changelog_missing_when_pub_item_added_without_entry() -> None:
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,1 +1,2 @@\n"
        " fn existing() {}\n"
        "+pub fn new_api() {}\n"
    )
    violations = find_changelog_violations(diff, changelog_path="CHANGELOG.md")
    assert len(violations) == 1
    assert "CHANGELOG.md" in violations[0]


def test_changelog_ok_when_changelog_touched_in_same_diff() -> None:
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,1 +1,2 @@\n"
        " fn existing() {}\n"
        "+pub fn new_api() {}\n"
        "diff --git a/CHANGELOG.md b/CHANGELOG.md\n"
        "--- a/CHANGELOG.md\n"
        "+++ b/CHANGELOG.md\n"
        "@@ -1,1 +1,2 @@\n"
        " # Changelog\n"
        "+- Added `new_api`.\n"
    )
    assert find_changelog_violations(diff, changelog_path="CHANGELOG.md") == []


def test_changelog_check_disabled_when_path_unset() -> None:
    diff = "+pub fn new_api() {}\n"
    assert find_changelog_violations(diff, changelog_path=None) == []


def test_changelog_ok_when_no_pub_item_changed() -> None:
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,1 +1,2 @@\n"
        " fn existing() {}\n"
        "+fn private_helper() {}\n"
    )
    assert find_changelog_violations(diff, changelog_path="CHANGELOG.md") == []


# ── smoke specs: additive-only ───────────────────────────────────────────────


def test_smoke_spec_flags_non_additive_edit() -> None:
    diff = (
        "diff --git a/tests/smoke/lane1.rs b/tests/smoke/lane1.rs\n"
        "--- a/tests/smoke/lane1.rs\n"
        "+++ b/tests/smoke/lane1.rs\n"
        "@@ -1,2 +1,1 @@\n"
        "-expect_step(\"must still work\");\n"
        " fn lane1() {}\n"
    )
    violations = find_smoke_spec_violations(diff, ("tests/smoke/lane1.rs",))
    assert len(violations) == 1
    assert "non-additive" in violations[0]


def test_smoke_spec_allows_pure_addition() -> None:
    diff = (
        "diff --git a/tests/smoke/lane1.rs b/tests/smoke/lane1.rs\n"
        "--- a/tests/smoke/lane1.rs\n"
        "+++ b/tests/smoke/lane1.rs\n"
        "@@ -1,1 +1,2 @@\n"
        " fn lane1() {}\n"
        "+expect_step(\"a brand new step\");\n"
    )
    assert find_smoke_spec_violations(diff, ("tests/smoke/lane1.rs",)) == []


def test_smoke_spec_ignores_unconfigured_paths() -> None:
    diff = (
        "diff --git a/tests/smoke/other.rs b/tests/smoke/other.rs\n"
        "--- a/tests/smoke/other.rs\n"
        "+++ b/tests/smoke/other.rs\n"
        "@@ -1,2 +1,1 @@\n"
        "-expect_step(\"removed\");\n"
        " fn other() {}\n"
    )
    assert find_smoke_spec_violations(diff, ("tests/smoke/lane1.rs",)) == []


# ── semver / feature matrix: injectable runners ─────────────────────────────


def test_semver_check_skipped_when_unconfigured() -> None:
    assert find_semver_violations(semver_command=None, repo_path="/repo") == []


def test_semver_check_reports_failure_from_injected_runner() -> None:
    """#2096 'a gate must be able to fail': the failing branch must be
    reachable, not just the passing one — this runner always fails."""
    calls = []

    def fake_runner(command: str, cwd: str | None) -> tuple[bool, str]:
        calls.append((command, cwd))
        return False, "API break: removed pub fn old_api()"

    violations = find_semver_violations(
        semver_command="cargo semver-checks check-release",
        repo_path="/repo/quadraui",
        runner=fake_runner,
    )
    assert len(violations) == 1
    assert "removed pub fn old_api" in violations[0]
    assert calls == [("cargo semver-checks check-release", "/repo/quadraui")]


def test_semver_check_passes_when_runner_reports_ok() -> None:
    violations = find_semver_violations(
        semver_command="cargo semver-checks check-release",
        repo_path="/repo",
        runner=lambda command, cwd: (True, ""),
    )
    assert violations == []


def test_feature_matrix_reports_each_failing_feature() -> None:
    def fake_runner(command: str, cwd: str | None) -> tuple[bool, str]:
        if "gtk" in command:
            return False, "error[E0432]: unresolved import"
        return True, ""

    violations = find_feature_matrix_violations(
        features=("gtk", "win-native"),
        command_template="cargo check --no-default-features --features {feature}",
        repo_path="/repo",
        runner=fake_runner,
    )
    assert len(violations) == 1
    assert "gtk" in violations[0]


def test_semver_check_default_runner_actually_shells_out_and_can_fail() -> None:
    """#2096 'a gate must be able to fail': with NO injected runner at all,
    the real default subprocess runner must observe an actual non-zero exit
    — not just skip the check or assume success from the absence of an
    exception."""
    violations = find_semver_violations(
        semver_command="exit 1", repo_path=None,
    )
    assert len(violations) == 1
    assert "semver check failed" in violations[0]


def test_semver_check_default_runner_passes_on_real_success() -> None:
    violations = find_semver_violations(semver_command="true", repo_path=None)
    assert violations == []


def test_feature_matrix_skipped_when_no_features_configured() -> None:
    assert find_feature_matrix_violations(
        features=(), command_template="cargo check --features {feature}", repo_path=None,
    ) == []


# ── #3674 review round 1 (blocking): default runner must never hang or
# raise — a timeout or a nonexistent `cwd` becomes an ordinary finding ──────


def test_default_command_runner_nonexistent_cwd_does_not_raise() -> None:
    ok, output = _default_command_runner("true", "/no/such/directory/at/all")
    assert ok is False
    assert "failed to start" in output


def test_default_command_runner_timeout_does_not_hang_or_raise(monkeypatch) -> None:
    monkeypatch.setattr(prereview_gate, "_DEFAULT_COMMAND_TIMEOUT_SECONDS", 0.2)
    ok, output = _default_command_runner("sleep 5", None)
    assert ok is False
    assert "timed out" in output


def test_default_command_runner_still_reports_a_real_failure() -> None:
    ok, output = _default_command_runner("exit 1", None)
    assert ok is False
    assert output == ""


# ── orchestration ────────────────────────────────────────────────────────────


def test_run_prereview_gate_noop_for_disabled_repo() -> None:
    result = run_prereview_gate(
        diff_text="+// Issue #1: bad comment\n",
        repo_config=PrereviewGateRepoConfig(enabled=False),
    )
    assert result == GateResult(passed=True, findings=[])


def test_run_prereview_gate_fails_on_comment_lint_violation() -> None:
    diff = (
        "diff --git a/coord/foo.py b/coord/foo.py\n"
        "--- a/coord/foo.py\n"
        "+++ b/coord/foo.py\n"
        "@@ -1,1 +1,2 @@\n"
        " def f(): pass\n"
        "+# see #42 for why\n"
    )
    result = run_prereview_gate(
        diff_text=diff,
        repo_config=PrereviewGateRepoConfig(enabled=True),
    )
    assert result.passed is False
    assert len(result.findings) == 1


def test_run_prereview_gate_combines_findings_from_multiple_checks() -> None:
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,1 +1,2 @@\n"
        " fn existing() {}\n"
        "+// previously this worked differently\n"
        "+pub fn new_api() {}\n"
    )
    result = run_prereview_gate(
        diff_text=diff,
        repo_config=PrereviewGateRepoConfig(enabled=True, changelog_path="CHANGELOG.md"),
    )
    assert result.passed is False
    assert len(result.findings) == 2


def test_run_prereview_gate_passes_clean_diff() -> None:
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,1 +1,2 @@\n"
        " fn existing() {}\n"
        "+fn helper() {}\n"
    )
    result = run_prereview_gate(
        diff_text=diff,
        repo_config=PrereviewGateRepoConfig(enabled=True, changelog_path="CHANGELOG.md"),
    )
    assert result == GateResult(passed=True, findings=[])


# ── #3674 review round 1 (blocking): semver/feature-matrix must never run
# against an unverified `repo_path` — `run_prereview_gate` checks the local
# checkout's own HEAD against `expected_head_sha` before trusting it ────────

_CLEAN_DIFF = (
    "diff --git a/src/lib.rs b/src/lib.rs\n"
    "--- a/src/lib.rs\n"
    "+++ b/src/lib.rs\n"
    "@@ -1,1 +1,2 @@\n"
    " fn existing() {}\n"
    "+fn helper() {}\n"
)


def test_semver_check_skipped_when_repo_path_missing() -> None:
    """No repo_path at all -> the semver command must never run, even
    though it's configured and would fail if it did."""
    calls = []

    def fake_runner(command: str, cwd: str | None) -> tuple[bool, str]:
        calls.append((command, cwd))
        return False, "should never be called"

    result = run_prereview_gate(
        diff_text=_CLEAN_DIFF,
        repo_config=PrereviewGateRepoConfig(enabled=True, semver_command="cargo semver-checks"),
        repo_path=None,
        expected_head_sha="deadbeef",
        semver_runner=fake_runner,
    )
    assert result == GateResult(passed=True, findings=[])
    assert calls == []


def test_semver_check_skipped_when_expected_head_sha_missing() -> None:
    """repo_path IS set but there's nothing to verify it against -> still
    skip rather than trust it blindly."""
    calls = []

    def fake_runner(command: str, cwd: str | None) -> tuple[bool, str]:
        calls.append((command, cwd))
        return False, "should never be called"

    result = run_prereview_gate(
        diff_text=_CLEAN_DIFF,
        repo_config=PrereviewGateRepoConfig(enabled=True, semver_command="cargo semver-checks"),
        repo_path="/work/api",
        expected_head_sha=None,
        semver_runner=fake_runner,
    )
    assert result == GateResult(passed=True, findings=[])
    assert calls == []


def test_semver_check_skipped_when_local_head_does_not_match() -> None:
    """repo_path's own HEAD (per head_sha_fetcher) disagrees with
    expected_head_sha -> this is the #3674 review round 1 scenario of
    "repo_path belongs to some other machine/branch/commit" — skip rather
    than run the command against the wrong checkout."""
    calls = []

    def fake_runner(command: str, cwd: str | None) -> tuple[bool, str]:
        calls.append((command, cwd))
        return False, "should never be called"

    result = run_prereview_gate(
        diff_text=_CLEAN_DIFF,
        repo_config=PrereviewGateRepoConfig(enabled=True, semver_command="cargo semver-checks"),
        repo_path="/work/api",
        expected_head_sha="deadbeef",
        semver_runner=fake_runner,
        head_sha_fetcher=lambda path: "some-other-sha",
    )
    assert result == GateResult(passed=True, findings=[])
    assert calls == []


def test_semver_check_runs_when_local_head_matches() -> None:
    """The one case the command is actually allowed to run: repo_path's own
    HEAD matches expected_head_sha exactly."""
    calls = []

    def fake_runner(command: str, cwd: str | None) -> tuple[bool, str]:
        calls.append((command, cwd))
        return False, "API break: removed pub fn old_api()"

    result = run_prereview_gate(
        diff_text=_CLEAN_DIFF,
        repo_config=PrereviewGateRepoConfig(enabled=True, semver_command="cargo semver-checks"),
        repo_path="/work/api",
        expected_head_sha="deadbeef",
        semver_runner=fake_runner,
        head_sha_fetcher=lambda path: "deadbeef",
    )
    assert result.passed is False
    assert calls == [("cargo semver-checks", "/work/api")]


def test_semver_check_skipped_when_head_sha_fetcher_raises() -> None:
    """A verification probe that itself raises must never crash the gate —
    treated exactly like "cannot verify, skip"."""
    def boom(path: str) -> str | None:
        raise RuntimeError("git not installed")

    result = run_prereview_gate(
        diff_text=_CLEAN_DIFF,
        repo_config=PrereviewGateRepoConfig(enabled=True, semver_command="cargo semver-checks"),
        repo_path="/work/api",
        expected_head_sha="deadbeef",
        semver_runner=lambda command, cwd: (False, "should never be called"),
        head_sha_fetcher=boom,
    )
    assert result == GateResult(passed=True, findings=[])


def test_feature_matrix_also_gated_by_head_sha_verification() -> None:
    calls = []

    def fake_runner(command: str, cwd: str | None) -> tuple[bool, str]:
        calls.append((command, cwd))
        return True, ""

    result = run_prereview_gate(
        diff_text=_CLEAN_DIFF,
        repo_config=PrereviewGateRepoConfig(enabled=True, feature_matrix=("gtk",)),
        repo_path="/work/api",
        expected_head_sha=None,
        feature_matrix_runner=fake_runner,
    )
    assert result == GateResult(passed=True, findings=[])
    assert calls == []
