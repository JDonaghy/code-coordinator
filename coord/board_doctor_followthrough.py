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
    """Everything the plan leg and the validator need about one row."""

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

    def as_record(self) -> dict[str, Any]:
        return {
            "queue": "drive-queue",
            "key": f"{self.repo_name}#{self.issue_number}",
            "cause": "merged-partial",
            "action": self.action,
            "dry_run": self.dry_run,
            "plan_leg_assignment_id": self.plan_leg_assignment_id,
            "follow_ups": list(self.follow_ups),
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
) -> Disposition:
    """Resolve one ``merged-partial`` row end to end.

    *alert* and *page* take ``(subject, body)``. With *dry_run* the plan leg
    still runs (it is read-only) and the plan is validated and rendered,
    but nothing is written, alerted, paged or recorded.
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
            partial.repo_name, partial.issue_number,
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
    for fu in plan.follow_ups:
        number = writer.create_issue(
            partial.repo_name, title=fu.title,
            body=_follow_up_body(fu, partial.issue_number),
            milestone=fu.milestone or partial.milestone,
        )
        disposition.follow_ups.append(number)
        by_key[fu.key] = number
        successors.append((number, fu.title))
        after = tuple(
            int(t[1:]) if t.startswith("#") else by_key[t] for t in fu.after
        )
        epic = fu.epic if fu.epic is not None else partial.epic_number
        if epic is not None:
            writer.add_child(partial.repo_name, epic=epic, child=number, after=after)
            new_nodes.setdefault(epic, []).append(
                WorkOrderNode(issue_number=number, after=after)
            )
    for epic, nodes in new_nodes.items():
        body = writer.get_issue_body(partial.repo_name, epic)
        # Raises on a malformed block so a write failure alerts rather than
        # overwriting rows this module could not read.
        existing = parse_work_order(body or "")
        known = set(existing.issue_numbers)
        merged = WorkOrder(nodes=existing.nodes + tuple(
            n for n in nodes if n.issue_number not in known
        ))
        writer.set_issue_body(
            partial.repo_name, epic,
            replace_work_order_section(body, render_work_order(merged, checkbox=False)),
        )
    return successors
