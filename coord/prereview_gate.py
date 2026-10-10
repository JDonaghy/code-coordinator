"""Pre-review mechanical gate (#3674).

``dispatch_review`` (``coord/review.py``) already has one mechanical
short-circuit — the #3180 ``_mechanical_mandatory_verdict`` check, which
catches a sealed-path tamper or a coordinator-owned-doc edit by inspecting
the diff, BEFORE a real reviewer session is ever spent re-deriving the same
answer by hand. This module is a second, repo-configured instance of that
same idea, generalised to a family of checks a 2026-10-09 sample of
request-changes rounds showed a script could catch for free:

- **Added-lines comment lint** — zero tolerance on the diff's *added*
  comment lines for ``#\\d+`` issue references and "history" phrases
  ("previously", "used to", ...), regardless of any per-module threshold a
  repo's own ratchet tool (e.g. quadraui's ``tools/comment_history_lint.py``)
  applies. A threshold lets a *new* violation slip through as long as the
  module stays under budget; this is a hard zero on anything the diff itself
  adds.
- **CHANGELOG** — when a diff adds/changes a ``pub`` item, a CHANGELOG entry
  must be part of the same diff.
- **Semver** — an injectable, repo-configured command (``cargo
  semver-checks`` for quadraui) run against the leg's own checkout.
- **Smoke specs** — a sealed smoke-spec file may only be ADDED to, never
  edited/removed, mirroring ``coord.review``'s own additive-only Tier-2
  lane-entrypoint rule (#3509) but driven by this gate's own config instead
  of ``AcceptanceConfig``.
- **Features** — the per-feature ``cargo check`` matrix a repo's own CI runs,
  likewise injectable/repo-configured.

All of these are deterministic, script-checkable rules — never an LLM
judgment call — so a reviewer should never have to re-derive them by hand.
:func:`run_prereview_gate` is the single entry point ``dispatch_review``
calls, before it ever spends an HTTP POST on a candidate reviewer machine:
a non-empty :class:`GateResult.findings` means the leg is bounced straight
back to the worker (see ``coord.review._record_prereview_gate_verdict``,
which reuses the exact same terminal-write seam
``_record_mechanical_review_verdict`` (#3180) already established) — the
reviewer never sees these findings at all, which is the whole point: a
check a script can run with zero tolerance should never cost a paid review
round just to be re-confirmed.

Every check here is a pure function over diff text (plus, for semver/
features, an injectable command runner) — no network, no ``gh`` shell-out,
so the whole gate is free to run on every completed work leg regardless of
whether a repo opts in (an unconfigured/disabled repo's
:class:`PrereviewGateRepoConfig` makes :func:`run_prereview_gate` a no-op).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterator

from coord.config import PrereviewGateRepoConfig

# ── diff parsing helpers ─────────────────────────────────────────────────────
#
# Deliberately minimal unified-diff parsing — just enough to answer "what file
# and line number is this ADDED line in" and "what files does this diff
# touch", the two primitives every check below needs. No dependency on
# `coord.github_ops` (which shells out to `gh`) — these are pure functions over
# whatever diff text the caller already fetched.

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_PLUS_PLUS_PLUS_RE = re.compile(r"^\+\+\+ (?:b/(.+)|/dev/null)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _iter_diff_added_lines(diff_text: str) -> Iterator[tuple[str, int, str]]:
    """Yield ``(file_path, new_line_number, content)`` for every ADDED line.

    *content* excludes the leading ``+``. Skips the ``+++``/``---`` file
    headers themselves (never real content) and anything outside a hunk
    (e.g. the ``diff --git`` line) by only counting lines once a ``@@`` hunk
    header has been seen for the current file.
    """
    current_file: str | None = None
    new_lineno: int | None = None
    for raw_line in diff_text.splitlines():
        git_m = _DIFF_GIT_RE.match(raw_line)
        if git_m:
            current_file = git_m.group(2)
            new_lineno = None
            continue
        plus_m = _PLUS_PLUS_PLUS_RE.match(raw_line)
        if plus_m:
            current_file = plus_m.group(1)  # None for /dev/null (deleted file)
            new_lineno = None
            continue
        if raw_line.startswith("---"):
            continue
        hunk_m = _HUNK_HEADER_RE.match(raw_line)
        if hunk_m:
            new_lineno = int(hunk_m.group(1))
            continue
        if new_lineno is None or current_file is None:
            continue
        if raw_line.startswith("+"):
            yield current_file, new_lineno, raw_line[1:]
            new_lineno += 1
        elif raw_line.startswith("-"):
            continue  # removed line — doesn't exist in the new file
        else:
            new_lineno += 1  # context line, present in both


def _diff_touched_files(diff_text: str) -> set[str]:
    """Every file path a diff touches (added, modified, or as the new side
    of a rename) — derived from ``+++ b/<path>`` headers so a deleted file
    (``+++ /dev/null``) is correctly excluded."""
    touched: set[str] = set()
    for raw_line in diff_text.splitlines():
        m = _PLUS_PLUS_PLUS_RE.match(raw_line)
        if m and m.group(1):
            touched.add(m.group(1))
    return touched


def _diff_has_removed_or_modified_line(diff_text: str, file_path: str) -> bool:
    """True when *file_path*'s hunks in *diff_text* contain any ``-`` line
    (a removal) — i.e. the diff is NOT purely additive for this file."""
    in_target = False
    for raw_line in diff_text.splitlines():
        git_m = _DIFF_GIT_RE.match(raw_line)
        if git_m:
            in_target = git_m.group(2) == file_path
            continue
        plus_m = _PLUS_PLUS_PLUS_RE.match(raw_line)
        if plus_m:
            in_target = plus_m.group(1) == file_path
            continue
        if not in_target:
            continue
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("-"):
            return True
    return False


# ── check 1: added-lines comment lint ───────────────────────────────────────

DEFAULT_HISTORY_PHRASES: tuple[str, ...] = (
    "previously",
    "used to",
    "before this change",
    "no longer",
    "formerly",
    "old behavior",
    "old behaviour",
    "used to be",
    "in the past",
    "originally",
)

_COMMENT_PREFIXES: tuple[str, ...] = ("//", "#", "/*", "*", "<!--", "--")


def _is_comment_line(stripped: str) -> bool:
    return any(stripped.startswith(p) for p in _COMMENT_PREFIXES)


def find_comment_lint_violations(
    diff_text: str | None,
    *,
    issue_ref_pattern: str = r"#\d+",
    history_phrases: tuple[str, ...] = DEFAULT_HISTORY_PHRASES,
) -> list[str]:
    """Zero-tolerance check: does any ADDED comment line reference an issue
    number or narrate the diff's own history ("previously", "used to", ...)?

    Only ADDED lines are ever inspected — an unchanged (context) line or a
    removed line carrying the exact same text is never flagged, regardless
    of what it says; the rule is about what THIS diff newly introduces, not
    about pre-existing comments it merely leaves untouched.
    """
    if not diff_text:
        return []
    issue_ref_re = re.compile(issue_ref_pattern)
    violations: list[str] = []
    for file_path, lineno, content in _iter_diff_added_lines(diff_text):
        stripped = content.strip()
        if not stripped or not _is_comment_line(stripped):
            continue
        lower = stripped.lower()
        hit_issue_ref = issue_ref_re.search(stripped) is not None
        hit_phrase = next((p for p in history_phrases if p in lower), None)
        if hit_issue_ref or hit_phrase:
            why = "issue reference" if hit_issue_ref else f"history phrase {hit_phrase!r}"
            violations.append(
                f"{file_path}:{lineno}: added comment contains a {why}: {stripped!r}"
            )
    return violations


# ── check 2: CHANGELOG ───────────────────────────────────────────────────────

_PUB_ITEM_RE = re.compile(r"^\s*pub\s+(fn|struct|enum|trait|const|type|mod|static)\b")


def find_changelog_violations(
    diff_text: str | None,
    *,
    changelog_path: str | None,
    pub_item_re: re.Pattern[str] = _PUB_ITEM_RE,
) -> list[str]:
    """When *diff_text* adds/changes a ``pub`` item, *changelog_path* must be
    part of the same diff. Returns ``[]`` when *changelog_path* is unset
    (check disabled) or the diff touches no ``pub`` item at all."""
    if not changelog_path or not diff_text:
        return []
    if changelog_path in _diff_touched_files(diff_text):
        return []
    pub_hits = [
        f"{file_path}:{lineno}"
        for file_path, lineno, content in _iter_diff_added_lines(diff_text)
        if pub_item_re.match(content)
    ]
    if not pub_hits:
        return []
    return [
        f"public item(s) changed ({', '.join(pub_hits)}) with no {changelog_path} "
        "entry in this diff"
    ]


# ── check 3: sealed smoke specs — additive-only ─────────────────────────────


def find_smoke_spec_violations(
    diff_text: str | None,
    smoke_spec_paths: tuple[str, ...],
) -> list[str]:
    """A sealed smoke-spec file may only be ADDED to — any ``-`` line inside
    one of *smoke_spec_paths* (a deleted/rewritten step) is a violation.
    A wholly new file under one of these paths is fine (nothing to remove
    from); only an existing file's hunk losing a line trips this."""
    if not smoke_spec_paths or not diff_text:
        return []
    violations: list[str] = []
    for file_path in sorted(_diff_touched_files(diff_text)):
        if file_path not in smoke_spec_paths:
            continue
        if _diff_has_removed_or_modified_line(diff_text, file_path):
            violations.append(
                f"{file_path}: non-additive edit to a sealed smoke-spec file "
                "(only adding new lines is allowed)"
            )
    return violations


