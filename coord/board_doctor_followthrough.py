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
from dataclasses import dataclass, field
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

_ISSUE_REF_RE = re.compile(r"^#?(\d+)$")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


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


@dataclass(frozen=True)
class FollowThroughPlan:
    items: tuple[RemainingItem, ...] = ()
    follow_ups: tuple[FollowUp, ...] = ()

    @property
    def all_landed(self) -> bool:
        return not any(i.verified_still_missing for i in self.items)


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
    #: Plan key → filed issue number, for every follow-up created so far.
    follow_up_keys: dict[str, int] = field(default_factory=dict)
    #: Filed issue number → pinned machine ("" = left to `coord drive`) for
    #: every follow-up the auto-queue step enqueued.
    queued: dict[int, str] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return {
            "queue": "drive-queue",
            "key": f"{self.repo_name}#{self.issue_number}",
            "cause": "merged-partial",
            "action": self.action,
            "dry_run": self.dry_run,
            "plan_leg_assignment_id": self.plan_leg_assignment_id,
            "follow_ups": list(self.follow_ups),
            "queued": {str(k): v for k, v in self.queued.items()},
            "reasons": list(self.reasons),
        }


class FollowThroughWriter(Protocol):
    """The GitHub-side writes this disposition performs."""

    def create_issue(
        self, repo_name: str, *, title: str, body: str, milestone: str | None,
    ) -> int: ...

    def add_child(
        self, repo_name: str, *, epic: int, child: int, after: Sequence[int],
    ) -> None: ...

    def get_issue_body(self, repo_name: str, number: int) -> str: ...

    def set_issue_body(self, repo_name: str, number: int, body: str) -> None: ...

    def close_issue(self, repo_name: str, number: int, *, comment: str) -> None: ...


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
    """

    def create_issue(
        self, repo_name: str, *, title: str, body: str, milestone: str | None,
    ) -> int:
        from coord import github_ops  # noqa: PLC0415

        result = github_ops.create_issue(repo_name, title, body, milestone=milestone)
        return int(result["number"])

    def add_child(
        self, repo_name: str, *, epic: int, child: int, after: Sequence[int],
    ) -> None:
        from coord import github_ops  # noqa: PLC0415

        github_ops.add_sub_issue(repo_name, parent_number=epic, child_number=child)
        parent = github_ops.get_issue_parent(repo_name, child)
        linked = parent.get("number") if parent else None
        if linked != epic:
            raise RuntimeError(
                f"add_sub_issue({repo_name}, parent={epic}, child={child}) "
                f"returned without error but {repo_name}#{child}'s parent "
                f"now reads {linked!r}, not {epic}"
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
     "capability": "<or null>", "machine": "<or null>", "files": ["path", ...]}
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
        "\"#N\") or the key of an EARLIER follow-up in your list.",
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
        ))
    return FollowThroughPlan(items=tuple(items), follow_ups=tuple(follow_ups))


# ── Validation ───────────────────────────────────────────────────────────────




def validate_plan(
    plan: FollowThroughPlan,
    *,
    partial: PartialMerge,
    cap: int = DEFAULT_FOLLOWUP_CAP,
    backends: Sequence[str] = (),
    epic_exists: Callable[[int], bool] = lambda _n: True,
    milestone_exists: Callable[[str], bool] = lambda _m: True,
) -> list[str]:
    """Every reason *plan* must not be written; empty means it is valid."""
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
    dry_run: bool = False,
    auto_queue: AutoQueueContext | None = None,
) -> Disposition:
    """Resolve one ``merged-partial`` row end to end.

    *alert* and *page* take ``(subject, body)``. With *dry_run* the plan leg
    still runs (it is read-only) and the plan is validated and rendered,
    but nothing is written, alerted, paged or recorded.

    *auto_queue* (see :func:`build_auto_queue_context`) additionally
    drive-queues each filed follow-up once the original is closed; ``None``
    files without queueing.
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

    result = plan_leg(build_plan_leg_prompt(partial))
    disposition.plan_leg_assignment_id = result.assignment_id
    try:
        plan = parse_plan(result.output)
    except PlanError as exc:
        disposition.reasons.append(str(exc))
        if not dry_run:
            alert(f"{key}: follow-through plan unreadable", str(exc))
        return _finish(ACTION_PLAN_FAILED)

    disposition.rendered_plan = render_plan(plan)
    reasons = validate_plan(
        plan, partial=partial, cap=cap, backends=backends,
        epic_exists=epic_exists, milestone_exists=milestone_exists,
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
        successors = _write_follow_ups(partial, plan, writer, disposition)
        writer.close_issue(
            partial.github_slug, partial.issue_number,
            comment=_close_comment(plan, successors),
        )
    except Exception as exc:  # noqa: BLE001 — every write failure must alert
        disposition.reasons.append(f"write failed: {exc}")
        alert(
            f"{key}: follow-through write failed",
            f"Writing stopped at: {exc}\nFollow-ups already filed: "
            + (", ".join(f"#{n}" for n in disposition.follow_ups) or "none"),
        )
        return _finish(ACTION_WRITE_FAILED)
    if auto_queue is not None and disposition.follow_ups:
        try:
            auto_queue_follow_ups(partial, plan, disposition, auto_queue, alert=alert)
        except Exception as exc:  # noqa: BLE001 — the follow-ups are filed; alert, don't fail
            disposition.reasons.append(f"auto-queue failed: {exc}")
            alert(f"{key}: follow-up auto-queue failed", str(exc))
    return _finish(ACTION_ALL_LANDED if plan.all_landed else ACTION_FILED)


def _write_follow_ups(
    partial: PartialMerge,
    plan: FollowThroughPlan,
    writer: FollowThroughWriter,
    disposition: Disposition,
) -> list[tuple[int, str]]:
    """Create, link and order every follow-up; returns ``(number, title)``s."""
    if plan.all_landed:
        return []
    by_key: dict[str, int] = {}
    successors: list[tuple[int, str]] = []
    new_nodes: dict[int, list[WorkOrderNode]] = {}
    repo = partial.github_slug
    for fu in plan.follow_ups:
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
    return replace_work_order_section(body, render_work_order(merged, checkbox=False))
