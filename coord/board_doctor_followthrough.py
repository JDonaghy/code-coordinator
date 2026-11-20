"""Board-doctor follow-through for ``merged-partial`` drive-queue rows.

A ``merged-partial`` row is an issue whose PR merged while its worker
declared ``ISSUE_RESOLUTION: partial`` — the code landed, but the issue's
own problem is not fully solved, so the issue stays open. Left alone, that
row sits forever with nobody owning the remainder. This module is the
disposition that resolves it:

1. **Plan leg.** :func:`build_plan_leg_prompt` composes a read-only prompt
   (issue body, the merged PR's review findings and ``ISSUE_RESOLUTION``
   text, the merged diff, the parent epic body, the target repo's
   CLAUDE.md). The injected *plan_leg* callable runs it and returns raw
   text; :func:`parse_plan` reads the JSON plan out of it.
2. **Validate.** :func:`validate_plan` rejects a plan that exceeds the
   per-issue cap, names an undeclared ``after`` target, cites an epic or
   milestone that does not exist, proposes a cross-backend issue for a repo
   with a one-backend-per-issue rule, or is internally inconsistent. A
   rejected plan alerts and writes nothing.
3. **Write.** Through the injected :class:`FollowThroughWriter`: create each
   follow-up (carrying :data:`FOLLOWTHROUGH_MARKER`), link it under the epic
   with its ``after`` edges, append it to the epic's ``## Work order``
   (existing rows preserved), then close the original with a comment
   listing what landed and each successor. A plan whose every item is
   ``verified_still_missing: false`` just closes with that evidence.
4. **Record.** Every disposition is appended to the board-doctor decision
   log (:func:`coord.state.record_board_doctor_decision`), which
   ``coord drive-queue sweep`` already reports from.

Depth limit: an issue whose body already carries
:data:`FOLLOWTHROUGH_MARKER` is itself a follow-through product. If it
merges partial again the disposition pages a human instead of recursing,
and never dispatches a plan leg for it.

:func:`run_followthrough` is pure with respect to its seams — the plan leg,
the writer, the alert/page sinks and the recorder are all parameters — so
the whole flow is testable with fakes and no network.
"""

from __future__ import annotations

import json
import os
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from coord.milestone_order import (
    WorkOrder,
    WorkOrderError,
    WorkOrderNode,
    parse_sub_issues,
    parse_work_order,
    render_work_order,
    replace_work_order_section,
)
from coord.preflight_size import detect_cross_backend_scope

#: Process-wide kill switch: set to any non-empty value to disable the
#: disposition regardless of config.
NO_FOLLOWTHROUGH_ENV_VAR = "COORD_BOARD_DOCTOR_NO_FOLLOWTHROUGH"

#: Per-issue follow-up budget used when no config value is supplied.
DEFAULT_FOLLOWUP_CAP = 5

#: Hidden marker stamped into every follow-up body this disposition files.
#: Its presence on a ``merged-partial`` issue is what trips the depth limit.
FOLLOWTHROUGH_MARKER = "<!-- coord:followthrough-of={issue} -->"
_FOLLOWTHROUGH_MARKER_RE = re.compile(r"<!--\s*coord:followthrough-of=#?(\d+)\s*-->")

ACTION_FILED = "followthrough-filed"
ACTION_ALL_LANDED = "followthrough-all-landed"
ACTION_REJECTED = "followthrough-rejected"
ACTION_DEPTH_PAGED = "followthrough-depth-paged"
ACTION_WRITE_FAILED = "followthrough-write-failed"
ACTION_PLAN_FAILED = "followthrough-plan-failed"
#: Nothing was filed because an already-open PR carries a GitHub closing
#: keyword for the parent — remediation is already in flight.
ACTION_ALREADY_COVERED = "followthrough-already-covered"

_ISSUE_REF_RE = re.compile(r"^#?(\d+)$")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)

#: Marker prefix for the one ``evidence`` string that carries a
#: ``followthrough-write-failed`` disposition's resumable state — the
#: validated plan plus which of its follow-ups already have a confirmed
#: issue number. ``evidence`` is the only free-form, JSON-round-tripping
#: field :func:`coord.state.record_board_doctor_decision` actually persists
#: from the dict :func:`Disposition.as_record` builds (every other key
#: there — ``follow_ups``, ``plan_leg_assignment_id``, ``queued``,
#: ``reasons`` — is read straight back out of the in-memory dict by this
#: module's own tests, but dropped on the floor by the real SQL writer,
#: which only has fixed columns for ``queue``/``key``/``cause``/``tier``/
#: ``action``/``count``/``escalated``/``confidence``/``evidence_json``/
#: ``outcome``/``recorded_at``). Piggybacking on ``evidence`` — rather than
#: adding a new column — keeps this a same-file change: `coord.board_doctor.
#: _load_pending_followthrough` reads it back via the existing
#: :func:`coord.state.board_doctor_decisions`, exactly like the diagnostic
#: evidence strings :func:`coord.board_doctor.merged_partial_rows` already
#: writes into the same field.
PENDING_FOLLOWTHROUGH_EVIDENCE_PREFIX = "pending-followthrough-json="


class PlanError(ValueError):
    """The plan leg's output could not be read as a follow-through plan."""


@dataclass(frozen=True)
class PartialMerge:
    """Everything the plan leg and the validator need about one row.

    ``repo_name`` is the LOCAL `coordinator.yml` repo name — the identity
    used for drive-queue/board bookkeeping (disposition keys, auto-queue
    entries, machine routing). ``github_repo`` is the GitHub ``owner/repo``
    slug, which every GitHub-side read/write must use instead
    (:mod:`coord.github_ops` takes the slug, never the local name — the two
    differ for most repos in this fleet). Use :attr:`github_slug`, never
    ``github_repo`` directly, since it falls back to ``repo_name`` when
    unset.
    """

    repo_name: str
    issue_number: int
    title: str
    body: str
    resolution_text: str = ""
    review_findings: str = ""
    merged_diff: str = ""
    pr_number: int | None = None
    epic_number: int | None = None
    epic_body: str = ""
    milestone: str | None = None
    claude_md: str = ""
    github_repo: str = ""

    @property
    def github_slug(self) -> str:
        """The ``owner/repo`` slug every :class:`FollowThroughWriter` call
        for this row must use. Falls back to :attr:`repo_name` when
        ``github_repo`` is unset, which keeps every existing caller/test
        that already treats the local name as the slug unchanged."""
        return self.github_repo or self.repo_name


@dataclass(frozen=True)
class RemainingItem:
    item: str
    verified_still_missing: bool
    evidence: str = ""


@dataclass(frozen=True)
class FollowUp:
    """One proposed successor issue.

    ``after`` entries are either ``"#N"`` (an issue already declared in the
    epic's ``## Work order``) or the ``key`` of an EARLIER follow-up in the
    same plan — the ordering requirement keeps the new edges acyclic.
    """

    key: str
    title: str
    body: str
    epic: int | None = None
    milestone: str | None = None
    after: tuple[str, ...] = ()
    capability: str | None = None
    machine: str | None = None
    files: tuple[str, ...] = ()
    #: An existing open issue/PR (``"#N"``) that already covers this
    #: slice, per the plan leg's own verification. When set, the write step
    #: links/orders *that* issue instead of filing a duplicate — the
    #: structured counterpart to a plan leg merely SAYING so in a
    #: follow-up's prose `body`, which the writer does not read.
    already_covered_by: str | None = None


@dataclass(frozen=True)
class FollowThroughPlan:
    items: tuple[RemainingItem, ...] = ()
    follow_ups: tuple[FollowUp, ...] = ()

    @property
    def all_landed(self) -> bool:
        return not any(i.verified_still_missing for i in self.items)


@dataclass(frozen=True)
class PendingFollowThrough:
    """Resumable state for a parent row whose previous disposition
    created some follow-ups durably but did not finish (#4007): the plan
    that was already read, validated and rendered, plus which of its
    `key`s already have a confirmed issue number. A later run reuses this
    instead of dispatching a fresh plan leg and re-creating what already
    exists — see :func:`run_followthrough`'s `coverage` parameter."""

    plan: FollowThroughPlan
    follow_up_keys: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FollowThroughCoverage:
    """Pre-existing coverage for a parent row, checked BEFORE planning
    (#4007).

    ``covering_pr`` is an open PR number whose body already carries a
    GitHub closing keyword for the parent issue — remediation is already
    in flight, so :func:`run_followthrough` files nothing and does not
    even dispatch a plan leg. ``pending`` is this parent's own unfinished
    attempt (see :class:`PendingFollowThrough`) — resuming it skips the
    plan leg and every already-filed `create_issue`, retrying only the
    link/epic-splice/close steps that did not confirm last time.
    """

    covering_pr: int | None = None
    pending: PendingFollowThrough | None = None


@dataclass(frozen=True)
class PlanLegResult:
    """What the injected plan leg returns: its assignment id and raw output."""

    assignment_id: str
    output: str


@dataclass
class Disposition:
    repo_name: str
    issue_number: int
    action: str
    dry_run: bool = False
    plan_leg_assignment_id: str | None = None
    follow_ups: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    rendered_plan: str = ""
    #: Plan key → filed issue number, for every follow-up created so far
    #: (including one reused from a resumed, previously-recorded attempt).
    follow_up_keys: dict[str, int] = field(default_factory=dict)
    #: Plan key → existing issue number, for every follow-up the plan
    #: itself named `already_covered_by` rather than asking to file (#4007).
    deduped: dict[str, int] = field(default_factory=dict)
    #: Filed issue number → pinned machine ("" = left to `coord drive`) for
    #: every follow-up the auto-queue step enqueued.
    queued: dict[int, str] = field(default_factory=dict)
    #: The plan this disposition acted on (``None`` before one is read or
    #: resumed) — serialized into `as_record`'s `evidence` so a write
    #: failure's resumable state survives into the durable log (#4007).
    plan: FollowThroughPlan | None = None
    #: The decision log's `cause` column (#4038). Defaults to
    #: ``"merged-partial"`` — every caller before #4038 built this
    #: dataclass for exactly one cause (`coord.board_doctor.
    #: CAUSE_MERGED_PARTIAL`'s own string value, duplicated here as a raw
    #: literal rather than imported since `coord.board_doctor` imports
    #: THIS module, not the other way round) — so an existing caller that
    #: never sets this field records byte-for-byte what it always did.
    #: :func:`run_refile` is the one other caller, and passes
    #: ``"branch-merged-guard"`` (`coord.board_doctor.
    #: CAUSE_BRANCH_MERGED_GUARD`'s own string value, same reason).
    cause: str = "merged-partial"

    def as_record(self) -> dict[str, Any]:
        evidence = list(self.reasons)
        if self.action == ACTION_WRITE_FAILED and self.plan is not None:
            evidence.append(
                PENDING_FOLLOWTHROUGH_EVIDENCE_PREFIX
                + json.dumps({
                    "plan": _plan_to_jsonable(self.plan),
                    "follow_up_keys": {str(k): v for k, v in self.follow_up_keys.items()},
                })
            )
        return {
            "queue": "drive-queue",
            "key": f"{self.repo_name}#{self.issue_number}",
            "cause": self.cause,
            "action": self.action,
            "dry_run": self.dry_run,
            "plan_leg_assignment_id": self.plan_leg_assignment_id,
            "follow_ups": list(self.follow_ups),
            "follow_up_keys": {str(k): v for k, v in self.follow_up_keys.items()},
            "deduped": {str(k): v for k, v in self.deduped.items()},
            "queued": {str(k): v for k, v in self.queued.items()},
            "reasons": list(self.reasons),
            "evidence": evidence,
        }