# ── checks 4 & 5: semver / feature matrix — injectable command runners ──────

CommandRunner = Callable[[str, "str | None"], tuple[bool, str]]


def _default_command_runner(command: str, cwd: str | None) -> tuple[bool, str]:
    """Real ``subprocess`` runner — the default when a repo configures a
    *semver_command*/*feature_matrix* but the caller injects no stub.
    Exercised in production only when a repo actually opts in; every test in
    this repo injects a fake runner instead."""
    result = subprocess.run(
        command, shell=True, cwd=cwd, capture_output=True, text=True, check=False,
    )
    return result.returncode == 0, (result.stdout + result.stderr)


def find_semver_violations(
    *,
    semver_command: str | None,
    repo_path: str | None,
    runner: CommandRunner | None = None,
) -> list[str]:
    """Run *semver_command* (e.g. ``cargo semver-checks check-release``) and
    report a finding iff it exits non-zero. ``[]`` when *semver_command* is
    unset — this check never runs for a repo that hasn't configured one."""
    if not semver_command:
        return []
    run = runner or _default_command_runner
    ok, output = run(semver_command, repo_path)
    if ok:
        return []
    return [f"semver check failed ({semver_command!r}): {output.strip()[:500]}"]


def find_feature_matrix_violations(
    *,
    features: tuple[str, ...],
    command_template: str,
    repo_path: str | None,
    runner: CommandRunner | None = None,
) -> list[str]:
    """Run ``command_template.format(feature=f)`` for each *f* in *features*
    (e.g. ``cargo check --no-default-features --features {feature}``) and
    report one finding per feature whose check exits non-zero. ``[]`` when
    *features* is empty — this check never runs for a repo that configures
    no feature matrix."""
    if not features:
        return []
    run = runner or _default_command_runner
    violations: list[str] = []
    for feature in features:
        command = command_template.format(feature=feature)
        ok, output = run(command, repo_path)
        if not ok:
            violations.append(
                f"feature {feature!r} failed ({command!r}): {output.strip()[:500]}"
            )
    return violations


