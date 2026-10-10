"""Epic/milestone lesson carry-forward (#3676).

In a port-style epic every child tends to trip over the SAME reviewer rules —
quadraui#1340's children (#1343, #1344, #1346, #1347) each repeated the same
two blocking findings (a missing targeted driver test; issue-number comments),
and each repeat cost a $3-8 fix round. The first sibling's request-changes
review already said exactly what the rest would get wrong.

So when a child of an epic or milestone gets a request-changes review, its
blocking findings are split into individual bullets, deduplicated, and stored
on every **not-yet-dispatched** sibling as ``issue_context`` rows with
``source=SIBLING_LESSON_SOURCE`` (:func:`record_sibling_lessons`, the write
side, called from the same review-verdict seam that records the #603
per-issue context entry — :func:`coord.issue_store._post_result_local`).
The work-dispatch briefing (:func:`coord.dispatch.dispatch`) then renders
them as a "Lessons from siblings" section (:func:`sibling_lessons_block`, the
read side).

Storing them as ``issue_context`` rows — rather than in a new table — means
the read side is the existing daemon-routed ``list_issue_context`` (works on a
thin client unchanged), ``coord context show`` shows them, and they are
dropped with the rest of the issue's context when it closes. They are kept OUT
of the per-issue digest (:func:`coord.state.render_issue_context` filters the
source) so they never eat its entry budget.

Siblings are:

* every other OPEN issue in the same GitHub milestone (``issues.
  milestone_number`` in the local cache), and
* every other child listed in the ``## Sub-issues`` block of a cached epic
  whose checklist names this issue (:func:`coord.milestone_order.
  parse_sub_issues`, the same grammar ``coord milestone add-child`` writes).

Tracking/epic issues themselves (a body with a non-empty ``## Sub-issues`` or
``## Work order`` block) are never siblings — they are never dispatched as
work. "Not yet dispatched" means zero work-like legs on the issue so far
(:func:`coord.state._work_leg_count_for_issue_local`, the same count the
drive queue's fix budget uses): an in-flight sibling already has its own
briefing and its own reviews.

Everything here is best-effort: a lesson that fails to record costs at worst
the fix round this feature exists to save, never a review verdict or a
dispatch.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from coord.state import SIBLING_LESSON_SOURCE

log = logging.getLogger(__name__)

__all__ = [
    "SIBLING_LESSON_SOURCE",
    "find_undispatched_siblings",
    "record_sibling_lessons",
    "render_sibling_lessons",
    "sibling_lessons_block",
    "split_findings",
]

# A single carried-over finding is a reminder, not the full review — the
# sibling's own reviewer will cite the rule again if it is violated. Long
# findings are cut so one verbose review can't swamp every sibling briefing.
MAX_LESSON_CHARS = 800
# Read-side caps: the newest lessons win once a busy epic has accumulated
# more than this many distinct findings.
MAX_RENDERED_LESSONS = 15
MAX_RENDERED_CHARS = 6000

_BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_FROM_PREFIX_RE = re.compile(r"^\(from #\d+\)\s*")
_NORMALIZE_STRIP_RE = re.compile(r"[*_`~>]")
_WHITESPACE_RE = re.compile(r"\s+")


# ── pure helpers ─────────────────────────────────────────────────────────────


def split_findings(blocking: str) -> list[str]:
    """Split a ``## Blocking findings`` section into individual findings.

    A top-level bullet (``- ``, ``* ``, ``+ ``, ``1. ``) at column 0 starts a
    new finding; indented lines and non-bullet lines continue the current
    one. A section with no top-level bullets at all is one finding. Explicit
    "None." style markers never reach here (:func:`coord.review.
    extract_blocking_section` already returns "" for them).
    """
    findings: list[list[str]] = []
    for raw in (blocking or "").splitlines():
        if not raw.strip():
            continue
        if not raw[:1].isspace() and _BULLET_RE.match(raw):
            findings.append([_BULLET_RE.sub("", raw, count=1).strip()])
        elif findings:
            findings[-1].append(raw.strip())
        else:
            findings.append([raw.strip()])
    out: list[str] = []
    for parts in findings:
        text = " ".join(p for p in parts if p).strip()
        if not text:
            continue
        if len(text) > MAX_LESSON_CHARS:
            text = text[:MAX_LESSON_CHARS].rstrip() + "…"
        out.append(text)
    return out


def _normalize(finding: str) -> str:
    """Dedup key for a finding: case-, whitespace- and markdown-insensitive,
    and blind to the ``(from #N)`` provenance prefix stored on the row."""
    text = _FROM_PREFIX_RE.sub("", (finding or "").strip())
    text = _NORMALIZE_STRIP_RE.sub("", text).lower()
    return _WHITESPACE_RE.sub(" ", text).strip(" .;:")


def _lesson_body(source_issue: int, finding: str) -> str:
    return f"(from #{source_issue}) {finding}"


def render_sibling_lessons(entries: Iterable[dict]) -> str:
    """Render ``SIBLING_LESSON_SOURCE`` context rows as the briefing section.

    Pure. Returns "" when there is nothing to render. Deduplicates again on
    read (two writers racing past each other's dedup check must still never
    show a worker the same lesson twice), keeps the newest
    ``MAX_RENDERED_LESSONS`` and renders them oldest-first.
    """
    lessons = sorted(
        (e for e in entries if e.get("source") == SIBLING_LESSON_SOURCE),
        key=lambda e: e.get("created_at") or 0,
        reverse=True,
    )
    seen: set[str] = set()
    picked: list[str] = []
    for e in lessons:
        body = (e.get("body") or "").strip()
        key = _normalize(body)
        if not body or not key or key in seen:
            continue
        seen.add(key)
        picked.append(body)
        if len(picked) >= MAX_RENDERED_LESSONS:
            break
    if not picked:
        return ""
    lines: list[str] = []
    used = 0
    for body in reversed(picked):
        line = f"- {body}"
        if used + len(line) + 1 > MAX_RENDERED_CHARS and lines:
            break
        lines.append(line)
        used += len(line) + 1
    return (
        "\n\n## Lessons from siblings\n\n"
        "A reviewer already requested changes on other issues in this same "
        "epic/milestone for the blocking findings below — each one cost that "
        "sibling a paid fix round. They are rules this repo's reviewer "
        "enforces, not tasks for this issue: before you declare done, check "
        "your own diff against every item and don't repeat them.\n\n"
        + "\n".join(lines)
        + "\n"
    )


# ── read side ────────────────────────────────────────────────────────────────


def sibling_lessons_block(repo_name: str, issue_number: int) -> str:
    """The "Lessons from siblings" briefing section for *issue_number*, or "".

    Rides the dispatch hot path, so it is FULLY fail-soft like
    :func:`coord.state.issue_context_block`: any failure reads as "no
    lessons" and never breaks a dispatch. Routed through the daemon on a thin
    client by :func:`coord.state.list_issue_context`.
    """
    try:
        from coord.state import list_issue_context  # noqa: PLC0415

        return render_sibling_lessons(list_issue_context(repo_name, issue_number))
    except Exception:  # noqa: BLE001 — never let a lessons read break dispatch
        log.debug("sibling lessons read failed for %s#%s", repo_name, issue_number,
                  exc_info=True)
        return ""


# ── write side (local DB — daemon-side, like the #603 review context write) ──


def _is_tracking_body(body: str) -> bool:
    from coord.milestone_order import parse_sub_issues, parse_work_order  # noqa: PLC0415

    for parse in (parse_sub_issues, parse_work_order):
        try:
            if parse(body or "").nodes:
                return True
        except Exception:  # noqa: BLE001 — a malformed block still marks a tracker
            if body and re.search(r"^#{1,6}\s*(Sub-issues|Work order)\s*$", body,
                                  re.IGNORECASE | re.MULTILINE):
                return True
    return False


def _epic_children_naming(conn, repo_name: str, issue_number: int) -> set[int]:
    """Children of every cached epic in *repo_name* whose ``## Sub-issues``
    checklist names *issue_number*."""
    from coord import sql  # noqa: PLC0415
    from coord.milestone_order import parse_sub_issues  # noqa: PLC0415

    rows = sql.execute(
        conn,
        "SELECT number, body FROM issues WHERE repo_name = ? AND body LIKE ?",
        (repo_name, "%Sub-issues%"),
    ).fetchall()
    children: set[int] = set()
    for r in rows:
        try:
            order = parse_sub_issues(r["body"] or "")
        except Exception:  # noqa: BLE001 — an unparseable epic contributes nothing
            continue
        numbers = set(order.issue_numbers)
        if issue_number in numbers:
            children |= numbers
    return children


def find_undispatched_siblings(repo_name: str, issue_number: int) -> list[int]:
    """Open, never-dispatched siblings of *issue_number* (see module docstring).

    Local-DB read — call it where the canonical board lives (the daemon, or
    a no-daemon install), which is where the review-verdict write that
    triggers it already runs.
    """
    from coord import sql  # noqa: PLC0415
    from coord.state import _work_leg_count_for_issue_local, get_connection  # noqa: PLC0415

    conn = get_connection()
    candidates: set[int] = set()
    row = sql.execute(
        conn,
        "SELECT milestone_number FROM issues WHERE repo_name = ? AND number = ?",
        (repo_name, issue_number),
    ).fetchone()
    milestone = row["milestone_number"] if row is not None else None
    if milestone is not None:
        for r in sql.execute(
            conn,
            "SELECT number FROM issues WHERE repo_name = ? AND milestone_number = ?",
            (repo_name, milestone),
        ).fetchall():
            candidates.add(int(r["number"]))
    candidates |= _epic_children_naming(conn, repo_name, issue_number)
    candidates.discard(issue_number)
    if not candidates:
        return []

    siblings: list[int] = []
    for number in sorted(candidates):
        r = sql.execute(
            conn,
            "SELECT state, body FROM issues WHERE repo_name = ? AND number = ?",
            (repo_name, number),
        ).fetchone()
        # An epic child listed but not (yet) in the cache is still a sibling:
        # the checklist is the authority, and a never-synced issue is about
        # as undispatched as an issue gets.
        if r is not None and (r["state"] or "open") != "open":
            continue
        if r is not None and _is_tracking_body(r["body"] or ""):
            continue
        if _work_leg_count_for_issue_local(conn, repo_name, number) > 0:
            continue
        siblings.append(number)
    return siblings


def record_sibling_lessons(
    repo_name: str, issue_number: int, blocking: str,
) -> dict[int, int]:
    """Carry *issue_number*'s blocking findings to its undispatched siblings.

    *blocking* is the review's ``## Blocking findings`` section
    (:func:`coord.review.extract_blocking_section`). Each finding is added to
    each sibling at most once — a finding a sibling already carries (from
    this issue or any other) is skipped, so re-recording the same review, or
    two siblings tripping the same rule, never duplicates a lesson.

    Returns ``{sibling_issue: lessons_added}`` for siblings that gained at
    least one lesson. Local-DB writer; best-effort per sibling.
    """
    findings = split_findings(blocking)
    if not findings:
        return {}
    from coord.state import _add_issue_context_entry_local, _list_issue_context_local  # noqa: PLC0415

    added: dict[int, int] = {}
    for sibling in find_undispatched_siblings(repo_name, issue_number):
        try:
            existing = {
                _normalize(e.get("body") or "")
                for e in _list_issue_context_local(repo_name, sibling)
                if e.get("source") == SIBLING_LESSON_SOURCE
            }
            count = 0
            for finding in findings:
                key = _normalize(finding)
                if not key or key in existing:
                    continue
                _add_issue_context_entry_local(
                    repo_name,
                    sibling,
                    _lesson_body(issue_number, finding),
                    source=SIBLING_LESSON_SOURCE,
                )
                existing.add(key)
                count += 1
            if count:
                added[sibling] = count
        except Exception:  # noqa: BLE001 — one sibling must not sink the rest
            log.warning(
                "sibling lessons: failed to record %s#%s's findings on #%s",
                repo_name, issue_number, sibling, exc_info=True,
            )
    if added:
        log.info(
            "sibling lessons: carried %s#%s's blocking findings to %s",
            repo_name, issue_number,
            ", ".join(f"#{n} (+{c})" for n, c in sorted(added.items())),
        )
    return added