class FollowThroughWriter(Protocol):
    """The GitHub-side writes this disposition performs."""

    def create_issue(
        self, repo_name: str, *, title: str, body: str, milestone: str | None,
        labels: Sequence[str] = (),
    ) -> int: ...

    def add_child(
        self, repo_name: str, *, epic: int, child: int, after: Sequence[int],
    ) -> None: ...

    def get_issue_body(self, repo_name: str, number: int) -> str: ...

    def set_issue_body(self, repo_name: str, number: int, body: str) -> None: ...

    def close_issue(self, repo_name: str, number: int, *, comment: str) -> None: ...

    #: #4038: a pointer comment that does NOT close anything — `run_refile`
    #: posts the refile's `#N` onto the original issue, which stays open
    #: until the refile itself is observed closed (see
    #: :func:`coord.board_doctor.run_pending_refile_close_sweep`).
    def comment_issue(self, repo_name: str, number: int, *, body: str) -> None: ...


@dataclass
class GitHubFollowThroughWriter:
    """Real :class:`FollowThroughWriter` over the GitHub-backed
    issue-tracker seam (:mod:`coord.github_ops`) — never `gh` directly
    (#3945). ``add_child`` links the live GitHub sub-issues relationship
    (:func:`coord.github_ops.add_sub_issue`); the ``after`` ordering itself
    is recorded separately, in the epic body's own checklist
    (:func:`_write_follow_ups`/:func:`_splice_epic_checklists`), since the
    sub-issues API has no concept of inter-child ordering.

    Every write here reads its own result back and raises when the read
    disagrees with what was requested — a clean `gh`/REST return only
    proves the request was issued, not that it landed (the same rule
    `coord.merge_queue.process` applies to its own post-`close_issue`
    check). The caller (:func:`run_followthrough`) already treats any
    exception from a write as a failure to alert on, so an unconfirmed
    write surfaces exactly the same way a `gh` error would.

    ``add_child``'s read-back retries with a short backoff — a clean
    ``add_sub_issue`` return whose immediate read-back still sees the
    child's parent unset is more likely read-after-write lag on GitHub's
    own API than a real failure — before concluding the link truly did
    not take. *sleep*/*readback_attempts* are injectable for tests.
    """

    sleep: Callable[[float], None] = _time.sleep
    readback_attempts: int = 3
    readback_backoff_seconds: float = 1.0

    def create_issue(
        self, repo_name: str, *, title: str, body: str, milestone: str | None,
        labels: Sequence[str] = (),
    ) -> int:
        from coord import github_ops  # noqa: PLC0415

        result = github_ops.create_issue(
            repo_name, title, body, labels=list(labels) or None, milestone=milestone,
        )
        return int(result["number"])

    def add_child(
        self, repo_name: str, *, epic: int, child: int, after: Sequence[int],
    ) -> None:
        from coord import github_ops  # noqa: PLC0415

        github_ops.add_sub_issue(repo_name, parent_number=epic, child_number=child)
        attempts = max(1, self.readback_attempts)
        linked: int | None = None
        for attempt in range(attempts):
            parent = github_ops.get_issue_parent(repo_name, child)
            linked = parent.get("number") if parent else None
            if linked == epic:
                return
            if attempt < attempts - 1:
                self.sleep(self.readback_backoff_seconds * (attempt + 1))
        raise RuntimeError(
            f"add_sub_issue({repo_name}, parent={epic}, child={child}) "
            f"returned without error but {repo_name}#{child}'s parent "
            f"still reads {linked!r}, not {epic}, after "
            f"{attempts} read-back attempt{'s' if attempts != 1 else ''}"
        )

    def get_issue_body(self, repo_name: str, number: int) -> str:
        from coord import github_ops  # noqa: PLC0415

        return github_ops.get_issue(repo_name, number).get("body") or ""

    def set_issue_body(self, repo_name: str, number: int, body: str) -> None:
        from coord import github_ops  # noqa: PLC0415

        github_ops.update_issue_body(repo_name, number, body)
        actual = github_ops.get_issue(repo_name, number).get("body") or ""
        if actual != body:
            raise RuntimeError(
                f"update_issue_body({repo_name}#{number}) returned without "
                "error but the issue body read back afterward does not "
                "match what was written"
            )

    def close_issue(self, repo_name: str, number: int, *, comment: str) -> None:
        from coord import github_ops  # noqa: PLC0415

        github_ops.close_issue(repo_name, number, comment=comment)
        state = github_ops.get_issue_state(repo_name, number)
        if state != "closed":
            raise RuntimeError(
                f"close_issue({repo_name}#{number}) returned without error "
                f"but the issue reads {state!r}, not confirmed closed"
            )

    def comment_issue(self, repo_name: str, number: int, *, body: str) -> None:
        from coord import github_ops  # noqa: PLC0415

        github_ops.post_issue_comment(repo_name, number, body)


# ── Gating ───────────────────────────────────────────────────────────────────


def followthrough_enabled(repo_name: str, config: Any) -> bool:
    """Whether the disposition may act for *repo_name*.

    Three independent gates, mirroring the other board-doctor tiers:
    :data:`NO_FOLLOWTHROUGH_ENV_VAR`, the fleet-wide
    ``board_doctor.followthrough`` switch, and the per-repo
    ``repos[].board_doctor.followthrough`` opt-in (default off).
    """
    if os.environ.get(NO_FOLLOWTHROUGH_ENV_VAR):
        return False
    if not config.board_doctor.followthrough:
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.followthrough)


def followthrough_auto_queue_enabled(repo_name: str, config: Any) -> bool:
    """Whether filed follow-ups for *repo_name* are also drive-queued.

    Requires :func:`followthrough_enabled` plus the per-repo
    ``repos[].board_doctor.followthrough_auto_queue`` opt-in (default off).
    """
    if not followthrough_enabled(repo_name, config):
        return False
    repo = config.repo(repo_name)
    return bool(getattr(repo.board_doctor, "followthrough_auto_queue", False))


def is_followthrough_product(body: str | None) -> bool:
    """True when *body* carries :data:`FOLLOWTHROUGH_MARKER`."""
    return bool(body and _FOLLOWTHROUGH_MARKER_RE.search(body))


# ── Plan leg I/O ─────────────────────────────────────────────────────────────

_PLAN_SCHEMA = """\
{
  "items": [
    {"item": "<one remaining deliverable>", "verified_still_missing": true,
     "evidence": "<file:line / grep result / test showing it is (or is not) missing>"}
  ],
  "follow_ups": [
    {"key": "a", "title": "...", "body": "...", "epic": <int or null>,
     "milestone": "<title or null>", "after": ["#123", "<earlier key>"],
     "capability": "<or null>", "machine": "<or null>", "files": ["path", ...],
     "already_covered_by": "<#N of an existing open issue/PR that already covers this slice, or null>"}
  ]
}"""


def build_plan_leg_prompt(partial: PartialMerge) -> str:
    """The read-only plan leg's prompt for *partial*."""
    sections = [
        "You are a READ-ONLY follow-through planner. Do not edit, commit or push "
        "anything. You are checked out at the base branch HEAD, after the PR "
        f"for {partial.repo_name}#{partial.issue_number} merged with "
        "ISSUE_RESOLUTION: partial.",
        "For every deliverable in the issue that the resolution says remains, "
        "verify against the code whether it is still missing and cite evidence. "
        "Then propose one follow-up issue per coherent remaining slice. "
        "`after` may name issues already in the epic's `## Work order` (as "
        "\"#N\") or the key of an EARLIER follow-up in your list. Check whether "
        "an existing OPEN issue or PR already covers a slice before proposing a "
        "new follow-up for it — if one does, set that follow-up's "
        "`already_covered_by` to its `#N` instead of writing it into the prose "
        "`body`; the writer only reads the structured field.",
        "Reply with exactly one JSON object in this shape and nothing else:\n"
        + _PLAN_SCHEMA,
        f"## Issue #{partial.issue_number}: {partial.title}\n\n{partial.body}",
        f"## ISSUE_RESOLUTION text\n\n{partial.resolution_text or '(none recorded)'}",
        f"## Review findings\n\n{partial.review_findings or '(none recorded)'}",
        f"## Merged diff\n\n```diff\n{partial.merged_diff}\n```",
    ]
    if partial.epic_number is not None:
        sections.append(f"## Parent epic #{partial.epic_number}\n\n{partial.epic_body}")
    if partial.claude_md:
        sections.append(f"## Target repo CLAUDE.md\n\n{partial.claude_md}")
    return "\n\n".join(sections)


