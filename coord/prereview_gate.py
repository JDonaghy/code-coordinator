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
which shares its actual terminal-write seam,
``coord.review._record_terminal_mechanical_verdict``, with the #3180
``_record_mechanical_review_verdict`` path — #3674 review round 1: these
used to be two independently-maintained copies of the same write, now one)
— the reviewer never sees these findings at all, which is the whole point:
a check a script can run with zero tolerance should never cost a paid
review round just to be re-confirmed.

Every check here is a pure function over diff text (plus, for semver/
features, an injectable command runner) — no network, no ``gh`` shell-out,
so the whole gate is free to run on every completed work leg regardless of
whether a repo opts in (an unconfigured/disabled repo's
:class:`PrereviewGateRepoConfig` makes :func:`run_prereview_gate` a no-op).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterator

from coord.config import PrereviewGateRepoConfig

log = logging.getLogger(__name__)

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
    # "used to" (below) already matches "used to be" as a substring — a
    # separate "used to be" entry here would be dead weight (#3674 review
    # round 1 nit).
    "used to",
    "before this change",
    "no longer",
    "formerly",
    "old behavior",
    "old behaviour",
    "in the past",
    "originally",
)

#: #3674 review round 1 (nit): `#` is included unconditionally here even
#: though for quadraui (Rust, the repo this feature is explicitly built
#: for) a `#`-prefixed line is usually an attribute/macro
#: (`#[derive(Debug)]`, `#![no_std]`), not a comment — a real, if
#: low-probability, false-positive source if such a line's text ever
#: contains `#\d+` or a history-phrase substring. Unlike that risk,
#: `comment_prefixes` IS now configurable per repo (mirrors
#: `issue_ref_pattern`/`history_phrases`) via
#: `PrereviewGateRepoConfig.comment_prefixes` — a Rust-heavy repo that hits
#: a real false positive can override this default instead of living with
#: it.
_COMMENT_PREFIXES: tuple[str, ...] = ("//", "#", "/*", "*", "<!--", "--")


def _is_comment_line(stripped: str, comment_prefixes: tuple[str, ...]) -> bool:
    return any(stripped.startswith(p) for p in comment_prefixes)


def find_comment_lint_violations(
    diff_text: str | None,
    *,
    issue_ref_pattern: str = r"#\d+",
    history_phrases: tuple[str, ...] = DEFAULT_HISTORY_PHRASES,
    comment_prefixes: tuple[str, ...] = _COMMENT_PREFIXES,
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
        if not stripped or not _is_comment_line(stripped, comment_prefixes):
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
    (check disabled) or the diff touches no ``pub`` item at all.

    #3674 review round 1 (nit): *changelog_path* matches by EXACT string
    only (see :func:`find_smoke_spec_violations`'s docstring for the same
    note) — deliberate for a single, explicit path, not a directory."""
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
    from); only an existing file's hunk losing a line trips this.

    #3674 review round 1 (nit): *smoke_spec_paths* matches by EXACT string
    only, no prefix/directory support — deliberate for a short, explicit
    per-repo list; unlike `additive_only_entrypoints`/sealed-path elsewhere
    in `coord/review.py`, this never needs to match "everything under a
    directory"."""
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

# #3674 review round 1 (blocking): the one other `shell=True` subprocess
# pattern in this codebase (`coord/acceptance_drivers.py`'s `_run_setup`/
# `_run_generic`) always passes an explicit `timeout=` and catches
# `subprocess.TimeoutExpired`/`OSError` around the call — a hung
# `cargo semver-checks`/`cargo check` or a nonexistent `cwd` must never hang
# or crash this gate the same way. 600s mirrors that module's own default
# acceptance-driver timeout order of magnitude; there's no per-repo override
# yet since no repo has opted into `semver_command`/`feature_matrix` in
# production.
_DEFAULT_COMMAND_TIMEOUT_SECONDS = 600


def _default_command_runner(command: str, cwd: str | None) -> tuple[bool, str]:
    """Real ``subprocess`` runner — the default when a repo configures a
    *semver_command*/*feature_matrix* but the caller injects no stub.
    Exercised in production only when a repo actually opts in; every test in
    this repo injects a fake runner instead.

    Never raises: a timeout or a ``cwd`` that doesn't exist (or any other
    failure to even start the subprocess) is reported as an ordinary
    ``(False, <reason>)`` finding, exactly like a non-zero exit — the only
    thing a caller can rely on this function doing is returning, not
    raising (#3674 review round 1: the previous version had neither a
    timeout nor an except clause).
    """
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True,
            check=False, timeout=_DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"command timed out after {_DEFAULT_COMMAND_TIMEOUT_SECONDS}s: "
            f"{command!r} (cwd={cwd!r})"
        )
    except OSError as e:
        # Covers a nonexistent/unreadable `cwd` (FileNotFoundError is an
        # OSError subclass) and a shell that fails to even start.
        return False, f"command failed to start: {command!r} (cwd={cwd!r}): {e}"
    return result.returncode == 0, (result.stdout + result.stderr)