# ── orchestration ────────────────────────────────────────────────────────────


@dataclass
class GateResult:
    """The outcome of :func:`run_prereview_gate` for one completed work leg.

    *passed* is ``True`` iff every enabled check produced zero findings —
    this is the ONLY question ``dispatch_review`` asks (#2096 "a gate must
    be able to fail": there is no default/permissive branch here, a missing
    or misconfigured check simply contributes no findings and plays no part
    in *passed*, it is never silently treated as "passing" once a finding
    exists). *findings* is the flattened, human-readable list across every
    check that ran, in check order (comment lint, CHANGELOG, smoke specs,
    semver, feature matrix) — empty exactly when *passed* is ``True``.
    """

    passed: bool
    findings: list[str] = field(default_factory=list)


def run_prereview_gate(
    *,
    diff_text: str | None,
    repo_config: PrereviewGateRepoConfig,
    repo_path: str | None = None,
    semver_runner: CommandRunner | None = None,
    feature_matrix_runner: CommandRunner | None = None,
) -> GateResult:
    """Run every check *repo_config* enables against *diff_text*.

    A disabled repo (``repo_config.enabled`` is ``False``, the default for
    any repo that never configures a ``prereview_gate:`` block) is a no-op:
    returns ``GateResult(passed=True)`` without inspecting the diff at all.
    """
    if not repo_config.enabled or not diff_text:
        return GateResult(passed=True)

    findings: list[str] = []
    if repo_config.comment_lint:
        findings.extend(
            find_comment_lint_violations(
                diff_text,
                issue_ref_pattern=repo_config.issue_ref_pattern,
                history_phrases=repo_config.history_phrases,
            )
        )
    if repo_config.changelog_path:
        findings.extend(
            find_changelog_violations(diff_text, changelog_path=repo_config.changelog_path)
        )
    if repo_config.smoke_spec_paths:
        findings.extend(
            find_smoke_spec_violations(diff_text, repo_config.smoke_spec_paths)
        )
    if repo_config.semver_command:
        findings.extend(
            find_semver_violations(
                semver_command=repo_config.semver_command,
                repo_path=repo_path,
                runner=semver_runner,
            )
        )
    if repo_config.feature_matrix:
        findings.extend(
            find_feature_matrix_violations(
                features=repo_config.feature_matrix,
                command_template=repo_config.feature_matrix_command_template,
                repo_path=repo_path,
                runner=feature_matrix_runner,
            )
        )
    return GateResult(passed=not findings, findings=findings)