def _extract_json(text: str) -> Any:
    candidates = [m.group(1) for m in _JSON_FENCE_RE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise PlanError("plan leg output contains no parseable JSON object")


def _opt_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PlanError(f"follow-up field {key!r} must be a string")
    return value


def _opt_issue_ref(raw: Mapping[str, Any], key: str) -> str | None:
    """Like :func:`_opt_str`, but also accepts a bare int issue number —
    `epic` three lines below already does, so a plan leg spelling this
    field the same way must not hard-fail the whole plan."""
    value = raw.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return f"#{value}"
    if not isinstance(value, str):
        raise PlanError(f"follow-up field {key!r} must be a string or integer")
    return value


def parse_plan(text: str) -> FollowThroughPlan:
    """Parse the plan leg's output into a :class:`FollowThroughPlan`.

    Accepts a bare JSON object or one inside a fenced code block. Raises
    :class:`PlanError` for anything that does not match the schema.
    """
    data = _extract_json(text)
    if not isinstance(data, dict):
        raise PlanError("plan must be a JSON object")
    raw_items = data.get("items", [])
    raw_follow_ups = data.get("follow_ups", [])
    if not isinstance(raw_items, list) or not isinstance(raw_follow_ups, list):
        raise PlanError("`items` and `follow_ups` must be lists")

    items: list[RemainingItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or not isinstance(raw.get("item"), str):
            raise PlanError("each item needs a string `item`")
        missing = raw.get("verified_still_missing")
        if not isinstance(missing, bool):
            raise PlanError(f"item {raw['item']!r}: `verified_still_missing` must be a boolean")
        items.append(RemainingItem(
            item=raw["item"], verified_still_missing=missing,
            evidence=str(raw.get("evidence") or ""),
        ))

    follow_ups: list[FollowUp] = []
    for index, raw in enumerate(raw_follow_ups):
        if not isinstance(raw, dict):
            raise PlanError("each follow-up must be an object")
        title, body = raw.get("title"), raw.get("body")
        if not isinstance(title, str) or not title.strip():
            raise PlanError(f"follow-up {index}: `title` must be a non-empty string")
        if not isinstance(body, str):
            raise PlanError(f"follow-up {index}: `body` must be a string")
        epic = raw.get("epic")
        if epic is not None and (isinstance(epic, bool) or not isinstance(epic, int)):
            raise PlanError(f"follow-up {index}: `epic` must be an integer or null")
        after = raw.get("after") or []
        files = raw.get("files") or []
        if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
            raise PlanError(f"follow-up {index}: `after` must be a list of strings")
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise PlanError(f"follow-up {index}: `files` must be a list of strings")
        key = raw.get("key")
        follow_ups.append(FollowUp(
            key=str(key) if key not in (None, "") else str(index),
            title=title.strip(), body=body, epic=epic,
            milestone=_opt_str(raw, "milestone"),
            after=tuple(a.strip() for a in after),
            capability=_opt_str(raw, "capability"),
            machine=_opt_str(raw, "machine"),
            files=tuple(files),
            already_covered_by=_opt_issue_ref(raw, "already_covered_by"),
        ))
    return FollowThroughPlan(items=tuple(items), follow_ups=tuple(follow_ups))


def _plan_to_jsonable(plan: FollowThroughPlan) -> dict[str, Any]:
    """*plan* in the same shape :func:`parse_plan` reads — the inverse is
    :func:`plan_from_jsonable`, which reuses :func:`parse_plan` itself
    rather than a second hand-rolled reader, so a resumed plan is read back
    through the exact same validation a fresh plan leg's output goes
    through (#4007)."""
    return {
        "items": [
            {"item": i.item, "verified_still_missing": i.verified_still_missing,
             "evidence": i.evidence}
            for i in plan.items
        ],
        "follow_ups": [
            {"key": f.key, "title": f.title, "body": f.body, "epic": f.epic,
             "milestone": f.milestone, "after": list(f.after),
             "capability": f.capability, "machine": f.machine,
             "files": list(f.files), "already_covered_by": f.already_covered_by}
            for f in plan.follow_ups
        ],
    }


def plan_from_jsonable(data: Mapping[str, Any]) -> FollowThroughPlan:
    """Inverse of :func:`_plan_to_jsonable` — the one parser
    (:func:`parse_plan`) handles both a fresh plan leg's raw text and a
    resumed disposition's persisted JSON, rather than two independently
    drifting readers for the same schema (#4007)."""
    return parse_plan(json.dumps(data))


def _issue_ref_to_int(ref: str) -> int | None:
    """``"#123"``/``"123"`` → ``123``; anything else → ``None``."""
    match = _ISSUE_REF_RE.match(ref.strip())
    return int(match.group(1)) if match else None


# ── Validation ───────────────────────────────────────────────────────────────




def validate_plan(
    plan: FollowThroughPlan,
    *,
    partial: PartialMerge,
    cap: int = DEFAULT_FOLLOWUP_CAP,
    backends: Sequence[str] = (),
    epic_exists: Callable[[int], bool] = lambda _n: True,
    milestone_exists: Callable[[str], bool] = lambda _m: True,
    covering_issue_open: Callable[[int], bool] = lambda _n: True,
) -> list[str]:
    """Every reason *plan* must not be written; empty means it is valid.

    *covering_issue_open* answers whether a follow-up's `already_covered_by`
    reference names an issue that is readably OPEN right now — the plan
    leg's word that a reference parses as `#N` is not, by itself, proof the
    issue exists, is still open, or has anything to do with this parent.
    Defaulting to "always open" keeps every caller that does not pass a
    live check (most tests) exercising the same parse-only validation as
    before; `coord.board_doctor` wires its own `_issue_open`.
    """
    reasons: list[str] = []
    if not plan.items:
        reasons.append("plan verifies no remaining items")
    if plan.all_landed:
        if plan.follow_ups:
            reasons.append(
                "plan proposes follow-ups but verifies every item already landed"
            )
        return reasons
    if not plan.follow_ups:
        reasons.append("plan verifies items still missing but proposes no follow-up")
    if len(plan.follow_ups) > cap:
        reasons.append(
            f"plan proposes {len(plan.follow_ups)} follow-ups, over the per-issue cap of {cap}"
        )

    try:
        declared = set(parse_work_order(partial.epic_body or "").issue_numbers)
    except WorkOrderError as exc:
        declared = set()
        reasons.append(f"epic's ## Work order does not parse: {exc}")

    # #3945 review: `_splice_epic_checklists` splices the SAME `after`
    # edges into `## Sub-issues` too, whenever that section also exists —
    # `parse_sub_issues` then requires every edge to be declared WITHIN
    # that section (it does not fall back to `## Work order`'s). Checking
    # that here, before anything is written, turns a plan this validator
    # would otherwise accept but the write would hard-fail on (after the
    # follow-up issues already exist — orphaning them) into an ordinary
    # rejection: alert, write nothing.
    sub_issues_bounds = _find_checklist_bounds(
        (partial.epic_body or "").splitlines(), _SUB_ISSUES_HEADING_RE,
    )
    declared_sub_issues: set[int] | None = None
    if sub_issues_bounds is not None:
        try:
            declared_sub_issues = set(parse_sub_issues(partial.epic_body or "").issue_numbers)
        except WorkOrderError as exc:
            declared_sub_issues = set()
            reasons.append(f"epic's ## Sub-issues does not parse: {exc}")

    seen_keys: set[str] = set()
    for fu in plan.follow_ups:
        label = f"follow-up {fu.key!r} ({fu.title})"
        if fu.key in seen_keys:
            reasons.append(f"{label}: duplicate key")
        for target in fu.after:
            ref = _ISSUE_REF_RE.match(target)
            if target.startswith("#") and ref:
                if int(ref.group(1)) not in declared:
                    reasons.append(
                        f"{label}: `after` target {target} is not declared in the "
                        "epic's ## Work order"
                    )
                elif (
                    declared_sub_issues is not None
                    and int(ref.group(1)) not in declared_sub_issues
                ):
                    reasons.append(
                        f"{label}: `after` target {target} is declared in the "
                        "epic's ## Work order but not its ## Sub-issues section "
                        "— splicing the same edge into both would fail"
                    )
            elif target not in seen_keys:
                reasons.append(
                    f"{label}: `after` target {target!r} is not an earlier follow-up key"
                )
        seen_keys.add(fu.key)
        epic = fu.epic if fu.epic is not None else partial.epic_number
        if fu.after and epic is None:
            reasons.append(f"{label}: declares `after` edges but has no epic to record them in")
        if fu.epic is not None and not epic_exists(fu.epic):
            reasons.append(f"{label}: epic #{fu.epic} does not exist")
        if fu.milestone is not None and not milestone_exists(fu.milestone):
            reasons.append(f"{label}: milestone {fu.milestone!r} does not exist")
        if fu.already_covered_by is not None:
            covering_number = _issue_ref_to_int(fu.already_covered_by)
            if covering_number is None:
                reasons.append(
                    f"{label}: `already_covered_by` {fu.already_covered_by!r} is "
                    "not a valid issue reference"
                )
            elif not covering_issue_open(covering_number):
                reasons.append(
                    f"{label}: `already_covered_by` {fu.already_covered_by!r} does "
                    "not reference a currently open issue"
                )
        named = detect_cross_backend_scope(title=fu.title, body=fu.body, backends=backends)
        if named:
            reasons.append(
                f"{label}: crosses backends ({', '.join(named)}) in a repo with a "
                "one-backend-per-issue rule"
            )
    return reasons


# ── Rendering ────────────────────────────────────────────────────────────────


def render_plan(plan: FollowThroughPlan) -> str:
    """Human-readable plan, used by dry-run output and alert bodies."""
    lines = ["Remaining items:"]
    for item in plan.items:
        status = "still missing" if item.verified_still_missing else "landed"
        lines.append(f"  - [{status}] {item.item}")
        if item.evidence:
            lines.append(f"      evidence: {item.evidence}")
    if plan.follow_ups:
        lines.append("Proposed follow-ups:")
        for fu in plan.follow_ups:
            extras = []
            if fu.epic is not None:
                extras.append(f"epic #{fu.epic}")
            if fu.milestone:
                extras.append(f"milestone {fu.milestone!r}")
            if fu.after:
                extras.append("after " + ",".join(fu.after))
            if fu.capability:
                extras.append(f"capability {fu.capability}")
            if fu.machine:
                extras.append(f"machine {fu.machine}")
            if fu.already_covered_by:
                extras.append(f"already covered by {fu.already_covered_by}")
            suffix = f" ({'; '.join(extras)})" if extras else ""
            lines.append(f"  - [{fu.key}] {fu.title}{suffix}")
            if fu.files:
                lines.append(f"      files: {', '.join(fu.files)}")
    return "\n".join(lines)


def _follow_up_body(fu: FollowUp, original: int) -> str:
    parts = [fu.body.rstrip()]
    if fu.files and "## Files" not in fu.body:
        parts.append("## Files\n" + "\n".join(f"- `{f}`" for f in fu.files))
    if fu.capability or fu.machine:
        routing = []
        if fu.capability:
            routing.append(f"capability: {fu.capability}")
        if fu.machine:
            routing.append(f"machine: {fu.machine}")
        parts.append("## Routing\n" + "\n".join(f"- {r}" for r in routing))
    parts.append(f"Follow-through of #{original}.")
    parts.append(FOLLOWTHROUGH_MARKER.format(issue=original))
    return "\n\n".join(p for p in parts if p)


def _close_comment(plan: FollowThroughPlan, successors: Sequence[tuple[int, str]]) -> str:
    landed = [i for i in plan.items if not i.verified_still_missing]
    lines = ["Board-doctor follow-through: closing this merged-partial issue."]
    if landed:
        lines.append("\n**Verified landed:**")
        lines.extend(f"- {i.item} — {i.evidence}" if i.evidence else f"- {i.item}"
                     for i in landed)
    if successors:
        lines.append("\n**Remaining work moved to:**")
        lines.extend(f"- #{n} — {title}" for n, title in successors)
    return "\n".join(lines)


# ── Auto-queue ───────────────────────────────────────────────────────────────

#: Short capability spellings a plan leg may use for the configured names.
_CAPABILITY_ALIASES = {"win": "windows", "mac": "macos"}


def normalize_capability(raw: str) -> str:
    """``"[win]"`` / ``"Win"`` → ``"windows"``; unknown names pass through."""
    name = raw.strip().strip("[]").strip().lower()
    return _CAPABILITY_ALIASES.get(name, name)


def capable_machines(
    capability: str, repo_name: str, machines: Sequence[Any],
) -> list[str]:
    """Machines that declare *capability* and have *repo_name* checked out.

    Same candidate filter as :func:`coord.dispatch.route_work_by_capability`
    (``can_work_on`` plus a configured ``repo_path``), in ``coordinator.yml``
    declaration order. Pause/quiet-hours state is not consulted: a queue pin
    outlives a pause, and the tick already waits for a paused pinned machine.
    """
    return [
        m.name for m in machines
        if capability in m.capabilities
        and m.can_work_on(repo_name)
        and m.repo_path(repo_name) is not None
    ]


def _default_enqueue(repo_name: str, issue_number: int, **kwargs: Any) -> Any:
    from coord.state import enqueue_drive_queue  # noqa: PLC0415

    return enqueue_drive_queue(repo_name, issue_number, **kwargs)


@dataclass
class AutoQueueContext:
    """What the auto-queue step needs beyond the plan.

    *entries* is the drive queue as it stands (``QueueEntry`` rows);
    *draining_epic* is the epic whose rows the queue is currently working
    through, or ``None``. *enqueue* has the signature of
    :func:`coord.state.enqueue_drive_queue`.
    """

    machines: Sequence[Any]
    entries: Sequence[Any] = ()
    draining_epic: int | None = None
    enqueue: Callable[..., Any] = _default_enqueue


def build_auto_queue_context(
    repo_name: str,
    config: Any,
    *,
    entries: Sequence[Any] = (),
    draining_epic: int | None = None,
    enqueue: Callable[..., Any] = _default_enqueue,
) -> AutoQueueContext | None:
    """An :class:`AutoQueueContext`, or ``None`` when *repo_name* has not
    opted in — ``None`` makes :func:`run_followthrough` file but never queue."""
    if not followthrough_auto_queue_enabled(repo_name, config):
        return None
    return AutoQueueContext(
        machines=list(config.machines), entries=entries,
        draining_epic=draining_epic, enqueue=enqueue,
    )


def _queue_position(partial: PartialMerge, ctx: AutoQueueContext) -> int | None:
    """The original row's slot when its epic is draining, else ``None`` (tail)."""
    if partial.epic_number is None or partial.epic_number != ctx.draining_epic:
        return None
    for entry in ctx.entries:
        if entry.repo == partial.repo_name and entry.issue == partial.issue_number:
            return int(entry.position)
    return None


def auto_queue_follow_ups(
    partial: PartialMerge,
    plan: FollowThroughPlan,
    disposition: Disposition,
    ctx: AutoQueueContext,
    *,
    alert: Callable[[str, str], None],
) -> None:
    """Drive-queue every filed follow-up in plan order.

    ``after`` edges are the plan's own, resolved to ``repo#N`` keys. A
    follow-up declaring a capability is pinned to the first capable machine
    (or its own declared ``machine`` when that one is capable); one whose
    capability no machine covers is not queued and alerts instead. Without a
    capability the follow-up's declared ``machine`` is used if configured,
    else routing is left to ``coord drive``.

    A follow-up resolved via `already_covered_by` (#4007) is never
    (re-)queued here — it points at an existing issue this disposition did
    not file, and that issue's own queue state (if any) is not this
    disposition's to change.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        QueueEntry,
        QueueError,
        entry_key,
        validate_enqueue,
    )

    repo = partial.repo_name
    known_machines = {m.name for m in ctx.machines}
    entries: list[Any] = list(ctx.entries)
    position = _queue_position(partial, ctx)
    for fu in plan.follow_ups:
        if fu.key in disposition.deduped:
            continue
        number = disposition.follow_up_keys.get(fu.key)
        if number is None:
            continue
        key = entry_key(repo, number)
        machine: str | None
        if fu.capability:
            capability = normalize_capability(fu.capability)
            capable = capable_machines(capability, repo, ctx.machines)
            if not capable:
                reason = (
                    f"#{number} not queued: no machine declares capability "
                    f"{capability!r} for {repo}"
                )
                disposition.reasons.append(reason)
                alert(f"{key}: follow-up capability unroutable", reason)
                continue
            machine = fu.machine if fu.machine in capable else capable[0]
        else:
            machine = fu.machine if fu.machine in known_machines else None
        after = [
            entry_key(repo, int(t[1:]) if t.startswith("#")
                      else disposition.follow_up_keys[t])
            for t in fu.after
        ]
        try:
            validate_enqueue(entries, repo, number, after)
        except QueueError as exc:
            reason = f"#{number} not queued: {exc}"
            disposition.reasons.append(reason)
            alert(f"{key}: follow-up not queued", reason)
            continue
        ctx.enqueue(repo, number, machine=machine, after=after, position=position)
        entries.append(QueueEntry(
            repo=repo, issue=number, machine=machine or "", after=tuple(after),
        ))
        disposition.queued[number] = machine or ""
        if position is not None:
            position += 1


# ── Orchestration ────────────────────────────────────────────────────────────


def _default_record(entry: dict) -> None:
    from coord.state import record_board_doctor_decision  # noqa: PLC0415

    record_board_doctor_decision(entry)


def run_followthrough(
    partial: PartialMerge,
    *,
    plan_leg: Callable[[str], PlanLegResult],
    writer: FollowThroughWriter,
    alert: Callable[[str, str], None],
    page: Callable[[str, str], None],
    record: Callable[[dict], None] = _default_record,
    cap: int = DEFAULT_FOLLOWUP_CAP,
    backends: Sequence[str] = (),
    epic_exists: Callable[[int], bool] = lambda _n: True,
    milestone_exists: Callable[[str], bool] = lambda _m: True,
    covering_issue_open: Callable[[int], bool] = lambda _n: True,
    dry_run: bool = False,
    auto_queue: AutoQueueContext | None = None,
    coverage: FollowThroughCoverage | None = None,
) -> Disposition:
    """Resolve one ``merged-partial`` row end to end.

    *alert* and *page* take ``(subject, body)``. With *dry_run* the plan leg
    still runs (it is read-only) and the plan is validated and rendered,
    but nothing is written, alerted, paged or recorded.

    *auto_queue* (see :func:`build_auto_queue_context`) additionally
    drive-queues each filed follow-up once the original is closed; ``None``
    files without queueing.

    *coverage* (:class:`FollowThroughCoverage`, #4007) is checked BEFORE the
    plan leg runs at all: an open PR already carrying a closing keyword for
    the parent short-circuits straight to :data:`ACTION_ALREADY_COVERED`
    (nothing is planned, filed or paged); a recorded, unfinished attempt
    (``coverage.pending``) is resumed instead of dispatching a fresh plan
    leg — its already-validated plan is reused and
    :func:`_write_follow_ups` skips `create_issue` for every follow-up that
    already has a confirmed number, retrying only the link/close steps that
    did not confirm last time.

    *covering_issue_open* is forwarded to :func:`validate_plan` to confirm
    a plan's own `already_covered_by` claim names a real, currently open
    issue before the dedupe is honoured.
    """
    key = f"{partial.repo_name}#{partial.issue_number}"
    disposition = Disposition(
        repo_name=partial.repo_name, issue_number=partial.issue_number,
        action="", dry_run=dry_run,
    )

    def _finish(action: str) -> Disposition:
        disposition.action = action
        if not dry_run:
            record(disposition.as_record())
        return disposition

    if is_followthrough_product(partial.body):
        disposition.reasons.append(
            "issue is itself a follow-through product and merged partial again"
        )
        if not dry_run:
            page(
                f"{key}: follow-through product merged partial again",
                f"{key} was filed by the board-doctor follow-through and its PR "
                "merged with ISSUE_RESOLUTION: partial again. Not recursing — a "
                "human needs to decide how to finish it.",
            )
        return _finish(ACTION_DEPTH_PAGED)

    if coverage is not None and coverage.covering_pr is not None:
        disposition.reasons.append(
            f"open PR #{coverage.covering_pr} already carries a GitHub closing "
            f"keyword for {key} — remediation is already in flight, not filing"
        )
        return _finish(ACTION_ALREADY_COVERED)

    pending = coverage.pending if coverage is not None else None
    already_created: dict[str, int] = dict(pending.follow_up_keys) if pending else {}
    if pending is not None:
        plan = pending.plan
    else:
        result = plan_leg(build_plan_leg_prompt(partial))
        disposition.plan_leg_assignment_id = result.assignment_id
        try:
            plan = parse_plan(result.output)
        except PlanError as exc:
            disposition.reasons.append(str(exc))
            if not dry_run:
                alert(f"{key}: follow-through plan unreadable", str(exc))
            return _finish(ACTION_PLAN_FAILED)

    disposition.plan = plan
    disposition.rendered_plan = render_plan(plan)
    reasons = validate_plan(
        plan, partial=partial, cap=cap, backends=backends,
        epic_exists=epic_exists, milestone_exists=milestone_exists,
        covering_issue_open=covering_issue_open,
    )
    if reasons:
        disposition.reasons.extend(reasons)
        if not dry_run:
            alert(
                f"{key}: follow-through plan rejected",
                "\n".join(f"- {r}" for r in reasons) + "\n\n" + disposition.rendered_plan,
            )
        return _finish(ACTION_REJECTED)

    if dry_run:
        disposition.action = ACTION_ALL_LANDED if plan.all_landed else ACTION_FILED
        return disposition

    try:
        successors = _write_follow_ups(
            partial, plan, writer, disposition, already_created=already_created,
        )
        writer.close_issue(
            partial.github_slug, partial.issue_number,
            comment=_close_comment(plan, successors),
        )
    except Exception as exc:  # noqa: BLE001 — every write failure must alert
        disposition.reasons.append(f"write failed: {exc}")
        alert(
            f"{key}: follow-through write failed",
            f"Writing stopped at: {exc}\nFollow-ups already filed: "
            # `follow_up_keys` (not `follow_ups`): a resumed attempt's
            # reused child issues confirm here too, not just one THIS run
            # freshly created — "Filed so far: none" would otherwise be
            # false the moment a resumed write fails again.
            + (", ".join(f"#{n}" for n in disposition.follow_up_keys.values()) or "none"),
        )
        return _finish(ACTION_WRITE_FAILED)
    if auto_queue is not None and disposition.follow_up_keys:
        try:
            auto_queue_follow_ups(partial, plan, disposition, auto_queue, alert=alert)
        except Exception as exc:  # noqa: BLE001 — the follow-ups are filed; alert, don't fail
            disposition.reasons.append(f"auto-queue failed: {exc}")
            alert(f"{key}: follow-up auto-queue failed", str(exc))
    if not plan.all_landed and not disposition.follow_ups and disposition.deduped:
        # Every follow-up resolved via `already_covered_by` — nothing was
        # actually filed this run. `ACTION_FILED` still closes the parent
        # correctly (the remainder has somewhere to live), but a bare
        # action name reads as a claim that something new was filed, so
        # the decision log carries an explicit reason instead.
        disposition.reasons.append(
            "every follow-up was already covered by an existing issue; "
            "nothing new was filed"
        )
    return _finish(ACTION_ALL_LANDED if plan.all_landed else ACTION_FILED)


def _write_follow_ups(
    partial: PartialMerge,
    plan: FollowThroughPlan,
    writer: FollowThroughWriter,
    disposition: Disposition,
    *,
    already_created: Mapping[str, int] | None = None,
) -> list[tuple[int, str]]:
    """Create, link and order every follow-up; returns ``(number, title)``s.

    *already_created* (#4007) is a plan ``key`` → issue-number map for a
    follow-up a PREVIOUS, unfinished attempt already confirmed
    `create_issue` for — skipped here rather than re-created; only the
    link/epic-splice steps run for it, since those are what did not confirm
    last time.

    A follow-up whose plan names `already_covered_by` is never created at
    all, this run or any other — the existing issue it points at is linked
    and ordered in its place (#4007).
    """
    if plan.all_landed:
        return []
    by_key: dict[str, int] = dict(already_created or {})
    successors: list[tuple[int, str]] = []
    new_nodes: dict[int, list[WorkOrderNode]] = {}
    repo = partial.github_slug
    for fu in plan.follow_ups:
        covering = _issue_ref_to_int(fu.already_covered_by) if fu.already_covered_by else None
        if covering is not None:
            disposition.deduped[fu.key] = covering
            number = covering
        elif fu.key in by_key:
            number = by_key[fu.key]
        else:
            number = writer.create_issue(
                repo, title=fu.title,
                body=_follow_up_body(fu, partial.issue_number),
                milestone=fu.milestone or partial.milestone,
            )
            disposition.follow_ups.append(number)
        disposition.follow_up_keys[fu.key] = number
        by_key[fu.key] = number
        successors.append((number, fu.title))
        after = tuple(
            int(t[1:]) if t.startswith("#") else by_key[t] for t in fu.after
        )
        epic = fu.epic if fu.epic is not None else partial.epic_number
        if epic is not None:
            writer.add_child(repo, epic=epic, child=number, after=after)
            new_nodes.setdefault(epic, []).append(
                WorkOrderNode(issue_number=number, after=after)
            )
    for epic, nodes in new_nodes.items():
        body = writer.get_issue_body(repo, epic)
        updated = _splice_epic_checklists(body or "", nodes)
        writer.set_issue_body(repo, epic, updated)
    return successors


def _find_checklist_bounds(
    lines: Sequence[str], heading_re: re.Pattern[str],
) -> tuple[int, int] | None:
    """``(start, end)`` line-index bounds of the checklist section
    *heading_re* matches in *lines* — the line right after the heading
    through the line right before the next markdown heading (or EOF).
    ``None`` when *heading_re* matches no line. Mirrors
    ``coord.milestone_order``'s own (private) section-boundary rule
    exactly, duplicated here rather than imported since that module
    exposes the splice only as "replace the whole section"
    (:func:`~coord.milestone_order.replace_work_order_section`), which is
    lossy for a hand-written row's prose suffix — see
    :func:`_splice_epic_checklists`.
    """
    start = None
    for i, line in enumerate(lines):
        if heading_re.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("#"):
            end = i
            break
    return start, end


_WORK_ORDER_HEADING_RE = re.compile(r"^#{1,6}\s*Work order\s*$", re.IGNORECASE)
_SUB_ISSUES_HEADING_RE = re.compile(r"^#{1,6}\s*Sub-issues\s*$", re.IGNORECASE)


def _splice_epic_checklists(body: str, nodes: Sequence[WorkOrderNode]) -> str:
    """Append *nodes* to the epic's ``## Work order`` and/or
    ``## Sub-issues`` checklist(s), preserving every existing line
    byte-for-byte (#3945).

    ``coord milestone add-child`` rewrites the WHOLE checklist from its
    parsed :class:`WorkOrder` — and :class:`WorkOrderNode` carries no
    free-text field, so a hand-written `` - [ ] #N — tranche 2 of #101``
    row comes back as bare `` - [ ] #N``, silently dropping the prose.
    This splices the NEW rows in at the end of each section that already
    exists in *body* instead, leaving every byte of the existing section
    untouched, and appends a fresh ``## Work order`` section (the
    pre-#3945 fallback, lossless since there was nothing to preserve) only
    when *body* has neither section at all.

    Skips a node already declared in a given section (idempotent re-run).
    Validates each spliced section with the matching parser before
    returning — a cycle, a duplicate, or an ``after`` edge to something
    undeclared raises :class:`WorkOrderError` rather than landing a
    corrupted checklist.
    """
    lines = body.splitlines()
    # `splitlines()` drops whether *body* ended in a trailing newline;
    # restore it on the way out so a splice is a true byte-for-byte
    # preservation of the rest of the body, not just its lines (#3945
    # review).
    trailing_newline = body.endswith("\n")
    spliced_any = False
    for heading_re, parser in (
        (_WORK_ORDER_HEADING_RE, parse_work_order),
        (_SUB_ISSUES_HEADING_RE, parse_sub_issues),
    ):
        bounds = _find_checklist_bounds(lines, heading_re)
        if bounds is None:
            continue
        spliced_any = True
        start, end = bounds
        existing = parser("\n".join(lines))
        known = set(existing.issue_numbers)
        to_add = [n for n in nodes if n.issue_number not in known]
        if not to_add:
            continue
        # `checkbox=False`: matches the "neither section exists" fallback
        # below and `coord.milestone_order`'s own migration away from the
        # checkbox grammar (#1061) — a splice must not introduce a new
        # `- [ ] #N` row into a section the rest of this body already
        # writes checkbox-free.
        new_lines = render_work_order(
            WorkOrder(nodes=tuple(to_add)), checkbox=False,
        ).splitlines()
        section_end = end
        while section_end > start and not lines[section_end - 1].strip():
            section_end -= 1
        lines = lines[:section_end] + new_lines + lines[section_end:]
        parser("\n".join(lines))  # raises WorkOrderError on a corrupted splice
    if spliced_any:
        result = "\n".join(lines)
        return result + "\n" if trailing_newline else result
    existing = parse_work_order(body)
    merged = WorkOrder(nodes=existing.nodes + tuple(
        n for n in nodes if n.issue_number not in set(existing.issue_numbers)
    ))
    result = replace_work_order_section(body, render_work_order(merged, checkbox=False))
    parse_work_order(result)  # raises WorkOrderError on an undeclared `after` target
    return result


# ── #4038: auto-refile a #3376 branch-merged-guard refusal ──────────────────
#
# A `branch-merged-guard` drive-queue row is an OPEN issue the #3376
# structural dispatch-liveness gate refuses permanently because one of its
# own `issue-{N}-*` branches already merged — the row can never be
# relaunched, since the guard refuses the SAME dispatch again on every
# retry, yet the issue's own remaining work (if any) is not on the default
# branch. This disposition resolves it:
#
# 1. **Plan leg.** :func:`build_refile_plan_leg_prompt` composes a
#    read-only prompt (issue body only — unlike `merged-partial`, no PR
#    ever merged for THIS issue, so there is no review/diff/resolution
#    text to inject); the injected *plan_leg* callable runs it and
#    :func:`parse_refile_plan` reads the JSON verdict out of it.
# 2. **Remainder present** (``remainder_on_default_branch=True``): nothing
#    to refile — close the issue as done.
# 3. **Remainder absent**: file one new issue (same title plus "(refile of
#    #N)", same labels/milestone, body carrying the refile note required
#    by #4038), swap it into the epic's `## Sub-issues`/`## Work order`
#    slot the original occupied (if any), move the ORIGINAL drive-queue
#    row onto the new issue number (same position, same `after=` edges),
#    re-point every queued dependent's `after=` the same way #3984's split
#    repoint does, then comment the pointer on the original — WITHOUT
#    closing it yet.
# 4. **Deferred close.** :func:`coord.board_doctor.
#    run_pending_refile_close_sweep` runs on every sweep independent of
#    this row's own cause (the drive-queue row is gone the moment step 3
#    moves it) and closes the original as superseded the first tick it
#    observes the refile issue itself closed — never left for an operator.
#
# Depth limit: an issue whose body already carries :data:`REFILE_MARKER` is
# itself a refile product. If its own branch later merges into another
# #3376 refusal, this pages a human instead of recursing.

#: Process-wide kill switch, mirroring :data:`NO_FOLLOWTHROUGH_ENV_VAR`.
NO_AUTO_REFILE_ENV_VAR = "COORD_BOARD_DOCTOR_NO_AUTO_REFILE"

#: Hidden marker stamped into every issue this disposition files. Its
#: presence is what trips the depth limit.
REFILE_MARKER = "<!-- coord:refile-of={issue} -->"
_REFILE_MARKER_RE = re.compile(r"<!--\s*coord:refile-of=#?(\d+)\s*-->")

ACTION_REFILE_FILED = "refile-filed"
ACTION_REFILE_ALL_LANDED = "refile-all-landed"
ACTION_REFILE_PLAN_FAILED = "refile-plan-failed"
ACTION_REFILE_WRITE_FAILED = "refile-write-failed"
ACTION_REFILE_DEPTH_PAGED = "refile-depth-paged"
#: Minted only by :func:`coord.board_doctor.run_pending_refile_close_sweep`
#: — the deferred step-4 close, never by :func:`run_refile` itself.
ACTION_REFILE_SUPERSEDED = "refile-superseded-closed"
#: Minted only by :func:`coord.board_doctor.run_refile_sweep`, BEFORE
#: :func:`run_refile` is ever called: a row whose key :func:`pending_refiles`
#: already reports pending is reusing a previous dispatch's own refile, not
#: filing a fresh one. Never recorded to the durable decision log (would
#: overwrite the :data:`ACTION_REFILE_FILED` entry :func:`pending_refiles`
#: itself reads back, making that key invisible to it on the next pass) —
#: this action exists purely for the returned disposition list's own
#: visibility.
ACTION_REFILE_ALREADY_FILED = "refile-already-filed"

#: Marker prefix for the one ``evidence`` string an :data:`ACTION_REFILE_FILED`
#: disposition carries — which real issue number the original was refiled
#: as, so a later, independent sweep pass can find it again without a
#: second, divergent "what did we file" record. Same piggyback-on-``evidence``
#: rationale as :data:`PENDING_FOLLOWTHROUGH_EVIDENCE_PREFIX`'s own docstring.
PENDING_REFILE_CLOSE_EVIDENCE_PREFIX = "pending-refile-close-json="


def auto_refile_enabled(repo_name: str, config: Any) -> bool:
    """Whether the auto-refile disposition may act for *repo_name* (#4038).

    Three independent gates, same shape as :func:`followthrough_enabled`:
    :data:`NO_AUTO_REFILE_ENV_VAR`, the fleet-wide ``board_doctor.
    auto_refile`` switch, and the per-repo ``repos[].board_doctor.
    auto_refile`` opt-in (default off).
    """
    if os.environ.get(NO_AUTO_REFILE_ENV_VAR):
        return False
    if not config.board_doctor.auto_refile:
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.auto_refile)


def is_refile_product(body: str | None) -> bool:
    """True when *body* carries :data:`REFILE_MARKER`."""
    return bool(body and _REFILE_MARKER_RE.search(body))


@dataclass(frozen=True)
class RefileCandidate:
    """Everything :func:`run_refile` needs about one `branch-merged-guard`
    row — both the GitHub-side context for filing the replacement and the
    drive-queue row's own snapshot (``queue_*``) for moving it onto the new
    issue number unchanged (#4038's "same position, same edges").
    """

    repo_name: str
    issue_number: int
    title: str
    body: str
    merged_branch: str = ""
    labels: tuple[str, ...] = ()
    milestone: str | None = None
    epic_number: int | None = None
    github_repo: str = ""
    queue_position: int | None = None
    queue_after: tuple[str, ...] = ()
    queue_machine: str = ""
    queue_hold_after: bool = False
    queue_hold_reason: str = ""
    queue_resume_when: str = ""
    queue_hold_scope: str = "entry"
    queue_max_fix_rounds: int | None = None
    queue_no_acceptance: bool = False
    queue_plan_destructive: bool = False

    @property
    def github_slug(self) -> str:
        """Mirrors :attr:`PartialMerge.github_slug` exactly."""
        return self.github_repo or self.repo_name


class RefilePlanError(ValueError):
    """The refile plan leg's output could not be read as a refile verdict."""


@dataclass(frozen=True)
class RefilePlan:
    """The refile plan leg's verdict: is the remainder this issue describes
    already on the default branch (nothing to refile), or still missing
    (needs a fresh issue)?
    """

    remainder_on_default_branch: bool
    evidence: str = ""


_REFILE_PLAN_SCHEMA = (
    '{"remainder_on_default_branch": true, "evidence": '
    '"<file:line / grep result / test showing the remainder is (or is not) '
    'already on the default branch>"}'
)


def build_refile_plan_leg_prompt(candidate: RefileCandidate) -> str:
    """The read-only plan leg's prompt for *candidate*.

    Unlike :func:`build_plan_leg_prompt` (`merged-partial`), no PR ever
    merged for THIS issue — the #3376 guard refused dispatch before any
    worker ever ran — so there is no review/diff/``ISSUE_RESOLUTION`` text
    to inject; only the issue's own body, which is also where #4038's
    acceptance step ("where the issue lists acceptance tests or files,
    check them") lives.
    """
    branch = candidate.merged_branch or f"issue-{candidate.issue_number}-*"
    sections = [
        "You are a READ-ONLY planner. Do not edit, commit or push anything. "
        f"You are checked out at the default branch HEAD. "
        f"{candidate.repo_name}#{candidate.issue_number}'s own branch "
        f"({branch}) already merged, but a structural dispatch-liveness "
        "guard (#3376) refuses to dispatch this issue again regardless of "
        "what remains — so if any of its described work is still missing, "
        "it needs a FRESH issue number to be driven under.",
        "Check the issue body below against the live code — its own "
        "acceptance criteria and/or `## Files` list, if it names any — and "
        "decide whether the remainder it describes is ALREADY on the "
        "default branch (nothing left to do) or still missing (needs a "
        "fresh issue).",
        "Reply with exactly one JSON object in this shape and nothing "
        "else:\n" + _REFILE_PLAN_SCHEMA,
        f"## Issue #{candidate.issue_number}: {candidate.title}\n\n{candidate.body}",
    ]
    return "\n\n".join(sections)


def parse_refile_plan(text: str) -> RefilePlan:
    """Parse the plan leg's output into a :class:`RefilePlan`.

    Shares :func:`_extract_json`'s fenced-or-bare JSON extraction with
    :func:`parse_plan` — same tolerance for a plan leg that wraps its
    answer in prose or a code fence.
    """
    data = _extract_json(text)
    if not isinstance(data, dict):
        raise RefilePlanError("refile plan must be a JSON object")
    remainder_on_default_branch = data.get("remainder_on_default_branch")
    if not isinstance(remainder_on_default_branch, bool):
        raise RefilePlanError(
            "`remainder_on_default_branch` must be a boolean"
        )
    return RefilePlan(
        remainder_on_default_branch=remainder_on_default_branch,
        evidence=str(data.get("evidence") or ""),
    )


def _refile_note(original_issue: int) -> str:
    """The note #4038 requires at the top of every refiled body."""
    return (
        f"Refiled from #{original_issue} for execution: #{original_issue} "
        f"has a merged `issue-{original_issue}-*` branch, so the #3376 "
        "guard can't dispatch it again."
    )


def _file_refile(candidate: RefileCandidate, writer: FollowThroughWriter) -> int:
    """File the replacement issue and, if the original had an epic, swap it
    into that epic's sub-issue slot. Returns the new issue number.

    The epic swap is a plain text substitution of ``#{old}`` for ``#{new}``
    across the WHOLE epic body (confirmed by :meth:`FollowThroughWriter.
    set_issue_body`'s own read-back) — simpler and safer than re-rendering
    the ``## Work order``/``## Sub-issues`` checklist from a parsed
    :class:`WorkOrder` (:func:`_splice_epic_checklists`'s own rationale for
    why that loses a hand-written row's prose), and sufficient for "add the
    new issue in its place": the old reference is gone, the new one is
    exactly where it was.
    """
    title = f"{candidate.title} (refile of #{candidate.issue_number})"
    body = (
        f"{_refile_note(candidate.issue_number)}\n\n{candidate.body}\n\n"
        + REFILE_MARKER.format(issue=candidate.issue_number)
    )
    new_number = writer.create_issue(
        candidate.github_slug, title=title, body=body,
        milestone=candidate.milestone, labels=candidate.labels,
    )
    if candidate.epic_number is not None:
        writer.add_child(
            candidate.github_slug, epic=candidate.epic_number,
            child=new_number, after=(),
        )
        epic_body = writer.get_issue_body(candidate.github_slug, candidate.epic_number)
        updated = epic_body.replace(
            f"#{candidate.issue_number}", f"#{new_number}",
        )
        if updated != epic_body:
            writer.set_issue_body(candidate.github_slug, candidate.epic_number, updated)
    return new_number


def _requeue_refile(candidate: RefileCandidate, new_number: int) -> None:
    """Move the drive-queue row from *candidate*'s old key onto
    *new_number*, inheriting its exact position and edges, then re-point
    every queued dependent's ``after=`` the same way #3984's split repoint
    does (:func:`coord.decomposition_chat._repoint_queued_dependents_at_split`)
    — without this, a dependent chained on the original would read its
    disappearance from the queue as "pre-req satisfied" and launch before
    the refile has landed.

    Confirms every write with a read-back, same discipline
    :func:`coord.decomposition_chat._repoint_queued_dependents_at_split`
    applies — an unconfirmed rewrite is not a rewrite (#2096). Raises
    (never swallows) on the first row that doesn't read back; the caller
    (:func:`run_refile`) treats that as a write failure to alert on, same
    as every other step in this disposition.
    """
    from coord.drive_queue import entry_key, rewrite_after_for_split  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        dequeue_drive_queue,
        enqueue_drive_queue,
        get_drive_queue_entry,
        list_drive_queue,
    )

    old_key = entry_key(candidate.repo_name, candidate.issue_number)
    new_key = entry_key(candidate.repo_name, new_number)

    dequeue_drive_queue(candidate.repo_name, candidate.issue_number, operator=False)
    enqueue_drive_queue(
        candidate.repo_name, new_number,
        machine=candidate.queue_machine or None,
        after=list(candidate.queue_after),
        position=candidate.queue_position,
        hold_after=candidate.queue_hold_after,
        hold_reason=candidate.queue_hold_reason,
        resume_when=candidate.queue_resume_when,
        hold_scope=candidate.queue_hold_scope,
        max_fix_rounds=candidate.queue_max_fix_rounds,
        no_acceptance=candidate.queue_no_acceptance,
        plan_destructive=candidate.queue_plan_destructive,
    )
    if get_drive_queue_entry(candidate.repo_name, new_number) is None:
        raise RuntimeError(
            f"refiled {old_key} as #{new_number} but the new row did not "
            "read back from the drive queue"
        )

    for row in list_drive_queue():
        current_after = [str(a) for a in (row.get("after_json") or [])]
        if old_key not in current_after:
            continue
        dep_repo = str(row.get("repo_name") or "")
        dep_issue = int(row.get("issue_number") or 0)
        new_after = rewrite_after_for_split(current_after, old_key, [new_key])
        enqueue_drive_queue(
            dep_repo, dep_issue,
            machine=row.get("machine") or None,
            after=new_after,
            hold_after=bool(row.get("hold_after")),
            hold_reason=str(row.get("hold_reason") or ""),
            resume_when=str(row.get("resume_when") or ""),
            hold_scope=str(row.get("hold_scope") or "entry"),
            max_fix_rounds=row.get("max_fix_rounds"),
            no_acceptance=bool(row.get("no_acceptance")),
            plan_destructive=bool(row.get("plan_destructive")),
        )
        confirmed = get_drive_queue_entry(dep_repo, dep_issue)
        confirmed_after = (
            [str(a) for a in (confirmed.get("after_json") or [])]
            if confirmed is not None else None
        )
        if confirmed is None or sorted(confirmed_after) != sorted(new_after):
            raise RuntimeError(
                f"refiled {old_key} as #{new_number} but re-pointing queued "
                f"dependent {dep_repo}#{dep_issue} did not read back the "
                "new after= list"
            )


def run_refile(
    candidate: RefileCandidate,
    *,
    plan_leg: Callable[[str], PlanLegResult],
    writer: FollowThroughWriter,
    alert: Callable[[str, str], None],
    record: Callable[[dict], None] = _default_record,
    dry_run: bool = False,
    requeue: Callable[[RefileCandidate, int], None] = _requeue_refile,
) -> Disposition:
    """Resolve one `branch-merged-guard` row end to end (#4038).

    With *dry_run* the plan leg still runs (it is read-only) and its
    verdict is rendered, but nothing is written, alerted or recorded —
    same contract :func:`run_followthrough`'s own *dry_run* gives.
    """
    key = f"{candidate.repo_name}#{candidate.issue_number}"
    disposition = Disposition(
        repo_name=candidate.repo_name, issue_number=candidate.issue_number,
        action="", dry_run=dry_run, cause="branch-merged-guard",
    )

    def _finish(action: str) -> Disposition:
        disposition.action = action
        if not dry_run:
            record(disposition.as_record())
        return disposition

    if is_refile_product(candidate.body):
        disposition.reasons.append(
            "issue is itself a refile product whose own branch merged "
            "into a #3376 guard refusal again"
        )
        if not dry_run:
            alert(
                f"{key}: refile product's branch guard-refused again",
                f"{key} was filed by the board-doctor auto-refile and its "
                "own branch already merged into another #3376 guard "
                "refusal. Not recursing — a human needs to look.",
            )
        return _finish(ACTION_REFILE_DEPTH_PAGED)

    result = plan_leg(build_refile_plan_leg_prompt(candidate))
    disposition.plan_leg_assignment_id = result.assignment_id
    try:
        plan = parse_refile_plan(result.output)
    except RefilePlanError as exc:
        disposition.reasons.append(str(exc))
        if not dry_run:
            alert(f"{key}: refile plan unreadable", str(exc))
        return _finish(ACTION_REFILE_PLAN_FAILED)

    disposition.rendered_plan = (
        f"remainder_on_default_branch={plan.remainder_on_default_branch}\n"
        f"evidence: {plan.evidence}"
    )

    if dry_run:
        disposition.action = (
            ACTION_REFILE_ALL_LANDED if plan.remainder_on_default_branch
            else ACTION_REFILE_FILED
        )
        return disposition

    if plan.remainder_on_default_branch:
        try:
            writer.close_issue(
                candidate.github_slug, candidate.issue_number,
                comment=(
                    "Board-doctor auto-refile: the remainder this issue "
                    "described is already on the default branch — "
                    f"closing as done. {plan.evidence}"
                ).rstrip(),
            )
        except Exception as exc:  # noqa: BLE001 — every write failure must alert
            disposition.reasons.append(f"close failed: {exc}")
            alert(f"{key}: refile close failed", str(exc))
            return _finish(ACTION_REFILE_WRITE_FAILED)
        return _finish(ACTION_REFILE_ALL_LANDED)

    try:
        new_number = _file_refile(candidate, writer)
        disposition.follow_ups.append(new_number)
        disposition.follow_up_keys["refile"] = new_number
        requeue(candidate, new_number)
        writer.comment_issue(
            candidate.github_slug, candidate.issue_number,
            body=(
                f"Refiled as #{new_number} for execution: the #3376 guard "
                "can't dispatch this issue again now that its "
                f"`issue-{candidate.issue_number}-*` branch has already "
                f"merged. This issue will close once #{new_number} merges."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — every write failure must alert
        disposition.reasons.append(f"write failed: {exc}")
        alert(
            f"{key}: refile write failed",
            f"Writing stopped at: {exc}\nFiled so far: "
            + (", ".join(f"#{n}" for n in disposition.follow_ups) or "none"),
        )
        return _finish(ACTION_REFILE_WRITE_FAILED)

    disposition.reasons.append(
        PENDING_REFILE_CLOSE_EVIDENCE_PREFIX
        + json.dumps({"refile_issue": new_number})
    )
    return _finish(ACTION_REFILE_FILED)


def pending_refiles(decisions: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``key -> refile_issue`` for every :data:`ACTION_REFILE_FILED` row in
    *decisions* not yet superseded (#4038).

    *decisions* is the FULL decision log (oldest first, same order
    :func:`coord.state.board_doctor_decisions` returns), not pre-filtered
    by action — the only way to tell a key's refile is ALREADY superseded
    is to see that a later entry for the same key recorded
    :data:`ACTION_REFILE_SUPERSEDED` (or anything else) instead. Mirrors
    :func:`coord.board_doctor._load_pending_followthrough`'s own
    "latest entry overall, not latest filtered by action" rule exactly,
    for the same reason.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for entry in decisions:
        key = entry.get("key")
        if key:
            latest[str(key)] = entry
    pending: dict[str, int] = {}
    for key, entry in latest.items():
        if entry.get("action") != ACTION_REFILE_FILED:
            continue
        prefix = PENDING_REFILE_CLOSE_EVIDENCE_PREFIX
        for item in reversed(entry.get("evidence") or []):
            if not (isinstance(item, str) and item.startswith(prefix)):
                continue
            try:
                payload = json.loads(item[len(prefix):])
                pending[key] = int(payload["refile_issue"])
            except Exception:  # noqa: BLE001 — a corrupt payload reads as "nothing pending"
                pass
            break
    return pending


# ── Auto-split (#4034) ───────────────────────────────────────────────────────
#
# A row that has given up on the #2972 fix-round ceiling (#3454/#3756) would
# otherwise just page a human (`_alert_fallback`, `coord.board_doctor_policy.
# _investigate`). When its LATEST review's remaining blocking finding(s) all
# describe work that was never built — "not addressed", "no consumer", "not
# implemented", or an `ISSUE_RESOLUTION: partial` naming the gap — rather than
# a defect in what landed, no further fix round can ever close it: the worker
# would just relearn the same missing-scope finding. This disposition is the
# quadraui#2531/#2599 remedy an operator used to perform by hand:
#
# 1. **Classify** (:func:`classify_split_findings`) the remaining blocking
#    findings. Eligible only when EVERY one reads as missing scope; any
#    finding that reads as a defect in delivered code refuses the split
#    outright, and an empty set (nothing blocking) has nothing to classify.
# 2. **File** (:func:`run_auto_split`) a follow-up issue whose body quotes
#    the missing-scope findings verbatim, with a `## Files` section drawn
#    from the paths those findings cite, and swap it into the original's
#    epic's `## Sub-issues` checklist (if any) with `{after: #orig}`.
# 3. **Rescope** the original with a dated note and drop the moved part from
#    its own `## Acceptance`.
# 4. **Reset** the original's review stage and **requeue** it unchanged
#    (same position, same `after=`), with the follow-up queued right behind
#    it — never left for an operator to notice.
#
# Depth limit: an issue whose body already carries :data:`AUTO_SPLIT_MARKER`
# is itself a split product. A missing-scope finding against ONE of those
# pages a human instead of recursing.

#: Process-wide kill switch, mirroring :data:`NO_AUTO_REFILE_ENV_VAR`.
NO_AUTO_SPLIT_ENV_VAR = "COORD_BOARD_DOCTOR_NO_AUTO_SPLIT"

#: Hidden marker stamped into every follow-up this disposition files. Its
#: presence is what trips the depth limit.
AUTO_SPLIT_MARKER = "<!-- coord:auto-split-of={issue} -->"
_AUTO_SPLIT_MARKER_RE = re.compile(r"<!--\s*coord:auto-split-of=#?(\d+)\s*-->")

ACTION_AUTO_SPLIT_FILED = "auto-split-filed"
ACTION_AUTO_SPLIT_WRITE_FAILED = "auto-split-write-failed"
ACTION_AUTO_SPLIT_DEPTH_PAGED = "auto-split-depth-paged"

#: #4034's own `StuckRow`/decision-log `cause` string (`coord.board_doctor`
#: has no `CAUSE_*` constant for this — the row's REAL cause is whatever
#: `queue_diagnose` already diagnosed it as; this is a disposition name,
#: not a classification, the same relationship `cause="branch-merged-guard"`
#: has to :func:`run_refile`'s own candidates).
AUTO_SPLIT_CAUSE = "ceiling-missing-scope"


def auto_split_enabled(repo_name: str, config: Any) -> bool:
    """Whether the auto-split disposition may act for *repo_name* (#4034).

    Three independent gates, same shape as :func:`auto_refile_enabled`:
    :data:`NO_AUTO_SPLIT_ENV_VAR`, the fleet-wide ``board_doctor.auto_split``
    switch, and the per-repo ``repos[].board_doctor.auto_split`` opt-in
    (default off).
    """
    if os.environ.get(NO_AUTO_SPLIT_ENV_VAR):
        return False
    if not config.board_doctor.auto_split:
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.auto_split)


def is_auto_split_product(body: str | None) -> bool:
    """True when *body* carries :data:`AUTO_SPLIT_MARKER`."""
    return bool(body and _AUTO_SPLIT_MARKER_RE.search(body))


#: Phrases a remaining blocking finding uses when it is pointing at scope
#: that was never built, never at a defect in what landed (#4034's own
#: vocabulary, drawn from the quadraui#2531/#2599 findings this disposition
#: was written to resolve without an operator).
_MISSING_SCOPE_RE = re.compile(
    r"not addressed|no consumer|not implemented|nothing\b[^.\n]*\bconsumes\b",
    re.IGNORECASE,
)
#: A reviewer's own `ISSUE_RESOLUTION: partial` naming the remaining gap
#: (#3502's marker) is the same shape as the phrases above — the finding is
#: reporting unbuilt scope, not a defect.
_ISSUE_RESOLUTION_PARTIAL_RE = re.compile(r"ISSUE_RESOLUTION:\s*partial\b", re.IGNORECASE)

#: A top-level markdown bullet starts a new finding — mirrors the rendering
#: every review this disposition classifies already uses (one finding per
#: ``-``/``*`` bullet, optionally continuing onto indented lines).
_TOP_BULLET_RE = re.compile(r"^[-*]\s+", re.MULTILINE)


def is_missing_scope_finding(text: str) -> bool:
    """Whether *text* (one blocking finding, verbatim) reads as unbuilt
    scope rather than a defect in delivered code (#4034 point 1).
    """
    return bool(_MISSING_SCOPE_RE.search(text) or _ISSUE_RESOLUTION_PARTIAL_RE.search(text))


def split_blocking_findings(text: str) -> list[str]:
    """Split a review's "## Blocking findings" text
    (:func:`coord.review.extract_blocking_section`'s own return value) into
    individual findings, one per top-level bullet.

    A body with no top-level bullet at all (free prose, no markdown list)
    is returned as a single finding — the whole text, since there is no
    reliable boundary to split on. Returns ``[]`` for blank/whitespace-only
    input.
    """
    cleaned = text.strip()
    if not cleaned:
        return []
    starts = [m.start() for m in _TOP_BULLET_RE.finditer(cleaned)]
    if not starts:
        return [cleaned]
    starts.append(len(cleaned))
    return [
        cleaned[a:b].strip()
        for a, b in zip(starts, starts[1:])
        if cleaned[a:b].strip()
    ]


@dataclass(frozen=True)
class SplitClassification:
    """:func:`classify_split_findings`'s verdict: may this ceiling row be
    auto-split, and why (or why not)."""

    eligible: bool
    reason: str
    findings: tuple[str, ...] = ()


def classify_split_findings(findings: Sequence[str]) -> SplitClassification:
    """Classify a ceiling-blocked row's remaining blocking findings (#4034
    point 1).

    Eligible only when EVERY finding reads as missing scope
    (:func:`is_missing_scope_finding`) — a single finding that reads as a
    defect in delivered code refuses the split outright (that shape
    belongs to the investigator tier, never a split), and an empty
    *findings* has nothing to classify (a nit-only review belongs to the
    #3835 Finisher tier, not here, and never reaches this far in the first
    place since it never generates further fix rounds).
    """
    cleaned = tuple(f.strip() for f in findings if f and f.strip())
    if not cleaned:
        return SplitClassification(
            eligible=False,
            reason="no blocking findings to classify — nothing to split",
        )
    missing = tuple(f for f in cleaned if is_missing_scope_finding(f))
    if len(missing) != len(cleaned):
        wrong = next(f for f in cleaned if not is_missing_scope_finding(f))
        return SplitClassification(
            eligible=False,
            reason=(
                "at least one remaining blocking finding describes "
                "delivered code rather than missing scope, so this "
                f"belongs to the investigator tier, not a split: {wrong[:200]!r}"
            ),
        )
    return SplitClassification(
        eligible=True,
        reason="every remaining blocking finding describes missing scope",
        findings=missing,
    )


#: A backtick-quoted path (optionally with a trailing `:line` or `:a-b`
#: range, which is dropped) — the shape every lesson/finding in this fleet
#: already cites a file in (see this issue's own "Lessons from siblings").
_FILE_REF_RE = re.compile(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)(?::\d+(?:-\d+)?)?`")


def extract_file_refs(findings: Sequence[str]) -> list[str]:
    """Every distinct backtick-quoted file path cited across *findings*, in
    first-seen order — the follow-up's own ``## Files`` declaration
    (#4034 point 2)."""
    seen: list[str] = []
    for finding in findings:
        for m in _FILE_REF_RE.finditer(finding):
            path = m.group(1)
            if path not in seen:
                seen.append(path)
    return seen


@dataclass(frozen=True)
class AutoSplitCandidate:
    """Everything :func:`run_auto_split` needs about one ceiling-blocked row
    (#4034) — both the GitHub-side context for filing the follow-up and the
    drive-queue row's own snapshot for re-adding the ORIGINAL unchanged
    (same position, same ``after=`` edges — unlike a refile, the original's
    own issue number never moves)."""

    repo_name: str
    issue_number: int
    title: str
    body: str
    findings: tuple[str, ...]
    labels: tuple[str, ...] = ()
    milestone: str | None = None
    epic_number: int | None = None
    github_repo: str = ""
    queue_position: int | None = None
    queue_after: tuple[str, ...] = ()
    queue_machine: str = ""
    queue_hold_after: bool = False
    queue_hold_reason: str = ""
    queue_resume_when: str = ""
    queue_hold_scope: str = "entry"
    queue_max_fix_rounds: int | None = None
    queue_no_acceptance: bool = False
    queue_plan_destructive: bool = False

    @property
    def github_slug(self) -> str:
        """Mirrors :attr:`RefileCandidate.github_slug` exactly."""
        return self.github_repo or self.repo_name


def _auto_split_follow_up_body(candidate: AutoSplitCandidate) -> tuple[str, list[str]]:
    """Render the follow-up's body: its "What" and "Acceptance" are the
    quoted missing-scope findings (#4034 point 2 — "its 'What' comes from
    the blocking findings, quoted" / "its acceptance is the original
    acceptance items those findings say are unmet"). Lacking a reliable way
    to tell WHICH of the original's own acceptance bullets each finding
    means, the findings themselves stand in as the acceptance text too —
    never less coverage than the findings already describe. Returns the
    rendered body and the file paths extracted from the findings, which the
    caller also needs to rescope the original's own ``## Acceptance``.
    """
    files = extract_file_refs(candidate.findings)
    findings_block = "\n\n".join(candidate.findings)
    parts = [
        f"Split from #{candidate.issue_number} by the board doctor (#4034): "
        "its last review's remaining blocking finding(s), quoted below, "
        "name scope that was never built — not a defect in the delivered "
        "code.",
        "## What\n\n" + findings_block,
        "## Acceptance\n\n" + findings_block,
    ]
    if files:
        parts.append("## Files\n\n" + "\n".join(f"- {f}" for f in files))
    parts.append(AUTO_SPLIT_MARKER.format(issue=candidate.issue_number))
    return "\n\n".join(parts), files


def _file_auto_split_follow_up(
    candidate: AutoSplitCandidate, writer: FollowThroughWriter,
) -> tuple[int, list[str]]:
    """File the follow-up and, if the original had an epic, add it to that
    epic's ``## Sub-issues`` checklist as ``{after: #orig}``. Returns the
    new issue number and the file paths its body cites.
    """
    title = f"{candidate.title} (split from #{candidate.issue_number})"
    body, files = _auto_split_follow_up_body(candidate)
    new_number = writer.create_issue(
        candidate.github_slug, title=title, body=body,
        milestone=candidate.milestone, labels=candidate.labels,
    )
    if candidate.epic_number is not None:
        writer.add_child(
            candidate.github_slug, epic=candidate.epic_number,
            child=new_number, after=(candidate.issue_number,),
        )
        epic_body = writer.get_issue_body(candidate.github_slug, candidate.epic_number)
        updated = _splice_epic_checklists(
            epic_body or "",
            (WorkOrderNode(issue_number=new_number, after=(candidate.issue_number,)),),
        )
        if updated != epic_body:
            writer.set_issue_body(candidate.github_slug, candidate.epic_number, updated)
    return new_number, files


#: A `## Acceptance` (or `## Acceptance criteria`) heading, same leniency a
#: review's own section parser gives its headings.
_ACCEPTANCE_HEADING_RE = re.compile(
    r"^#{1,6}[ \t]*Acceptance(?:\s+criteria)?\b.*$", re.IGNORECASE | re.MULTILINE,
)
_ANY_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S", re.MULTILINE)


def _rescope_original_body(
    body: str, *, new_number: int, findings: Sequence[str], moved_files: Sequence[str],
) -> str:
    """Rescope the original's body for #4034 point 3: a dated note at the
    top naming where the gap moved, and the moved part dropped from its own
    ``## Acceptance`` (a bullet line is dropped only when it names one of
    *moved_files* — the same mechanical, never-guessed matching #2247's own
    declared-file overlap uses, so a bullet this can't confidently tie to
    the moved part is left in place rather than risking a silent loss of
    coverage).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    gap = findings[0].strip() if findings else "scope named by the last review"
    gap = gap[:160] + ("…" if len(gap) > 160 else "")
    note = f"**Rescoped {today}:** {gap} — moved to #{new_number}.\n\n"

    m = _ACCEPTANCE_HEADING_RE.search(body)
    if m is None or not moved_files:
        return note + body
    start = m.end()
    nxt = _ANY_HEADING_RE.search(body, start)
    end = nxt.start() if nxt else len(body)
    section = body[start:end]
    lines = section.splitlines(keepends=True)
    kept = [line for line in lines if not any(f in line for f in moved_files)]
    if len(kept) == len(lines):
        return note + body
    return note + body[:start] + "".join(kept) + body[end:]


def _requeue_auto_split(candidate: AutoSplitCandidate, new_number: int) -> None:
    """Re-add the ORIGINAL row unchanged (same position, same ``after=``,
    no overlap re-prediction — its own declared footprint has not
    changed), then queue the follow-up right behind it (#4034 points 5-6).

    Confirms every write with a read-back, same discipline
    :func:`_requeue_refile` applies — an unconfirmed rewrite is not a
    rewrite (#2096).
    """
    from coord.drive_queue import entry_key  # noqa: PLC0415
    from coord.state import dequeue_drive_queue, enqueue_drive_queue, get_drive_queue_entry  # noqa: PLC0415

    orig_key = entry_key(candidate.repo_name, candidate.issue_number)

    dequeue_drive_queue(candidate.repo_name, candidate.issue_number, operator=False)
    enqueue_drive_queue(
        candidate.repo_name, candidate.issue_number,
        machine=candidate.queue_machine or None,
        after=list(candidate.queue_after),
        position=candidate.queue_position,
        hold_after=candidate.queue_hold_after,
        hold_reason=candidate.queue_hold_reason,
        resume_when=candidate.queue_resume_when,
        hold_scope=candidate.queue_hold_scope,
        max_fix_rounds=candidate.queue_max_fix_rounds,
        no_acceptance=candidate.queue_no_acceptance,
        plan_destructive=candidate.queue_plan_destructive,
    )
    if get_drive_queue_entry(candidate.repo_name, candidate.issue_number) is None:
        raise RuntimeError(
            f"auto-split re-queued {orig_key} but the row did not read "
            "back from the drive queue"
        )

    enqueue_drive_queue(candidate.repo_name, new_number, after=[orig_key])
    if get_drive_queue_entry(candidate.repo_name, new_number) is None:
        raise RuntimeError(
            f"auto-split filed #{new_number} but it did not read back "
            "from the drive queue"
        )


def _default_reset_review(repo_name: str, issue_number: int) -> None:
    """No-op placeholder — a production caller (`coord.board_doctor`) always
    passes its own *reset_review*, threaded with the live `board`/`config`
    :func:`coord.diagnose.diagnose_stage` needs, which this module does not
    carry. Kept so :func:`run_auto_split` has a harmless default for a
    caller (mostly tests) that doesn't care about the review stage reset.
    """


def run_auto_split(
    candidate: AutoSplitCandidate,
    *,
    writer: FollowThroughWriter,
    alert: Callable[[str, str], None],
    record: Callable[[dict], None] = _default_record,
    dry_run: bool = False,
    requeue: Callable[[AutoSplitCandidate, int], None] = _requeue_auto_split,
    reset_review: Callable[[str, int], None] = _default_reset_review,
) -> Disposition:
    """Resolve one ceiling-blocked, missing-scope-only row end to end
    (#4034). *candidate* is assumed already classified eligible
    (:func:`classify_split_findings`) — this function only guards the depth
    limit, files, rescopes, resets and requeues.

    With *dry_run*, nothing is written, alerted or recorded, mirroring
    :func:`run_refile`'s own contract.
    """
    key = f"{candidate.repo_name}#{candidate.issue_number}"
    disposition = Disposition(
        repo_name=candidate.repo_name, issue_number=candidate.issue_number,
        action="", dry_run=dry_run, cause=AUTO_SPLIT_CAUSE,
    )

    def _finish(action: str) -> Disposition:
        disposition.action = action
        if not dry_run:
            record(disposition.as_record())
        return disposition

    if is_auto_split_product(candidate.body):
        disposition.reasons.append(
            "issue is itself an auto-split product whose own split-off part "
            "hit a missing-scope finding again"
        )
        if not dry_run:
            alert(
                f"{key}: auto-split product hit a missing-scope finding again",
                f"{key} was filed by the board-doctor auto-split and its "
                "own remaining blocking finding(s) still read as missing "
                "scope. Not recursing — a human needs to look.",
            )
        return _finish(ACTION_AUTO_SPLIT_DEPTH_PAGED)

    if dry_run:
        disposition.action = ACTION_AUTO_SPLIT_FILED
        return disposition

    new_number: int | None = None
    try:
        new_number, moved_files = _file_auto_split_follow_up(candidate, writer)
        disposition.follow_ups.append(new_number)
        disposition.follow_up_keys["auto_split"] = new_number
        rescoped = _rescope_original_body(
            candidate.body, new_number=new_number,
            findings=candidate.findings, moved_files=moved_files,
        )
        writer.set_issue_body(candidate.github_slug, candidate.issue_number, rescoped)
        reset_review(candidate.repo_name, candidate.issue_number)
        requeue(candidate, new_number)
        writer.comment_issue(
            candidate.github_slug, candidate.issue_number,
            body=(
                f"Board-doctor auto-split (#4034): the remaining blocking "
                "review finding(s) named scope that was never built, not "
                f"a defect in what landed. Split out as #{new_number} and "
                "requeued right behind it."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — every write failure must alert
        disposition.reasons.append(f"write failed: {exc}")
        alert(
            f"{key}: auto-split write failed",
            f"Writing stopped at: {exc}\nFiled so far: "
            + (f"#{new_number}" if new_number is not None else "none"),
        )
        return _finish(ACTION_AUTO_SPLIT_WRITE_FAILED)

    return _finish(ACTION_AUTO_SPLIT_FILED)