def _default_head_sha_fetcher(repo_path: str) -> str | None:
    """Best-effort ``git rev-parse HEAD`` in *repo_path*, via the same
    timeout-/exception-safe :func:`_default_command_runner` — used to
    VERIFY a local checkout actually holds the leg's own commit before
    trusting it for a semver/feature-matrix ``subprocess.run`` (see
    :func:`run_prereview_gate`'s docstring). Returns ``None`` on ANY
    failure (missing path, not a git repo, git not installed, timeout,
    ...) — never raises; the caller treats ``None`` exactly like a
    mismatch: "cannot verify, skip the local-execution checks"."""
    ok, output = _default_command_runner("git rev-parse HEAD", repo_path)
    if not ok:
        return None
    stripped = output.strip()
    return stripped.splitlines()[-1] if stripped else None


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
    expected_head_sha: str | None = None,
    semver_runner: CommandRunner | None = None,
    feature_matrix_runner: CommandRunner | None = None,
    head_sha_fetcher: Callable[[str], "str | None"] | None = None,
) -> GateResult:
    """Run every check *repo_config* enables against *diff_text*.

    A disabled repo (``repo_config.enabled`` is ``False``, the default for
    any repo that never configures a ``prereview_gate:`` block) is a no-op:
    returns ``GateResult(passed=True)`` without inspecting the diff at all.

    #3674 review round 1 (blocking): *semver_command*/*feature_matrix* are
    the only two checks that ever shell out against *repo_path* — every
    other check here is a pure function over *diff_text*. Running a real
    command against *repo_path* is only sound when *repo_path* is actually
    known to hold the leg's own commit; nothing upstream of this function
    fetches or checks out that branch there first, so *repo_path* could just
    as easily be some unrelated directory sitting on a completely different
    machine/branch/commit. Rather than trust it blindly, this function
    VERIFIES it first: when *expected_head_sha* is given (the caller's own
    independently-fetched branch HEAD, e.g. ``dispatch_review``'s
    ``review_head_sha``), *repo_path*'s own ``git rev-parse HEAD`` (via
    *head_sha_fetcher*, defaulting to a real ``git`` shell-out) must match
    it exactly before either command-based check is allowed to run. A
    missing *repo_path*, a missing *expected_head_sha*, a fetch failure, or
    a mismatch all resolve the same way: the semver/feature-matrix checks
    are SKIPPED (not failed — there's nothing to blame the diff for when
    this gate itself couldn't verify where it was standing) and a warning
    is logged so an operator can see the check never actually ran.
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
                comment_prefixes=repo_config.comment_prefixes,
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

    wants_local_checks = bool(repo_config.semver_command or repo_config.feature_matrix)
    local_checks_verified = False
    if wants_local_checks:
        if not repo_path:
            log.warning(
                "prereview_gate: semver_command/feature_matrix configured but "
                "no repo_path is available for this leg — skipping both "
                "checks rather than running them against an unverified "
                "working directory"
            )
        elif not expected_head_sha:
            log.warning(
                "prereview_gate: semver_command/feature_matrix configured "
                "but no expected_head_sha was supplied to verify repo_path "
                "%r against — skipping both checks",
                repo_path,
            )
        else:
            fetch_head = head_sha_fetcher or _default_head_sha_fetcher
            try:
                local_sha = fetch_head(repo_path)
            except Exception as e:  # noqa: BLE001 — a verification probe must
                # never itself crash the gate; treat any failure as "can't
                # verify, skip" exactly like a returned None/mismatch.
                log.warning(
                    "prereview_gate: HEAD verification for repo_path %r "
                    "raised %r — skipping semver/feature-matrix checks",
                    repo_path, e,
                )
                local_sha = None
            if local_sha == expected_head_sha:
                local_checks_verified = True
            else:
                log.warning(
                    "prereview_gate: repo_path %r HEAD (%r) does not match "
                    "this leg's own head (%r) — skipping semver/"
                    "feature-matrix checks rather than running them against "
                    "an unverified checkout",
                    repo_path, local_sha, expected_head_sha,
                )

    if repo_config.semver_command and local_checks_verified:
        findings.extend(
            find_semver_violations(
                semver_command=repo_config.semver_command,
                repo_path=repo_path,
                runner=semver_runner,
            )
        )
    if repo_config.feature_matrix and local_checks_verified:
        findings.extend(
            find_feature_matrix_violations(
                features=repo_config.feature_matrix,
                command_template=repo_config.feature_matrix_command_template,
                repo_path=repo_path,
                runner=feature_matrix_runner,
            )
        )
    return GateResult(passed=not findings, findings=findings)
