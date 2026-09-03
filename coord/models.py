"""Dataclasses for the coordinator: repos, machines, assignments, board."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# #316: pattern that distinguishes a file-path value for `new_issue_guidance`
# from inline markdown text.  Matches paths like `docs/ISSUE_GUIDANCE.md` or
# `GUIDANCE.txt` but not multi-line or space-containing strings.
#
# The negative lookaheads reject (a) traversal sequences (`../`) and (b) any
# value starting with `/` or `\` (an absolute path).  Both protections matter
# because `Path("/repo") / "/etc/passwd.md"` silently discards the base and
# returns `Path("/etc/passwd.md")`, so absolute paths would otherwise escape
# the repo root just as effectively as `../`.  `resolve_new_issue_guidance`
# adds a second belt-and-braces check via `Path.resolve()` containment.
_GUIDANCE_PATH_RE: re.Pattern[str] = re.compile(
    r"^(?![/\\])(?!.*\.\.[/\\])[\w./\-]+\.(md|txt)$", re.IGNORECASE
)


@dataclass
class WorkerPermissionsConfig:
    """Per-repo allow/deny lists for worker commands.

    When ``deny`` is non-empty the coordinator injects a "forbidden commands"
    section into the worker system prompt so that ``claude -p`` refuses to run
    the listed patterns.  An empty ``deny`` list (``deny: []``) means no
    restrictions.
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


class _UatPreviewVars(dict):
    """``dict`` for ``str.format_map`` that leaves an unknown ``{placeholder}``
    unrendered instead of raising ``KeyError`` (see ``Repo.uat_preview``)."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class Repo:
    name: str
    github: str
    depends_on: list[str] = field(default_factory=list)
    default_branch: str = "main"
    # #934 (Pipeline v2 Phase 4, docs/PIPELINE_V2.md "Git model"): when set,
    # this repo has opted into the develop + feature-branch-per-milestone
    # git model — `develop` is the integration branch (`default_branch`
    # becomes the release branch) and milestone issues branch off
    # `feature/ms-NN` (see coord/branch_model.py) instead of
    # `default_branch` directly. `None` (the default) means the repo is
    # unaffected: every branch-resolution call site falls back to today's
    # flat `default_branch` behavior. This is the opt-in guard that keeps
    # in-flight work on `main` from breaking when `develop` appears for
    # repos that adopt the new model.
    develop_branch: str | None = None
    build_command: str | None = None
    test_command: str | None = None
    # #2091: the command this repo's CI actually runs — the "CI-equivalent"
    # suite.  The Test stage (`coord.smoke`) prefers this over
    # `test_command`/`smoke_tests.default_command`, so a green Test verdict
    # means the same thing a green CI run does.
    #
    # Why this is a SEPARATE field and not just "set test_command to the CI
    # command": `test_command` is also what a human runs locally and what
    # several narrower call sites use; it is routinely (and legitimately) a
    # fast subset — coord-portal's was a ~2 min slice while CI ran a 45 min
    # `npm run test:e2e` across two Playwright projects.  That divergence is
    # fine as long as the Test GATE runs the wider one, which is what this
    # field declares.  Leave it unset and the gate falls back to the old
    # behaviour, but `dispatch_smoke` logs that the verdict it is about to
    # produce is narrower than CI (see `resolve_smoke_command`).
    ci_command: str | None = None
    # #296: optional shell command to interactively run the app for manual
    # smoke testing.  Surfaced in the TUI Test stage detail panel so the
    # tester knows exactly what to launch.
    run_cmd: str | None = None
    worker_permissions: WorkerPermissionsConfig | None = None
    housekeeping: list[str] = field(default_factory=list)
    coordinator_only_files: list[str] = field(default_factory=list)
    # #268: repos a worker may reference for context but doesn't actually
    # build against.  Common cases: sister projects extracted from a
    # common ancestor (quadraui ← vimcode), reference implementations,
    # "lift X out of Y into Z" issues.
    #
    # Honoured by the freshness check (pulled alongside `depends_on`)
    # but ignored by the cycle detector — so a repo can list a sibling
    # that already points back via `depends_on` without tripping the
    # validator.  Reference entries do NOT walk transitively — they're
    # a flat list.
    reference_repos: list[str] = field(default_factory=list)
    # #316: per-repo guidance for drafting new GitHub issues. Accepts either
    # an inline markdown string OR a file path relative to the repo root
    # (e.g. `docs/ISSUE_GUIDANCE.md`). See `resolve_new_issue_guidance`.
    new_issue_guidance: str | None = None
    # #305: glob patterns (relative to the worktree root) for build artifacts
    # to stash before the worktree is removed.  Matches are copied to
    # ~/.coord/artifacts/<repo>/<branch>/ on the agent with latest-wins
    # semantics per (repo, branch) pair.  Files under 100 bytes or ending
    # in `.d` are excluded (dependency files, not binaries).
    artifact_paths: list[str] = field(default_factory=list)
    # #323: optional provider override for workers dispatched to this repo.
    # When set, overrides providers.default from coordinator.yml.  The value
    # must match a key in providers.definitions (or be "claude" which is
    # always implicit).  None means "use the global default".
    provider: str | None = None
    # #2687/#2948: per-PR preview URL template for the pre-merge UAT gate —
    # e.g. "https://github.com/{repo}/pull/{pr_number}" (natal-chart's actual
    # interim config, #2948). `None` (the default) is the "no override
    # configured" state.
    #
    # #2948: this is now an OPTIONAL OVERRIDE, not the primary way a repo
    # opts into the gate. It exists only for a repo whose preview host has a
    # genuinely templatable URL. The primary path — `coord.merge_queue.
    # evaluate_uat_verdict` reading the real preview URL off the GitHub
    # Deployment the CI action already creates per PR — needs no template at
    # all; see `uat_live_preview` for that opt-in. Before #2948 this template
    # ALSO offered a `{pr_branch_slug}` substitution meant to reconstruct a
    # Cloudflare Pages branch-alias subdomain — removed entirely, because it
    # was confirmed live (2026-08-29, against natal-chart) to never resolve:
    # Cloudflare Pages publishes no branch aliases at all (`main` itself
    # 404s), and even the pages.dev subdomain isn't derivable from the
    # project name, so no algorithm operating on the branch name alone could
    # ever have produced a working URL. A working preview link there can only
    # come from the live GitHub Deployment lookup, not a template.
    #
    # `coord.merge_queue.requires_uat` treats `uat_preview` OR
    # `uat_live_preview` as "this repo has opted in", REGARDLESS of whether
    # "uat" appears in `pipeline.default_gates` — the deliberate per-repo
    # half of the two-part opt-in (the other half is adding "uat" to the gate
    # list itself, which is fleet-wide). Substitution variables available in
    # the template — see `resolve_uat_preview_url` — are `{branch}` (raw
    # branch name), `{issue_number}`, `{pr_number}`, and `{repo}`. An unknown
    # `{...}` placeholder in the template is left unrendered rather than
    # raising, so a typo shows up as a visibly broken URL instead of crashing
    # the gate.
    uat_preview: str | None = None
    # #2948: per-repo opt-in to the LIVE preview-URL lookup — the primary
    # resolution path, used when `uat_preview` is unset (an explicit
    # `uat_preview` override always wins when both are set). `False` (the
    # default) means "not opted in", matching `uat_preview is None`'s
    # existing meaning for `coord.merge_queue.requires_uat` — a repo that
    # sets neither this nor `uat_preview` never blocks on the UAT gate, no
    # matter what `pipeline.default_gates` says. Set this for a repo like
    # natal-chart, whose preview host (Cloudflare Pages) has no derivable
    # URL of any kind and must be read from the GitHub Deployment the CI
    # action creates per PR (`coord.github_ops.get_pr_deployment_url`).
    uat_live_preview: bool = False

    def resolve_uat_preview_url(
        self,
        *,
        branch: str | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
    ) -> str | None:
        """Render this repo's `uat_preview` OVERRIDE template for one PR.

        Returns ``None`` when `uat_preview` is unset — including when the
        repo instead relies on `uat_live_preview`'s live lookup, which this
        method knows nothing about (see `coord.merge_queue.
        evaluate_uat_verdict`, which tries this first and falls back to the
        live lookup). Never raises: an unresolvable `{placeholder}` in the
        template leaves it unrendered rather than raising ``KeyError`` — see
        the field docstring.
        """
        if not self.uat_preview:
            return None
        try:
            return self.uat_preview.format_map(
                _UatPreviewVars(
                    branch=branch or "",
                    issue_number=issue_number if issue_number is not None else "",
                    pr_number=pr_number if pr_number is not None else "",
                    repo=self.name,
                )
            )
        except (ValueError, IndexError):
            # `str.format_map` can still raise on malformed format specs
            # (e.g. a stray "{}" or "{0}") that `_UatPreviewVars.__missing__`
            # can't intercept — fall back to the raw template rather than
            # taking down the merge gate over a typo in coordinator.yml.
            return self.uat_preview

    def resolve_new_issue_guidance(self, repo_path: Path) -> str:
        """Return the new-issue guidance string for this repo.

        Resolution order:
        1. If ``new_issue_guidance`` is ``None`` (or empty), return a
           generic default describing the required issue sections.
        2. If the value matches ``[\\w/.-]+\\.(md|txt)$`` **and** the file
           exists at ``repo_path / value``, return the file contents.
        3. If the pattern matches but the file is missing, return the value
           verbatim as inline text (so a misconfigured path is still visible
           to the worker rather than silently replaced).
        4. Otherwise, return the value verbatim (it is inline markdown).
        """
        _DEFAULT = (
            "Required sections: "
            "Title (active voice, ≤80 chars), "
            "What (1-3 sentences), "
            "Acceptance (bulleted, observable), "
            "Out of scope"
        )
        if not self.new_issue_guidance or not self.new_issue_guidance.strip():
            return _DEFAULT
        value = self.new_issue_guidance.strip()
        if _GUIDANCE_PATH_RE.match(value):
            # Belt-and-braces against an escape from `repo_path`: resolve the
            # candidate and the base, then confirm the candidate stays under
            # the base.  This guards against any future regex regression as
            # well as edge cases like symlinks pointing outside the tree.
            try:
                base = repo_path.resolve()
                candidate = (repo_path / value).resolve()
            except (OSError, RuntimeError):
                # Resolution failure (e.g. permission denied, symlink loop) —
                # treat as inline so we never silently read a surprising file.
                return value
            try:
                candidate.relative_to(base)
            except ValueError:
                # Path escapes the repo root — treat as inline rather than
                # reading a file outside the trusted tree.
                return value
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except (OSError, FileNotFoundError):
                # File missing — fall back to inline so the value is at least
                # surfaced in the prompt rather than silently defaulting.
                return value
        return value


@dataclass
class QuietHours:
    """A machine's recurring daily no-new-dispatch window (#1862).

    ``start``/``end`` are wall-clock ``datetime.time`` values evaluated in
    ``tz`` — a REQUIRED IANA zone name (see ``coord.config._parse_machines``,
    which refuses to parse a ``quiet_hours`` block with a missing/invalid
    ``tz`` rather than silently defaulting). `coord serve` runs on UTC; a
    naive ``"23:00"`` compared against the daemon's own clock would fire at
    the wrong wall-clock hour for any non-UTC operator — exactly the bug
    this field exists to prevent.

    This reuses ``coord.machine_pause``'s existing routing-pause semantics:
    it governs the ROUTING decision for *new* dispatch only and never
    cancels an in-flight assignment — a task still running when the window
    opens finishes normally.

    ``start == end`` is rejected at config-parse time (ambiguous — "quiet
    all day" vs "never quiet" — rather than guessed; a machine that wants to
    be quiet all day should just be `coord pause`d).

    Known limitation (#1862 review, not blocking, no issue filed yet):
    ``covers()``/``window_end_instant()`` do plain wall-clock arithmetic via
    ``ZoneInfo`` with no special-casing for the two annual DST-transition
    days in ``tz``. A window boundary that falls in a nonexistent (spring-
    forward) or ambiguous (fall-back) local hour on those two days could be
    off by up to an hour. Not called out by #1862 and not a regression from
    anything that existed before it — a fix would need to decide which of
    the two ambiguous instants "wins" and is a candidate for its own
    follow-up rather than folding into this feature.
    """

    start: time
    end: time
    tz: str

    def covers(self, now: datetime | None = None) -> bool:
        """True when *now* falls inside this window, evaluated in ``tz``.

        Half-open interval ``[start, end)``: the instant ``start`` is
        covered, the instant ``end`` is not — the machine wakes up exactly
        at ``end``, not a minute after. ``start > end`` denotes a window
        that wraps midnight (e.g. ``23:00`` → ``08:00``).
        """
        moment = self._local_time(now)
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end

    def window_end_instant(self, now: datetime | None = None) -> datetime:
        """The absolute UTC instant the window containing *now* ends.

        Only meaningful when ``self.covers(now)`` is true — used by
        ``coord.machine_pause`` to size a ``coord unpause`` override so it
        expires exactly when the window would have anyway, rather than a
        fixed duration that could outlive or undershoot it.
        """
        now = self._aware(now)
        zone = ZoneInfo(self.tz)
        local_now = now.astimezone(zone)
        end_dt = local_now.replace(
            hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0,
        )
        if self.start > self.end and local_now.time() >= self.start:
            # Wrapping window, currently in the "evening" half — the window
            # ends tomorrow's clock time, not today's.
            end_dt = end_dt + timedelta(days=1)
        return end_dt.astimezone(timezone.utc)

    def _local_time(self, now: datetime | None) -> time:
        return self._aware(now).astimezone(ZoneInfo(self.tz)).time()

    @staticmethod
    def _aware(now: datetime | None) -> datetime:
        now = now if now is not None else datetime.now(timezone.utc)
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


@dataclass
class Machine:
    name: str
    host: str
    capabilities: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    repo_paths: dict[str, str] = field(default_factory=dict)
    # #1417: optional per-machine override of `concurrency.max_workers`.
    # `None` means "no override" — the machine's effective cap is the
    # fleet-wide default. Set this lower on hardware that can't keep up with
    # the fleet norm (e.g. a 4-core box among 20-core desktops) so automated
    # capacity checks (`coord retry`) don't pile concurrent workers onto it.
    max_workers: int | None = None
    # #1862: optional recurring daily no-new-dispatch window. `None` (unset,
    # the default) means "behaves exactly as before this feature" — see
    # `QuietHours` above. Routing consults this only through
    # `coord.machine_pause.paused_set()`, never directly.
    quiet_hours: QuietHours | None = None

    def can_work_on(self, repo_name: str) -> bool:
        return repo_name in self.repos

    def repo_path(self, repo_name: str) -> str | None:
        return self.repo_paths.get(repo_name)


# #930 review fix: assignment ``type`` values that should flow through the
# normal Work → Test → Review → Merge pipeline like any other "work" — i.e.
# their completion is eligible for `coord.review.dispatch_review` /
# `dispatch_pending_reviews` and for the merge-queue auto-enqueue
# (`coord.merge_queue.enqueue_approved_work`, `coord.commands.merge`'s
# auto-enqueue scan). "work" is the default worker type; "mock-author"
# (#930, Gate A) commits `tests/acceptance/ms-NN/contract.md` and its own
# docstring/system-prompt promise it dispatches through the *same* pipeline
# as any other branch — so every completion-side filter that only matched
# ``type == "work"`` must also match this set, or a mock-author branch can
# never actually reach a review or the merge queue. "test-author" (#931,
# #1141) is structurally identical — it commits only
# `tests/acceptance/ms-NN/**` and needs a `skipped` test verdict — but was
# never added when mock-author landed, so every per-issue JIT acceptance
# slice silently stalled before review/merge with no error (confirmed live,
# #1141). Keep this set — not a bare string check — as the single source of
# truth so a future work-like type only needs to be added here.
WORK_LIKE_TYPES: frozenset[str] = frozenset({"work", "mock-author", "test-author"})

# #1175: subset of WORK_LIKE_TYPES whose entire job is writing under a
# repo's sealed acceptance paths (docs/ORACLE_LOOP.md — today just
# `tests/acceptance/`). `coord.review`'s oracle-tamper rule normally treats
# ANY diff touching a sealed path as a mandatory `request-changes` — correct
# for a `type="work"` PR (a worker must never edit the suite it's graded
# against) but a guaranteed false positive for these types, since authoring
# `tests/acceptance/ms-NN/**` (contract.md + mocks for "mock-author", the
# acceptance slice itself for "test-author") *is* the assignment. For these
# types the rule inverts: the violation is touching anything OUTSIDE the
# sealed path, not touching it. Keep this as a set here (not a bare string
# check in coord/review.py) so a future sealed-path-authoring type only
# needs to be added in one place.
SEALED_PATH_AUTHOR_TYPES: frozenset[str] = frozenset({"test-author", "mock-author"})

# #2555: filename of the shared sealed-acceptance manifest a
# `type="conflict-fix"` dispatch is authorized to edit additively for a
# SEALED_PATH_AUTHOR_TYPES branch — every milestone's
# `tests/acceptance/ms-NN/manifest.yml`. `coord.conflict_fix`'s sealed-author
# dispatch branch and `coord.notify`'s stalled-pipeline confinement check
# both key off this exact name, kept here as a single shared constant so they
# can't drift.
#
# #2543: as of the per-issue manifest-fragment restructure, this file no
# longer carries the routine per-issue `issues:`/`expected_red:` traffic
# that used to make it a two-slice collision point — that now lives in
# `tests/acceptance/ms-NN/manifest.d/<issue>.(yml|json)`, one file per issue,
# which two different issues' slices can never textually conflict on (see
# `coord.acceptance.MANIFEST_FRAGMENTS_DIRNAME`). This file is left as a
# single shared manifest by choice, not oversight — it now holds only rare,
# milestone-level, usually-hand-edited blocks (`gate_a:`, `exempt:`).
# `coord.conflict_fix._is_sealed_manifest_path` recognizes fragment paths
# too, for the legacy shape and the same-issue-retry edge case.
SEALED_MANIFEST_FILENAME = "manifest.yml"

# #1077: subset of WORK_LIKE_TYPES whose ``issue_number`` is the issue the PR
# actually *resolves* — i.e. merging it should auto-close that issue. "work"
# qualifies. "mock-author" (Gate A) is WORK_LIKE (it flows through the same
# Work → Test → Review → Merge pipeline) but its ``issue_number`` is the
# *milestone's tracking/epic issue* (see ``coord/mock_author.py``), not
# something the contract PR resolves — merging it must NOT close that issue,
# or the epic reads as "done" while its real sub-issues are still open/
# untouched (claude-coordinator#1041 got closed this way). This is the single
# source of truth for both the PR-body "Closes #N" vs "Refs #N" keyword
# choice (``coord/review.py``, ``coord/merge_queue.py``'s ``_briefing_body``)
# and the deterministic post-merge ``close_issue`` call
# (``coord/merge_queue.py``'s ``process``). "test-author" (#1141) deliberately
# stays OUT of this set too — its ``issue_number`` is likewise always the
# milestone's tracking issue (see ``for_issue_number`` below for the actual
# per-slice issue), never something the contract/fixture PR resolves.
# Verified against confirmed-live behaviour (PR #1139 correctly used
# "Refs #1117"), not merely assumed.
CLOSES_ISSUE_TYPES: frozenset[str] = frozenset({"work"})

# #1142: the assignment `type` `coord pr` gives its PR-opening helper session
# when the *original* assignment it's opening a PR for is NOT itself in
# ``CLOSES_ISSUE_TYPES`` (e.g. "test-author"/"mock-author", whose
# ``issue_number`` is the milestone's tracking issue, not something this PR
# resolves — see ``CLOSES_ISSUE_TYPES`` above). Before #1142, `coord pr`
# unconditionally dispatched its helper with the default `type="work"`,
# which made ``coord.stage_projection.merge_stage_status_for``'s #775
# fallback (and ``issue_has_any_approved_review``) mistake a merged
# PR-helper for the *tracking issue's own* merged work, showing an open epic
# as "Done". Giving the helper this distinct type keeps it out of both
# ``WORK_LIKE_TYPES`` and ``CLOSES_ISSUE_TYPES`` (and therefore out of every
# heuristic keyed on either) while still being dispatched exactly like a
# normal "work" session otherwise — see ``coord.agent.WRITE_CAPABLE_SPEC_TYPES``,
# which must also list it since it mutates GitHub via `gh pr create`.
PR_HELPER_TYPE = "pr-helper"

# #2966: "only the coordinator writes docs" (this repo's CLAUDE.md says so
# for every worker) was text-only enforcement — `Repo.coordinator_only_files`
# is the config key that was clearly designed to seed a work dispatch's
# `files_forbidden` with the repo's shared docs (see `coord.dispatch.dispatch`
# lines around #587), but it was set by ZERO of the fleet's 10 repos, so
# `files_forbidden` started empty for every work dispatch and only ever
# gained sealed acceptance paths (#944), never a doc. Two workers in the same
# repo consequently rewrote the same CLAUDE.md section in the same week,
# producing a semantic (prose) merge conflict that stalled a five-issue
# cross-repo chain for ~98 ticks until an operator intervened.
#
# Mirror the #944 sealed-paths fix for the same class of problem: don't
# depend on every operator remembering to configure this per repo. A repo's
# own rulebook is auto-forbidden unconditionally — `coordinator_owned_docs()`
# below UNIONS this default in regardless of what `coordinator_only_files`
# says, the same way sealed acceptance paths are auto-added regardless of
# `coordinator_only_files` (`coord.dispatch.dispatch`'s #944 comment).
COORDINATOR_OWNED_DOC_DEFAULTS: tuple[str, ...] = ("CLAUDE.md",)


def coordinator_owned_docs(repo: "Repo | None") -> list[str]:
    """Coordinator-only doc paths for *repo* (#2966).

    Returns :data:`COORDINATOR_OWNED_DOC_DEFAULTS` UNIONED with *repo*'s own
    ``coordinator_only_files`` (if any), deduped and order-preserving
    (defaults first). Callers use this instead of reading
    ``repo.coordinator_only_files`` directly so the fleet-wide default keeps
    applying even for the common case — today, every repo — where
    ``coordinator_only_files`` is unset.

    #1388-style fail-open: some ``Repo``-shaped stand-ins (a
    wire-reconstructed object, or a stale install predating this field)
    don't carry ``coordinator_only_files`` at all — ``getattr`` with a
    default treats that the same as "unset" rather than raising, matching
    how ``dispatch_review`` already tolerates a stand-in missing
    ``develop_branch``.
    """
    result: list[str] = list(COORDINATOR_OWNED_DOC_DEFAULTS)
    if repo is not None:
        for f in getattr(repo, "coordinator_only_files", None) or []:
            if f not in result:
                result.append(f)
    return result


def trust_issue_closed_for(assignment_type: str | None) -> bool:
    """Whether :func:`coord.github_ops.work_is_terminal` may trust
    ``issue_is_closed`` for a row of *assignment_type* (#2639).

    ``False`` only for :data:`SEALED_PATH_AUTHOR_TYPES` (``test-author``/
    ``mock-author``), whose ``issue_number`` is always the milestone's
    *tracking* issue — never this row's own deliverable (the per-slice issue
    lives in ``for_issue_number``) — so a tracking epic that's closed for
    most of a milestone's life must not read as "this row is done".

    ``True`` for everything else, including :data:`CLOSES_ISSUE_TYPES`
    (``work``, whose ``issue_number`` genuinely is its own deliverable — the
    #522 flood guard requires this) and interactive ``--merge-of`` sessions
    (``type="conflict-fix"`` with no sealed-path semantics). Within
    :data:`WORK_LIKE_TYPES` ∪ interactive-merge-session, ``WORK_LIKE_TYPES -
    SEALED_PATH_AUTHOR_TYPES == CLOSES_ISSUE_TYPES``, so this reduces to a
    single "trust it unless sealed-path-authoring" rule — kept as one shared
    helper (rather than re-deriving ``type not in SEALED_PATH_AUTHOR_TYPES``
    at each ``work_is_terminal`` call site) so the two families can't drift.

    Every call site that can process a :data:`WORK_LIKE_TYPES` row (or an
    interactive merge session) should pass
    ``trust_issue_closed=trust_issue_closed_for(row.type)`` to
    :func:`coord.github_ops.work_is_terminal` rather than relying on that
    kwarg's ``True`` default.
    """
    return assignment_type not in SEALED_PATH_AUTHOR_TYPES

# #685: the per-issue Test-stage POLICY labels, and the one pure function that
# reads them. ``test-mode:auto`` → the headless Test stage auto-dispatches
# (`coord.smoke.dispatch_pending_smoke`); ``test-mode:smoke`` → it deliberately
# does NOT, because the Test stage for that issue is human-attended (the TUI
# offers an interactive smoke agent instead); no label → back-compat, respect
# ``smoke_tests.auto_queue``.
#
# #2024: this used to be an inline pair of `in labels` checks inside
# ``coord.state._get_issue_test_mode_local`` and nowhere else, because only the
# dispatcher ever asked. The DRIVER has to ask too — a `test-mode:smoke` issue
# is precisely the shape where a completed fix round's Test stage will never be
# dispatched by anything automatic (``coord.dead_end`` shape 3), and a driver
# that can't see the policy counts "no state change" against a transition that
# is never coming (vimcode#635: 25 min, then 160 min, on one issue). Hoisted
# here — import-light, no DB — so the dispatcher's reading and the driver's
# reading are the same three lines and cannot drift.
TEST_MODE_AUTO_LABEL = "test-mode:auto"
TEST_MODE_SMOKE_LABEL = "test-mode:smoke"


def test_mode_from_labels(labels) -> str | None:
    """``"auto"`` / ``"smoke"`` / ``None`` for an issue's GitHub *labels*.

    ``auto`` wins when both are present (an explicit opt-in to the headless
    path beats the human-attended default), matching the original
    ``_get_issue_test_mode_local`` ordering exactly. Tolerates ``None`` and
    any non-list input by returning ``None`` — every caller here fails OPEN
    onto "no policy set", never onto a policy nobody asked for.
    """
    if not labels:
        return None
    try:
        names = list(labels)
    except TypeError:
        return None
    if TEST_MODE_AUTO_LABEL in names:
        return "auto"
    if TEST_MODE_SMOKE_LABEL in names:
        return "smoke"
    return None


# #2188: the per-issue DELIVERABLE policy label — mirrors `TEST_MODE_*_LABEL`'s
# shape (cheapest fit, no body parsing). An issue whose deliverable is a
# written artifact (a diagnosis, an audit, a spike/investigation writeup)
# rather than a diff cannot succeed in the default pipeline: a worker that
# does exactly what was asked ends with 0 commits by design, which the reap
# otherwise reads as the #448 "worker did nothing" ADVISORY and `coord drive`
# gives up on — discarding the whole deliverable (#2188's own worked example,
# #2132, lost $1.65 of correct, on-topic analysis this way). Labelling the
# issue `deliverable:analysis` inverts that reading: a clean exit with 0
# commits is `coord.agent.AgentServer._reap`'s SUCCESS condition for this
# issue, not its anomaly — see `_ZERO_COMMIT_TYPES`/`AgentAssignment.
# analysis_deliverable` in coord/agent.py and the `decide()` short-circuit in
# coord/drive.py that skips Test/Review/Merge for it.
DELIVERABLE_ANALYSIS_LABEL = "deliverable:analysis"


# #2234: the machine-readable tag `coord.drive.decide` embeds in its
# `_die()` message when a work row reaps as `coord.agent.
# REFUSED_POLICY` — a worker that exited cleanly (exit_code==0), pushed 0
# commits, and whose OWN final message cited a standing repo-rule
# prohibition as the reason it stopped (`coord.agent._looks_like_policy_
# refusal`; the #2195 shape: "only the coordinator writes docs"). That is a
# PERMANENT property of the issue, not a property of the attempt — nothing
# about waiting and relaunching changes a standing rule — so it must never
# be treated as a transient death.
#
# Text-marker matching (not a dedicated exit code) mirrors `coord.gate_a.
# park_marker`/`is_gate_a_refusal_reason` (#2063): `coord/drive_queue.py`'s
# `_reconcile_running` recognises this straight out of the drive's own
# `drive_exited` audit summary (`own_reason`), independent of the run's exit
# code, and routes it to `STATE_PARKED` rather than burning one of the
# queue's two launch attempts rediscovering a rule that cannot change
# on retry (#1844's same principle, applied post-dispatch) — and rather than
# `STATE_BLOCKED`, which nothing re-evaluates and reads, at a glance, exactly
# like a genuine crash (the whole complaint #2234 exists to fix). Hoisted
# here — import-light, no DB — so `coord/drive.py` (which writes the marker)
# and `coord/drive_queue.py` (which reads it back, on every subsequent tick
# too — see its parked-entry pre-pass) share one literal string.
POLICY_REFUSAL_MARKER = "[refused-by-policy #2234]"


def is_policy_refusal_reason(text: str | None) -> bool:
    """Whether *text* is a #2234 policy-refusal park reason.

    Unlike a Gate-A park (#2063), a policy refusal has no external verdict
    that can arrive and clear it — the rule it names is standing, not
    pending — so nothing auto-resumes an entry parked for this reason (see
    the pre-pass in `coord.drive_queue.plan_tick`, which checks this
    predicate and deliberately leaves the entry alone rather than falling
    through to the CI-park "resume" default). An operator clears it the same
    way they clear a `blocked` entry: do the coordinator-side work (or
    re-scope the issue), then `coord drive-queue remove`.
    """
    return bool(text) and POLICY_REFUSAL_MARKER in text


# #2850: the machine-readable tag `coord.drive.decide` embeds in its
# `_succeed()` message when the "terminal: merged" branch's own LIVE check
# (`MergeVerifier.verify_merged`) confirms the issue has genuinely landed —
# not merely that the board's cached `merge_status` claims so.
#
# Text-marker matching (not a dedicated exit code) mirrors
# `POLICY_REFUSAL_MARKER` immediately above: `EXIT_OK` is shared by every
# clean drive exit — this one, a `deliverable:analysis` 0-commit stop, and a
# `--no-merge` "review approved, stopping here" — so the exit code alone
# cannot distinguish "confirmed merged" from "stopped short of merging on
# purpose". `coord/drive_queue.py`'s `_reconcile_running` recognises this
# straight out of the drive's own `drive_exited` audit summary (`own_reason`)
# and marks the queue entry `done` instead of requeuing a launch that has
# nothing left to do — closing the #2850 failure mode: a drive that exits 0
# having MERGED got requeued and relaunched into dead air, with the bogus
# `running` row shadowing #2602's own live re-check for every dependent
# chained `--after` it.
MERGE_LANDED_MARKER = "[drive-merged #2850]"


def is_merge_landed_reason(text: str | None) -> bool:
    """Whether *text* is a #2850 confirmed-merge drive-exit reason."""
    return bool(text) and MERGE_LANDED_MARKER in text


@dataclass
class Assignment:
    machine_name: str
    repo_name: str
    issue_number: int
    issue_title: str
    files_allowed: list[str] = field(default_factory=list)
    files_forbidden: list[str] = field(default_factory=list)
    briefing: str = ""
    assignment_id: str | None = None
    status: str = "pending"  # pending | running | done | failed | advisory | refused_policy
    branch: str | None = None
    pr_url: str | None = None
    dispatched_at: float | None = None
    finished_at: float | None = None
    smoke_test: str | None = None  # None | pass | fail
    smoke_test_reason: str | None = None
    # "work" (default), "review", "plan", "smoke", "conflict-fix", "audit",
    # or a handful of other human-attended flavours (troubleshoot, chat,
    # merge). Review assignments target an existing PR rather than
    # implementing a fresh issue. Plan assignments are read-only: the worker
    # analyses the codebase and outputs a structured plan without writing any
    # code. conflict-fix is dispatched when a merge fails with a mechanical
    # (non-semantic) conflict — the worker rebases, resolves obvious
    # additive merges, and force-pushes; the coordinator owns the retry.
    # audit (#885) is a read-only, human-attended milestone-outcome analyst —
    # dispatched via `--audit-of <epic_issue>`, never through the headless
    # Work → Test → Review → Merge pipeline.
    type: str = "work"
    review_target: str | None = None
    review_of_assignment_id: str | None = None
    unreachable_count: int = 0
    # Model tier the worker was dispatched with (e.g. "haiku", "sonnet",
    # "opus"). None means the worker used claude's default. Tracked on the
    # board so escalation in `coord fix` / `coord retry` / `coord resume-stuck`
    # can step up the ladder.
    model: str | None = None
    # Parsed structured plan from a plan-only worker (type="plan"). Stored as
    # a plain dict (the serialised form of WorkerPlan.to_dict()) so it round-
    # trips cleanly through JSON without a custom encoder.
    plan: dict | None = None
    # Review lifecycle state for type="work" assignments.
    # None  — not applicable (review/smoke/plan assignments, or pre-feature boards)
    # "pending"    — work done, review not yet dispatched
    # "dispatched" — review assignment is in flight
    # "done"       — review assignment completed
    review_state: str | None = None
    # #1627: human-readable reason `dispatch_review()` set on THIS assignment
    # the last time it returned ``None`` without dispatching a review — e.g.
    # "assignment is type 'smoke', not reviewable work" or "a work/fix
    # assignment is actively rewriting the branch for this issue". Transient:
    # set in-memory on every early-return guard so a caller in the same
    # process (the `coord review` CLI, chiefly) can report *why* nothing was
    # dispatched instead of guessing. Not read back out of storage by
    # `row_to_assignment` — it isn't meant to survive a reload, only the one
    # dispatch_review() call that produced it.
    review_dispatch_reason: str | None = None
    # Pipeline gate requirements — controls which approval steps are enforced.
    # Empty list means "use config.pipeline.default_gates".
    # Examples: ["review", "merge"], ["merge"], ["review", "smoke", "merge"]
    required_gates: list[str] = field(default_factory=list)
    # Auto-loop iteration counter. For the original work assignment this is 0.
    # Each fix worker dispatched by auto_loop increments this by 1. Used to
    # enforce pipeline.max_review_iterations and stop runaway loops.
    review_iteration: int = 0
    # Timestamp when review findings were successfully posted to GitHub (as a
    # PR review or issue comment).  None means findings have not been posted
    # yet — either the review is still running, the worker produced no
    # structured output, or notify never saw the completion event.
    review_posted_at: float | None = None
    # #200: human-driven Test gate verdict for type="work" assignments.
    # None | "passed" | "failed" | "skipped". Review auto-dispatch is gated on
    # this being passed/skipped (or no Test stage configured).
    # #1395: also "running" — a transient, non-verdict marker an unattended
    # driver (scripts/drive-issue.sh) sets while it runs the suite locally
    # (bypassing dispatch_smoke), so coord.stage_projection.test_stage_status_for
    # can show the Test box Active instead of indistinguishable-from-idle
    # Pending. Every gate that reads this field keys off the terminal
    # passed/skipped/failed values explicitly, so "running" fails closed
    # everywhere by construction — never add a bare `is not None` check here.
    test_state: str | None = None
    test_reason: str | None = None
    # #2687: human UAT (User Acceptance Test) verdict for type="work"
    # assignments on a repo with `Repo.uat_preview` configured — a deliberate
    # per-repo opt-in, unlike the Test gate above which every repo shares.
    # None | "passed" | "failed". Recorded via `coord uat <id> --passed|
    # --failed [--note TEXT]`, mirroring the Test gate's verdict shape (see
    # `coord.state.record_uat_verdict`). Gated by `coord.merge_queue.
    # requires_uat`/`evaluate_uat_verdict` — unlike the Test/Review gates,
    # this one carries no SHA/patch-id staleness tracking: it's a human's
    # judgment on the rendered preview, not a re-runnable measurement, so
    # there's nothing to mechanically re-verify against a moved SHA.
    uat_state: str | None = None
    uat_reason: str | None = None
    # #1479: staleness anchor for a terminal (passed/skipped) Test-gate
    # verdict — captured once, best-effort, when the verdict is recorded
    # (``coord.state._record_test_verdict_local``). Mirrors the review gate's
    # ``review_head_sha``/``review_patch_id`` (#821/#1475) but adds a THIRD
    # value the review gate deliberately doesn't need: the merge base's own
    # HEAD SHA at test time. Review staleness is about what changed in the
    # branch; test staleness is also about what the branch was combined
    # with — a rebase onto a moved base can break tests without changing the
    # branch's own diff, so the merge gate (``coord.merge_queue.
    # has_smoke_verdict``) must re-verify even when the content is byte-
    # identical. All three are None for rows predating this feature or where
    # the anchor could not be captured (fails open — the staleness check is
    # skipped, matching #821/#1475's convention).
    test_head_sha: str | None = None
    test_patch_id: str | None = None
    test_base_sha: str | None = None
    # #1629 (H-2): the toolchain that produced this verdict — e.g.
    # "rustc 1.95.0" or "python 3.12.4, node 20.11.0" — captured (best-effort,
    # via coord.health.checks.toolchain) alongside a terminal test_state
    # write. Annotation only: no gate reads this field to block anything (see
    # coord.health.checks.toolchain.probe_toolchain_skew, the fleet-scope
    # check that judges skew — it is advisory, same as every other fleet
    # check). None for every row predating this feature or where the
    # producing toolchain could not be resolved — renders as "unknown", not
    # as a mismatch.
    test_toolchain: str | None = None
    # #253: parsed adversarial-review verdict for type="review" assignments.
    # None | "approve" | "request-changes". Set when notify or auto_loop
    # extracts the structured REVIEW_VERDICT from the reviewer's log; consumed
    # by the merge-queue gate (`has_approved_review`) to refuse merging work
    # whose review has not approved.
    review_verdict: str | None = None
    # #1456: audit trail for a coordinator-side verdict override.  When the
    # #476 approve-with-nits gate downgrades a reviewer's "request-changes" to
    # "approve", the reviewer's OWN verdict is preserved here and the evidence
    # that justified the override (the parsed finding counts) in
    # `review_verdict_override_reason`.  Both are None for the overwhelming
    # majority of reviews — a non-None `review_verdict_original` is the signal
    # that `review_verdict` is the coordinator's opinion, not the reviewer's.
    # A verdict that changes must be auditable, never overwritten in place: the
    # #1445 incident (a well-formed request-changes silently rewritten to
    # approve) was invisible precisely because only the final value was stored.
    review_verdict_original: str | None = None
    review_verdict_override_reason: str | None = None
    # #1956: provenance for `review_verdict` — WHO decided it and HOW, not
    # just what was decided. A relayed verdict (`coord report-result
    # --verdict` posted by an operator, not parsed from the reviewer's own
    # log) is otherwise indistinguishable from one the reviewer agent
    # produced itself — every downstream reader (merge gate, `coord gates`,
    # the TUI) sees a plain "approve" either way. Three values:
    #   "agent"      — parsed from the reviewer's own transcript (the
    #                  overwhelming common case; also the default when this
    #                  column is NULL, for every row predating this feature).
    #   "recovered"  — the reviewer reached a verdict and said so in prose,
    #                  but never emitted the machine-readable REVIEW_VERDICT
    #                  header; an operator (or the #1956 transcript-floor
    #                  fallback) rescued it from the transcript. Asserts
    #                  "the reviewer decided this, we merely restored it."
    #   "overridden" — a human (or the #476 approve-with-nits gate) recorded
    #                  a DIFFERENT verdict than the reviewer's own. Asserts
    #                  "the reviewer decided otherwise and this overrides
    #                  it." Always paired with `review_verdict_original`
    #                  when the override happened automatically (#476); a
    #                  manual override may have no prior agent verdict at
    #                  all (e.g. an interactive review that never finished).
    # `verdict_source_reason` is a required, human-readable justification for
    # anything that isn't "agent" — see issue_store._validate_result.
    verdict_source: str | None = None
    verdict_source_reason: str | None = None
    # #821: SHA of the branch HEAD captured at the time the review assignment
    # ran.  When set, `has_approved_review` compares this against the merge
    # queue entry's `branch_head_sha` to reject stale approvals — if the
    # branch gained commits after the review ran, the approval no longer
    # covers the current HEAD and the entry is re-blocked until re-reviewed.
    # None for review assignments predating this field or where SHA tracking
    # is not available.
    review_head_sha: str | None = None
    # #1475: content-addressed fingerprint (`git patch-id --stable`) of the
    # diff the review covered, captured alongside `review_head_sha`. When a
    # later commit-bound staleness check finds the SHAs differ (e.g. a
    # conflict-fix rebase moved the head), `has_approved_review` falls back
    # to comparing this against the branch's *current* patch-id — identical
    # ⇒ the rebase changed no content and the approval still covers it;
    # different (or either side unavailable) ⇒ stale, same as before this
    # field existed. Stored alongside `review_head_sha`, never replacing it
    # — the SHA remains the audit/#821 trail. None for rows predating #1475
    # or where the diff/patch-id could not be computed.
    review_patch_id: str | None = None
    # #1476: True when this ``type="review"`` assignment is a SCOPED
    # re-review — dispatched because a conflict-fix rebase changed content
    # under an already-`approve`d review (`review_patch_id` mismatch), with
    # no other intervening work/fix commit. The reviewer was handed the
    # prior approved diff as established context plus only the resolution
    # delta, not the full PR — so a `False`/default here means "read the
    # whole diff" for every audit consumer. `review_scope_base_sha` records
    # the prior review's `review_head_sha`, i.e. which commit the delta was
    # computed FROM, so the audit trail can reconstruct exactly what was
    # (and wasn't) re-read. Both None/False for every row predating this
    # feature and for ordinary full reviews.
    review_scoped: bool = False
    review_scope_base_sha: str | None = None
    # #208: parsed worker cost from the final stream-json `result` event.
    # None means "not yet captured" (older rows, in-flight workers, or
    # workers whose log lacked usage data).  Set on completion by
    # notify.py / reconcile via coord.usage.parse_usage_from_log.
    cost_usd: float | None = None
    # #252: worker-emitted smoke-test list parsed from the SMOKE_TESTS
    # block.  None = no block emitted (graceful TUI placeholder); [] =
    # explicit "(none — change is internal)"; non-empty list = bullets.
    smoke_tests: list[str] | None = None
    # #324: resolved provider name recorded at dispatch time so the TUI
    # can surface it in the assignment detail panel (#327).  None means
    # "dispatched before #324 landed or via a path that doesn't set this
    # field" — the TUI should show the implicit default ("claude") in that
    # case.  Always the *resolved* name (after the spec > repo > default
    # precedence chain), not just the raw proposal.provider field.
    provider_name: str | None = None
    # #546: token counts for automated (claude -p) assignments.  Parsed from
    # the final stream-json result event at the same time as cost_usd.  All
    # default to 0; interactive (Max/OAuth) sessions stay at 0 and the TUI
    # labels them "Max (subscription)" rather than projecting a dollar figure.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # #618: short one-liner written immediately when an interactive session
    # fails to launch (e.g. "branch already checked out at <path>").  Lets
    # the TUI explain the red box without any log file being present.
    # None for assignments that launched successfully.
    failure_reason: str | None = None
    # #944: Acceptance-gate verdict (oracle loop, docs/ORACLE_LOOP.md) for
    # type="work" assignments. None | "passed" | "failed" — set by `coord
    # acceptance record --issue N --sha <sha>`, the coordinator's external
    # re-run of the sealed suite against the pushed SHA (the trust gate a
    # headless worker's in-session "green" claim can't fake).
    acceptance_state: str | None = None
    acceptance_reason: str | None = None
    # SHA the last `acceptance record` verdict was recorded against — lets a
    # future gate detect staleness (new commits since the last record) the
    # same way review_head_sha detects a stale review approval.
    acceptance_sha: str | None = None
    # #932: per-test counts from the same verdict, so the Acceptance box can
    # read as partial progress ("3/7 acceptance green") rather than a bare
    # pass/fail — a growing suite is expected to be sub-100% until the
    # feature completes (docs/ORACLE_LOOP.md). None for rows predating #932.
    acceptance_total: int | None = None
    acceptance_passed: int | None = None
    # #874: prose summary extracted from the worker's "### Summary" block
    # and persisted at completion time.  NULL when the worker emitted no
    # summary.  A durable, board-sourced complement to the ephemeral GitHub
    # comment (which already contained the same text).
    completion_summary: str | None = None
    # #886 Phase 2: Milestone Outcome Audit structured verdict, set only on
    # type="audit" assignments (see #885's --audit-of).  audit_goals_json is
    # a raw JSON string — a list of {goal, metric_before, metric_after,
    # verdict, evidence} — deliberately NOT decoded here (mirrors
    # review_findings: the coord-tui client consumes it as Option<String>).
    # audit_run_number increments once per `--audit-of <epic>` run against
    # the same (repo_name, issue_number) so later runs can diff against
    # earlier ones. All None for rows predating this feature.
    audit_goals_json: str | None = None
    audit_bottom_line: str | None = None
    audit_run_number: int | None = None
    # #1084: for type="test-author" assignments, the specific work-order
    # member issue this JIT dispatch is extending the acceptance suite for
    # (`coord.test_author.dispatch_test_author`'s `issue_number` argument).
    # NOT the same as `issue_number` above, which test-author always sets to
    # the milestone's *tracking* issue (every JIT dispatch for a milestone
    # shares one branch/PR, so `issue_number` alone can't distinguish "this
    # is issue #1039's slice" from "issue #1042's slice" — see #1084's
    # friction log). None for milestone-mode (Gate A) authoring and for
    # every other assignment type.
    #
    # #1553: this is now the *attribution* field for the whole oracle-loop
    # slice chain, not just the originating test-author dispatch. Every
    # follow-up derived from a slice (its review, its `[fix-N]` bounces, its
    # smoke, a retry) inherits the same value, either explicitly at the
    # dispatch site or via the parent lookup in
    # `coord.state._record_dispatched_assignment_local`. Read it through
    # :func:`effective_issue_number` rather than by hand so "which issue is
    # this work actually for" has one answer everywhere.
    for_issue_number: int | None = None
    # #1499: durable provenance — set when this assignment was dispatched by
    # `coord drive` (never by a hand `coord assign`). Carries
    # `f"drive:{repo_name}#{issue_number}"` (the DRIVEN issue, which for most
    # assignment types is the same repo/issue this row is already keyed on,
    # but is spelled out explicitly so the value is self-describing on its
    # own in the audit log / board without a join). `None` means "dispatched
    # by hand" (or predates this column) — the whole point is that a drive's
    # own dispatches are distinguishable from a human's after the driver
    # process has exited and left nothing else behind. Threaded through
    # `coord assign --driven-by` (`coord.commands.dispatch.assign`), which
    # `coord/drive.py`'s work-stage `Action` sets on every `coord assign` it
    # shells out to.
    driven_by: str | None = None
    # #2316: the worker's own terminal `stop_reason` (e.g. `"end_turn"`,
    # opencode's `"length"`, claude's `"max_tokens"`) — persisted for EVERY
    # terminal work-like assignment, not just failed ones, so `coord gates`/
    # `coord status`/the dashboard can show it and a future gate can act on
    # it. Captured from the agent's own `/status` `completed` entry (already
    # sent by every agent build — `coord.agent.AgentServer.list_assignments`
    # parses the worker's log and includes it there) by
    # `coord.reconcile._capture_stop_reason_best_effort`. `None` for rows
    # predating this column and for a non-stream-json / PTY worker whose log
    # carries no such field.
    #
    # This is the raw diagnostic value, distinct from `failure_reason`: a
    # truncated (`stop_reason` in `coord.agent._TRUNCATION_STOP_REASONS`),
    # 0-commit run is separately classified FAILED by `AgentServer._reap`
    # with a human-readable `failure_reason` naming the truncation — see
    # `coord.agent.AgentAssignment.truncation_reason`. Before #2316 that same
    # shape (space-invaders#1: the model spent its whole output budget on one
    # reasoning block and hit opencode's 32k-token ceiling) landed on
    # `advisory` — the same bucket as a worker that looked and correctly
    # found nothing to do — so nobody re-drove it.
    stop_reason: str | None = None
    # #2417: the CALLING assignment's id when this row was dispatched by a
    # `coord` subcommand run from INSIDE another worker's own turn (e.g. a
    # `type="work"` session shelling out to `coord acceptance author repo ms
    # --issue N` or `coord fix <other-id>`), as opposed to a human typing the
    # same command in their own shell. Populated centrally in
    # `coord.state.record_dispatched`/`record_dispatched_assignment` from the
    # `COORD_ASSIGNMENT_ID` env var (see `coord.agent._build_worker_env`'s
    # #2217 note — every headless worker's subprocess environment carries its
    # own assignment id, so a `coord` CLI the worker shells out to inherits
    # it automatically; a human's own shell never has it set). `None` for a
    # hand dispatch, a coordinator/brain-proposed dispatch, and rows
    # predating this column.
    #
    # This is the missing link #2417 reported: before this field, the only
    # way to discover that a work row had dispatched an independent sibling
    # assignment (and whether that sibling then succeeded) was grepping the
    # worker's raw `claude -p` transcript for the printed "Dispatched ... to
    # ..." line and manually cross-referencing a second `coord log` by hand.
    # With this field, the ORIGIN row can be found by any consumer (the
    # board, `coord audit`, the TUI) via a reverse lookup: "which assignment
    # has `dispatched_by_assignment_id == this row's id`?" — see
    # `coord.state.find_dispatched_children`.
    dispatched_by_assignment_id: str | None = None


def effective_issue_number(assignment: "Assignment | dict") -> int:
    """The issue this assignment's work is *attributed* to (#1553).

    For ordinary work this is simply ``issue_number``. For oracle-loop
    acceptance-slice work (``coord acceptance author <repo> <tracking>
    --issue N`` and everything derived from it — its review, its
    ``[fix-N]`` bounces, its smoke, a retry) ``issue_number`` is the
    milestone's **tracking/epic** issue, because the whole milestone's JIT
    slices share one branch and one PR. The child issue the work is really
    *for* lives in ``for_issue_number``; this helper prefers it.

    Both halves are deliberately kept on the row:

    * ``issue_number`` stays the tracking issue, so the epic keeps its
      parent link, the shared branch/PR bookkeeping keeps working, and
      ``coord.stage_projection``'s merge attribution keeps deliberately
      skipping non-``CLOSES_ISSUE_TYPES`` rows (re-attributing *that* was
      tried in #1203 and reverted in #1652 — do not repeat it).
    * ``for_issue_number`` is the effective/attributed issue, which is what
      "is this child being worked on right now?" and "what did this child
      cost?" must key on.

    Accepts either an :class:`Assignment` or a wire/DB ``dict`` row so the
    board-JSON consumers (``coord.usage_rollup``) share one definition.
    Returns ``0`` for a row carrying neither field rather than raising —
    every caller here is a display/aggregation path.

    Readers that deliberately still key on the RAW ``issue_number``, so a
    future change doesn't "fix" them by accident:

    * ``coord.stage_projection.compute_board_stage_projection`` — the
      per-issue stage grouping and its merge-queue attribution. #1203 tried
      re-attributing that half by ``for_issue_number`` and #1652 reverted it
      (it moved a false "merged/Done" green from the epic onto the child).
    * ``coord.notify`` — ``_pipeline_heads``/``_has_live_session_for`` and
      every GitHub-comment target. Stall notices are posted to the issue
      whose branch/PR the work lives on, which for a slice IS the tracking
      issue; re-keying only the heads would desync the two.
    * ``coord.pipeline.PipelineView.issue_number`` — the CLI's per-assignment
      pipeline view, which reports the row as dispatched.

    ``coord.claim.has_active_work_followup`` itself keys its scan on the
    effective issue (it calls this helper internally), so BOTH of its
    callers — ``coord.review.dispatch_review`` and
    ``coord.review.dispatch_pending_reviews`` — must pass
    ``effective_issue_number(...)`` too, not the raw ``.issue_number``.
    Passing the raw value there silently reopens the #459 stale-review
    guard for exactly the oracle-loop slices this function exists for: an
    in-flight ``type="work"`` retry for child A (carrying
    ``for_issue_number``) would no longer be detected as "actively
    rewriting the branch" when checking a completed round for that same
    child, because the two sides would be comparing effective-vs-raw
    instead of effective-vs-effective.
    """
    if isinstance(assignment, dict):
        raw_for = assignment.get("for_issue_number")
        raw_own = assignment.get("issue_number")
    else:
        raw_for = getattr(assignment, "for_issue_number", None)
        raw_own = getattr(assignment, "issue_number", None)
    for raw in (raw_for, raw_own):
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


@dataclass
class Proposal:
    id: int
    machine_name: str
    repo_name: str
    issue_number: int
    issue_title: str
    rationale: str
    files_likely: list[str] = field(default_factory=list)
    briefing: str = ""
    # Optional model override. When None, the dispatcher falls back to
    # config.models.default.
    model: str | None = None
    # "work" (default) or "plan". Plan proposals dispatch read-only planning
    # workers that analyse the codebase and produce a structured plan without
    # writing any code.
    type: str = "work"
    # Pipeline gate requirements — mirrors Assignment.required_gates.
    # Set by the coordinator before dispatch so the ledger records intent.
    required_gates: list[str] = field(default_factory=list)
    # Optional explicit branch the agent must check out, bypassing the
    # slugified-title-derived branch name.  Used by follow-up dispatches
    # (pr, fix-up, continuation) so prefixed issue titles like
    # `[fix-1] …` or `[conflict-fix] …` don't push to a new orphan
    # branch — the worker must land commits on the parent assignment's
    # branch instead.
    target_branch: str | None = None
    # #315: when set, the dispatch payload includes `--resume <session_id>`
    # so the worker loads the prior claude conversation and continues it.
    # Only set by `coord chat-continue`; regular dispatches leave this None.
    resume_session_id: str | None = None
    # #324: optional provider override for this proposal's worker.  Mirrors
    # ``Repo.provider`` and ``AssignmentSpec.provider`` — uses the same
    # precedence chain: spec > repo > providers.default.  When None the
    # coordinator and agent both fall back to the global default.  Set by
    # ``coord assign --provider`` (#1707, the human escape hatch — validated
    # against ``providers.definitions`` at the CLI before it ever reaches
    # here) for a hand dispatch; brain-side automatic selection is not yet
    # implemented (deliberately out of scope for #1707).
    provider: str | None = None
    # #934: the GitHub Milestone number the target issue belongs to, when
    # known. Set by callers that already fetched the issue (e.g.
    # ``coord.milestone_dispatch.dispatch_entry``) so ``coord.dispatch.
    # dispatch()`` can resolve the worker's base branch via
    # ``coord.branch_model.resolve_base_branch`` — `feature/ms-NN` for a repo
    # that opted into the #934 git model, otherwise the existing
    # `default_branch` behavior. `None` (the default) preserves today's
    # behavior exactly: brain-proposed and other non-milestone-aware
    # dispatches are unaffected.
    milestone_number: int | None = None
    # #1430: the issue's GitHub label names, when the caller already fetched
    # them (avoids a redundant GH call).  ``coord.dispatch.dispatch()``
    # consults this via ``config.models.model_for_labels()`` to resolve
    # ``models.labels`` for ``type="work"`` proposals when *model* wasn't
    # already set by the caller.  Empty by default — callers that don't
    # populate it simply get today's ``models.default`` behavior.
    issue_labels: list[str] = field(default_factory=list)
    # #1499: mirrors `Assignment.driven_by` — set by `coord/drive.py`'s
    # work-stage dispatch so the resulting assignment row (recorded via
    # `_record_dispatched_local`, the Proposal-based INSERT path every plain
    # non-`--interactive` `coord assign` — including `coord drive`'s — goes
    # through) carries durable provenance. `None` for every other caller
    # (brain-proposed, milestone dispatch, ...).
    driven_by: str | None = None
    # #2417: mirrors `Assignment.dispatched_by_assignment_id` — see that
    # field's docstring. Set here (rather than left for the caller to
    # populate) by `coord.state.record_dispatched` reading
    # `COORD_ASSIGNMENT_ID` from the environment at record time, so every
    # `Proposal`-based dispatch path (`coord fix`/`coord pr`/`coord review`
    # follow-ups via `_dispatch_followup`, `coord assign`,
    # `coord/dispatch_workers.py`, ...) gets this for free without each call
    # site threading it through by hand.
    dispatched_by_assignment_id: str | None = None


@dataclass
class SplitChunk:
    title: str
    scope: str
    files_likely: list[str] = field(default_factory=list)


@dataclass
class SplitProposal:
    id: int
    repo_name: str
    issue_number: int
    issue_title: str
    rationale: str
    chunks: list[SplitChunk] = field(default_factory=list)


@dataclass
class Board:
    repos: list[Repo] = field(default_factory=list)
    machines: list[Machine] = field(default_factory=list)
    active: list[Assignment] = field(default_factory=list)
    completed: list[Assignment] = field(default_factory=list)
    round_number: int = 0

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    def machine(self, name: str) -> Machine | None:
        return next((m for m in self.machines if m.name == name), None)

    def idle_machines(self) -> list[Machine]:
        busy = {a.machine_name for a in self.active if a.status == "running"}
        return [m for m in self.machines if m.name not in busy]

    def active_files_by_repo(self) -> dict[str, list[str]]:
        """Map of repo_name -> files currently being touched by running assignments."""
        result: dict[str, list[str]] = {}
        for a in self.active:
            if a.status != "running":
                continue
            result.setdefault(a.repo_name, []).extend(a.files_allowed)
        return result

    def mark_done(
        self,
        machine_name: str,
        branch: str | None = None,
        pr_url: str | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.machine_name == machine_name and a.status == "running":
                a.status = "done"
                a.branch = branch
                a.pr_url = pr_url
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def mark_failed(self, machine_name: str) -> Assignment | None:
        for a in self.active:
            if a.machine_name == machine_name and a.status == "running":
                a.status = "failed"
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def find_by_id(self, assignment_id: str) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                return a
        for a in self.completed:
            if a.assignment_id == assignment_id:
                return a
        return None

    def mark_done_by_id(
        self,
        assignment_id: str,
        branch: str | None = None,
        pr_url: str | None = None,
        finished_at: float | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                a.status = "done"
                if branch is not None:
                    a.branch = branch
                if pr_url is not None:
                    a.pr_url = pr_url
                a.finished_at = finished_at
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def mark_failed_by_id(
        self,
        assignment_id: str,
        finished_at: float | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                a.status = "failed"
                a.finished_at = finished_at
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def gc(self, keep: int = 50) -> int:
        """Remove oldest completed assignments beyond *keep*. Returns count removed."""
        if len(self.completed) <= keep:
            return 0
        by_time = sorted(self.completed, key=lambda a: a.finished_at or 0)
        to_remove = len(self.completed) - keep
        self.completed = by_time[to_remove:]
        return to_remove
