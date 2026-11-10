"""Cross-repo gaps auto-filed from a ``BLOCKED_ON_UPSTREAM`` marker (#3676).

When a worker in one repo (say vimcode) hits a capability missing from a repo
it consumes (say quadraui), the old convention was to draft the upstream
issue as prose in a tracking doc (vimcode's ``docs/PENDING_QUADRAUI_ISSUES.md``
grew to 3,200 lines / 45 entries) and have a human file it later — and
reviewers came to REQUIRE that draft, so every gap cost a fix round of prose
plus a manual filing.

Now the worker emits a structured marker in its final message instead::

    BLOCKED_ON_UPSTREAM: quadraui: Expose per-pane scroll offsets
    vimcode's minimap needs the f32 scroll offset of a pane; today only the
    integer line index is public.

and the coordinator (:func:`process_upstream_gaps`, called from
:func:`coord.notify.post_transition` when a work-like leg finishes):

1. files the upstream issue through the issue-tracker seam
   (:func:`coord.state.create_issue` — daemon-routed, never a ``gh``
   shell-out of its own),
2. links the two: a pinned ``issue_context`` entry on the downstream issue
   (so every later worker/reviewer on it reads "blocked on quadraui#N" first)
   plus a comment on the downstream issue, and
3. blocks the downstream drive-queue row on it by adding an ``after=`` edge
   (the existing drive-queue ``after=`` mechanism, cross-repo keys and all —
   an open, unqueued upstream issue defers the dependent with "waiting on
   quadraui#N (open, not queued)", and its landing satisfies the edge).
   The row is usually ``running`` at this point (the work leg just ended
   mid-drive), and the tick reads ``after=`` only for ``waiting`` rows, so an
   in-flight row is taken out of flight first — its drive stopped and the
   row returned to ``waiting`` with the edge (see :func:`_block_queue_row`).

Parsing is tolerant (:func:`parse_upstream_markers`): several markers per
message, markdown decoration around the marker (bold, backticks, list
bullets, block quotes, code fences). Filing is idempotent: each gap gets a
deterministic key from (upstream repo, normalised title), stored in the
pinned context entry, and a gap whose key is already recorded on the
downstream issue is never filed again — re-reading the same final message
(a replayed transition, a manual re-run) files nothing new.

Before filing, :func:`_verify_gap` checks the worker's claim against the
upstream repo itself (#3930): the quadraui#2256 incident filed and chained a
dependency for primitives (``RowAlign``, ``FormSpacing::padding``) that
already existed on quadraui's ``develop`` — three work legs were then spent
confirming the gap wasn't real before anyone looked upstream. The check is
cheap and mechanical, never a judgement call: every inline-code symbol
(`` `Foo::bar` ``) named in the marker's title/body is ``git grep``'d against
the upstream repo's own fetched ``default_branch`` (see :func:`_verify_symbols`
— no local checkout step beyond the fetch; ``git grep <ref>`` reads the tree
object directly). If every named symbol is already there, nothing is filed,
no ``after=`` edge is added, and a comment naming the symbols and where they
were found goes on the downstream issue instead. A marker naming no checkable
symbols, or one the check itself couldn't run (no local checkout, a dead
fetch), still files as before — a tool failure must never silently swallow a
real gap — but the filed issue carries a note that it was not verified.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from coord.config import Config
    from coord.models import Repo

log = logging.getLogger(__name__)

__all__ = [
    "UPSTREAM_GAP_MARKER",
    "UPSTREAM_GAP_SOURCE",
    "UpstreamGap",
    "UpstreamGapOutcome",
    "final_message_from_log_text",
    "gap_key",
    "parse_upstream_markers",
    "process_upstream_gaps",
    "queue_prereq",
]

UPSTREAM_GAP_MARKER = "BLOCKED_ON_UPSTREAM"
# `issue_context.source` of the pinned "blocked on upstream" entry — also the
# idempotency ledger (see `_already_filed`).
UPSTREAM_GAP_SOURCE = "upstream-gap"

MAX_GAP_BODY_LINES = 30
MAX_GAP_BODY_CHARS = 4000
MAX_TITLE_CHARS = 200

_DECOR = "*_~ \t"  # backticks: see `_strip_decor`
# Leading list bullet / numbered item / block-quote marks before a marker.
_LINE_PREFIX_RE = re.compile(r"^\s*(?:>\s*)*(?:(?:[-*+]|\d+[.)])\s+)?")
_MARKER_RE = re.compile(
    r"^[*_`~\s]*" + UPSTREAM_GAP_MARKER + r"[*_`~\s]*:(?P<rest>.*)$"
)
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
# Other machine-read markers a worker's final message carries — each one ends
# a gap body rather than being swallowed into the upstream issue.
_STOP_PREFIXES = (
    "END_" + UPSTREAM_GAP_MARKER,
    "SMOKE_TESTS",
    "END_SMOKE_TESTS",
    "ISSUE_RESOLUTION:",
    "CONTINUATION:",
    "STATUS:",
    "STUCK:",
    "REVIEW_VERDICT:",
)


@dataclass(frozen=True)
class UpstreamGap:
    """One parsed ``BLOCKED_ON_UPSTREAM`` marker."""

    repo: str  # as written by the worker: a coord repo name or owner/name slug
    title: str
    body: str = ""

    @property
    def key(self) -> str:
        return gap_key(self.repo, self.title)


@dataclass
class UpstreamGapOutcome:
    """What :func:`process_upstream_gaps` did with one gap."""

    gap: UpstreamGap
    # "filed" | "already-filed" | "not-a-gap" | "unknown-repo" | "error"
    # "not-a-gap" (#3930): every symbol the marker named was confirmed, by a
    # cheap mechanical check against the upstream repo's own fetched default
    # branch, to already exist there — nothing filed, no `after=` edge added,
    # a comment naming the symbols posted on the downstream issue instead.
    status: str
    upstream_repo: str | None = None
    number: int | None = None
    url: str | None = None
    queued_after: bool = False
    # True when THIS call enqueued the upstream issue as a fresh drive-queue
    # root (so it actually gets worked). False if it was already queued, or
    # the enqueue failed (see notes). Distinct from ``queued_after``, which is
    # about holding the DOWNSTREAM row.
    upstream_queued: bool = False
    detail: str = ""
    notes: list[str] = field(default_factory=list)


# ── parsing ──────────────────────────────────────────────────────────────────


def _strip_decor(text: str) -> str:
    """Strip markdown emphasis wrapped around *text*.

    ``*``/``_``/``~`` runs are stripped from both ends. A backtick is only
    stripped from an end while the string's backticks are unbalanced, so a
    title ending in an inline code span (``Expose `foo()` ``) keeps its
    closing backtick while a marker wrapped whole in one code span loses
    both of its own.
    """
    text = (text or "").strip()
    while True:
        before = text
        text = text.strip(_DECOR)
        if text.count("`") % 2 == 1:
            if text.startswith("`"):
                text = text[1:]
            elif text.endswith("`"):
                text = text[:-1]
        if text == before:
            return text


def _normalize_title(title: str) -> str:
    text = re.sub(r"[*_`~]", "", _strip_decor(title).lower())
    return re.sub(r"\s+", " ", text).strip().rstrip(" .;:")


def gap_key(repo: str, title: str) -> str:
    """Deterministic idempotency key for a gap: repo + normalised title.

    *repo* is reduced to its last path component and lower-cased so
    ``quadraui`` and ``JDonaghy/quadraui`` name the same gap.
    """
    repo_part = _strip_decor(repo).rsplit("/", 1)[-1].lower()
    digest = hashlib.sha1(
        f"{repo_part}\n{_normalize_title(title)}".encode("utf-8")
    ).hexdigest()
    return digest[:12]


def _match_marker(line: str) -> tuple[str, str] | None:
    """``(repo, title)`` when *line* is a well-formed marker, else ``None``."""
    stripped = _LINE_PREFIX_RE.sub("", line, count=1)
    m = _MARKER_RE.match(stripped)
    if m is None:
        return None
    rest = _strip_decor(m.group("rest"))
    repo_raw, sep, title_raw = rest.partition(":")
    if not sep:
        return None
    repo = _strip_decor(repo_raw)
    title = _strip_decor(title_raw)
    if not repo or not title or not _REPO_RE.match(repo):
        return None
    # A template placeholder (`<repo>`, `<one-line title>`) — e.g. this
    # contract echoed back from a briefing — is never a real gap.
    if title.startswith("<") and title.endswith(">"):
        return None
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rstrip() + "…"
    return repo, title


def _is_body_stop(line: str) -> bool:
    if not line.strip() or _FENCE_RE.match(line) or _HEADING_RE.match(line):
        return True
    if _match_marker(line) is not None:
        return True
    bare = _strip_decor(_LINE_PREFIX_RE.sub("", line, count=1))
    return any(bare.startswith(p) for p in _STOP_PREFIXES)


def parse_upstream_markers(text: str) -> list[UpstreamGap]:
    """Every ``BLOCKED_ON_UPSTREAM: <repo>: <title>`` marker in *text*.

    The body is the run of non-blank lines directly below a marker; it ends
    at a blank line, a code fence, a heading, another marker, or another
    machine-read marker (``SMOKE_TESTS:``, ``ISSUE_RESOLUTION:``, ...).
    Duplicate markers for the same gap (same :func:`gap_key`) collapse to the
    first, keeping the longest body seen.
    """
    lines = (text or "").splitlines()
    gaps: dict[str, UpstreamGap] = {}
    order: list[str] = []
    i = 0
    while i < len(lines):
        hit = _match_marker(lines[i])
        if hit is None:
            i += 1
            continue
        repo, title = hit
        body_lines: list[str] = []
        j = i + 1
        while j < len(lines) and not _is_body_stop(lines[j]):
            if len(body_lines) < MAX_GAP_BODY_LINES:
                body_lines.append(lines[j].rstrip())
            j += 1
        body = textwrap.dedent("\n".join(body_lines)).strip()
        if len(body) > MAX_GAP_BODY_CHARS:
            body = body[:MAX_GAP_BODY_CHARS].rstrip() + "…"
        gap = UpstreamGap(repo=repo, title=title, body=body)
        if gap.key not in gaps:
            gaps[gap.key] = gap
            order.append(gap.key)
        elif len(body) > len(gaps[gap.key].body):
            gaps[gap.key] = UpstreamGap(repo=gaps[gap.key].repo,
                                        title=gaps[gap.key].title, body=body)
        i = j
    return [gaps[k] for k in order]


def final_message_from_log_text(text: str) -> str:
    """The worker's final message out of a raw worker log.

    A ``claude -p --output-format stream-json`` log: the terminal ``result``
    event's text (:attr:`coord.worker_events.WorkerSummary.result_text`),
    falling back to the last assistant turn when the leg died before
    emitting one. Anything else (a plain-text provider log) is returned
    whole — the marker grammar's placeholder rejection keeps an echoed
    briefing from ever parsing as a real gap.
    """
    if not text:
        return ""
    from coord.worker_events import (  # noqa: PLC0415
        WorkerSummary,
        iter_events_from_text,
        latest_assistant_turn_text_from_text,
        update_summary,
    )

    summary = WorkerSummary()
    saw_event = False
    for event in iter_events_from_text(text):
        saw_event = True
        update_summary(summary, event)
    if not saw_event:
        return text
    if summary.result_text:
        return summary.result_text
    return latest_assistant_turn_text_from_text(text) or ""


# ── verification (#3930) ─────────────────────────────────────────────────────

# An inline-code span in the marker's title/body — `` `like_this` `` — is a
# candidate symbol. Only one whose content reads as a single identifier (not
# a sentence, not a shell command, not free prose) is actually checked: this
# is a mechanical filter, not an attempt to understand the marker, so a
# backtick-quoted command or phrase is simply never grepped rather than
# mis-grepped.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_SYMBOL_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?$"
)


def _named_symbols(gap: UpstreamGap) -> list[str]:
    """The checkable symbols *gap*'s title/body actually name, in first-seen
    order, de-duplicated. Empty when the marker names none — a prose-only
    gap description, with no inline code at all, is the common case and is
    never treated as unverifiable-but-checkable."""
    seen: list[str] = []
    for text in (gap.title, gap.body):
        for m in _INLINE_CODE_RE.finditer(text or ""):
            candidate = m.group(1).strip()
            if _SYMBOL_RE.match(candidate) and candidate not in seen:
                seen.append(candidate)
    return seen


@dataclass(frozen=True)
class _SymbolCheck:
    """Outcome of :func:`_verify_symbols` — whether the check itself ran."""

    checked: bool
    missing: tuple[str, ...] = ()
    found_at: dict[str, str] = field(default_factory=dict)
    detail: str = ""


def _run_git(args: list[str], *, cwd, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )


def _verify_symbols(upstream: "Repo", config: "Config", symbols: list[str]) -> _SymbolCheck:
    """Check *symbols* against *upstream*'s fetched ``default_branch``.

    Mechanical, not semantic: ``git fetch`` the branch fresh from ``origin``
    into *upstream*'s locally-configured checkout (:func:`coord.revalidate.
    local_repo_dir` — the same repo-path resolution ``coord test``/
    ``coord merge --revalidate`` use), then ``git grep`` each symbol against
    the fetched ref directly. No worktree/checkout step beyond the fetch is
    needed — ``git grep <ref>`` reads the tree object in place.

    ``checked=False`` means the check itself could not run (no local
    checkout of *upstream* on this machine, or the fetch/grep failed) — this
    is distinct from both "ran, found every symbol" and "ran, found a
    missing symbol". A caller must never read an unrunnable check as either
    a confirmation or a refutation of the gap.
    """
    from coord.revalidate import local_repo_dir  # noqa: PLC0415

    repo_dir = local_repo_dir(config, upstream.name)
    if repo_dir is None or not repo_dir.exists():
        return _SymbolCheck(
            checked=False,
            detail=f"no local checkout of {upstream.name} configured on this machine",
        )

    branch = upstream.default_branch or "main"
    try:
        fetched = _run_git(["git", "fetch", "origin", branch], cwd=repo_dir, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        return _SymbolCheck(checked=False, detail=f"git fetch {branch} failed: {exc}")
    if fetched.returncode != 0:
        return _SymbolCheck(
            checked=False,
            detail=f"git fetch {branch} failed: {fetched.stderr.strip() or fetched.returncode}",
        )

    missing: list[str] = []
    found_at: dict[str, str] = {}
    for symbol in symbols:
        try:
            res = _run_git(
                ["git", "grep", "-I", "-n", "-F", "-e", symbol, "FETCH_HEAD"],
                cwd=repo_dir,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return _SymbolCheck(checked=False, detail=f"git grep {symbol!r} failed: {exc}")
        if res.returncode == 0 and res.stdout.strip():
            # "FETCH_HEAD:path:line:text" -> "path:line".
            parts = res.stdout.strip().splitlines()[0].split(":", 3)
            found_at[symbol] = f"{parts[1]}:{parts[2]}" if len(parts) >= 3 else symbol
        elif res.returncode == 1:
            missing.append(symbol)
        else:
            return _SymbolCheck(
                checked=False,
                detail=f"git grep {symbol!r} exited {res.returncode}: {res.stderr.strip()}",
            )
    return _SymbolCheck(checked=True, missing=tuple(missing), found_at=found_at)


@dataclass(frozen=True)
class _GapVerification:
    """What :func:`_verify_gap` concluded before filing."""

    # False only when every named symbol was confirmed, by a real check, to
    # already exist upstream — the one case where the gap is refuted.
    # Everything else (nothing to check, or the check itself didn't run)
    # defaults True: a tool failure must never silently swallow a real gap.
    real: bool
    # Appended to the filed issue body when `real` but not actually
    # confirmed (no checkable symbols, or verification couldn't run).
    note: str = ""
    # Why — used for the not-a-gap comment/outcome, and for logging.
    detail: str = ""


def _verify_gap(gap: UpstreamGap, upstream: "Repo", config: "Config") -> _GapVerification:
    """Is *gap* real, as far as a cheap grep of *upstream* can tell (#3930)?"""
    symbols = _named_symbols(gap)
    if not symbols:
        return _GapVerification(
            real=True,
            note=(
                "_Not verified against upstream before filing: the marker names "
                "no checkable symbols (an inline-code span like `` `Foo::bar` ``) "
                "— filed on the worker's word alone._"
            ),
        )
    check = _verify_symbols(upstream, config, symbols)
    if not check.checked:
        return _GapVerification(
            real=True,
            note=f"_Not verified against upstream before filing: {check.detail}._",
        )
    if not check.missing:
        branch = upstream.default_branch or "main"
        locations = "; ".join(
            f"`{s}` ({check.found_at[s]})" for s in symbols if s in check.found_at
        )
        return _GapVerification(
            real=False,
            detail=(
                f"every named symbol already exists on {upstream.name}@{branch} "
                f"({locations}) — not filed, the gap is not real"
            ),
        )
    return _GapVerification(real=True)


# ── filing ───────────────────────────────────────────────────────────────────


def _resolve_repo(config: "Config", name: str) -> "Repo | None":
    """A configured repo by coord name or GitHub slug (case-insensitive);
    a bare name also matches a slug's last component."""
    wanted = name.strip().lower()
    repos = list(getattr(config, "repos", []) or [])
    for repo in repos:
        if repo.name.lower() == wanted or (repo.github or "").lower() == wanted:
            return repo
    if "/" not in wanted:
        for repo in repos:
            if (repo.github or "").lower().rsplit("/", 1)[-1] == wanted:
                return repo
    return None


def _ledger_tag(key: str) -> str:
    return f"(gap-key {key})"


# Marks a ledger entry recorded by the `not-a-gap` path (#3930) rather than an
# actual filing — distinguishes the two in `_already_filed`'s loose
# source+tag match, which would otherwise treat either kind as "filed".
_NOT_A_GAP_MARK = "(verified not-a-gap)"


def _already_verified_not_a_gap(repo_name: str, issue_number: int, key: str) -> dict | None:
    """The downstream issue's existing "checked, not real" ledger entry for
    *key*, if any — a replayed transition must not re-run the fetch+grep
    (nor repost the comment) for a gap already confirmed unreal."""
    from coord.state import list_issue_context  # noqa: PLC0415

    tag = _ledger_tag(key)
    for entry in list_issue_context(repo_name, issue_number):
        body = entry.get("body") or ""
        if entry.get("source") == UPSTREAM_GAP_SOURCE and tag in body and _NOT_A_GAP_MARK in body:
            return entry
    return None


def _already_filed(repo_name: str, issue_number: int, key: str) -> dict | None:
    """The downstream issue's existing "filed" ledger entry for *key*, if
    any — never a `not-a-gap` entry (see :func:`_already_verified_not_a_gap`,
    checked first by :func:`_file_one`)."""
    from coord.state import list_issue_context  # noqa: PLC0415

    tag = _ledger_tag(key)
    for entry in list_issue_context(repo_name, issue_number):
        body = entry.get("body") or ""
        if (entry.get("source") == UPSTREAM_GAP_SOURCE and tag in body
                and _NOT_A_GAP_MARK not in body):
            return entry
    return None


def _upstream_issue_body(
    gap: UpstreamGap, *, downstream_ref: str, assignment_id: str | None,
    verify_note: str = "",
) -> str:
    detail = gap.body or "_(The worker gave no further detail.)_"
    leg = f" (assignment `{assignment_id}`)" if assignment_id else ""
    extra = f"\n\n{verify_note}" if verify_note else ""
    return (
        f"{detail}\n\n"
        "---\n\n"
        f"Blocks {downstream_ref}. Auto-filed by the coordinator from a "
        f"`{UPSTREAM_GAP_MARKER}` marker in the final message of a worker on "
        f"{downstream_ref}{leg} (#3676). That issue's drive-queue row waits "
        f"on this one.{extra}\n\n"
        f"<!-- coord:upstream-gap key={gap.key} downstream={downstream_ref} -->"
    )


def _enqueue_kwargs(entry, after: list[str]) -> dict:
    """The operator-declared fields of *entry*, carried through unchanged with
    a new ``after=`` list — :func:`coord.state.enqueue_drive_queue` replaces
    them all on every call (same discipline as ``coord.commands.drive_queue.
    _apply_reversed_overlap_after``)."""
    return dict(
        machine=entry.machine or None,
        after=after,
        hold_after=entry.hold_after,
        hold_reason=entry.hold_reason,
        resume_when=entry.resume_when,
        hold_scope=entry.hold_scope,
        max_fix_rounds=entry.max_fix_rounds,
        no_acceptance=entry.no_acceptance,
        plan_destructive=entry.plan_destructive,
    )


def queue_prereq(
    repo: str,
    number: int,
    *,
    position: int,
    github: str | None = None,
    assume_open: bool = False,
) -> tuple[bool, str]:
    """Enqueue *repo*#*number* as a fresh drive-queue root, idempotently.

    Shared by two callers that both hit the identical "a dependent's
    ``after=`` names an issue nothing ever enqueues" shape: :func:`_queue_upstream`
    (#2056/#1771, an auto-filed ``BLOCKED_ON_UPSTREAM`` gap) and
    :func:`coord.board_doctor_policy._queue_stranded_prereq` (#3944, the
    board-doctor sweep generalizing the same fix to any stranded pre-req,
    not just an auto-filed one). Without this the pre-req is never worked,
    so whatever row holds ``after=`` it defers forever "waiting on <dep>
    (open, not queued)". The pre-req is enqueued as a ROOT: nothing blocks
    it, so it enqueues with no ``after`` of its own.

    *position* is the caller's call — #3944's own dependent-position-or-
    front-of-repo rule lives in the caller, not here, so this stays a pure
    "add the row" primitive one question away from the ordering policy
    above it. Returns ``(enqueued_now, note)``.

    Idempotency / guards (membership alone is NOT enough — the row's live state
    and the issue's resolution matter):

    * Already queued and in flight / waiting (``running``/``waiting``) — left
      EXACTLY as-is; re-enqueuing would reset machine/after/holds to defaults
      and pull it out of flight.
    * Already queued and landed (``done``/``merged-partial``) — left as-is; it
      is satisfied, re-driving would burn a slot the tick reconciles back.
    * Already queued but terminally stuck (``blocked``/``failed``) — NOT
      silently reported as healthy, and deliberately NOT auto-re-driven (that
      would loop a fix-round-ceiling failure on every pass). Surfaced via
      the note so an operator — or the board-doctor — can intervene; the
      dependent already reads this dep as "will never satisfy" (not a silent
      hang).
    * Not queued at all, but the issue is already closed — NOT enqueued (a
      self-heal replay must not re-drive a pre-req resolved out-of-band, e.g.
      closed by hand or its ``done`` row removed). ``assume_open`` skips this
      check on a just-filed path, where the issue was created moments ago and
      the extra ``gh`` round-trip is pure cost.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        STATE_BLOCKED,
        STATE_DONE_LIKE,
        STATE_FAILED,
        QueueEntry,
    )
    from coord.state import enqueue_drive_queue, list_drive_queue  # noqa: PLC0415

    existing = next(
        (
            e
            for e in (QueueEntry.from_row(r) for r in list_drive_queue(repo))
            if e.issue == number
        ),
        None,
    )
    if existing is not None:
        if existing.state in STATE_DONE_LIKE:
            return False, f"pre-req already landed ({existing.state}) — not re-queued"
        if existing.state in (STATE_BLOCKED, STATE_FAILED):
            return False, (
                f"pre-req queue row is {existing.state} — needs attention, "
                "not auto-re-driven (would loop); dependent stays blocked on it"
            )
        return False, f"pre-req already in the drive queue ({existing.state}) — left as-is"

    if not assume_open:
        from coord.github_ops import issue_is_closed  # noqa: PLC0415

        # Fail-open: a transient gh error returns False, so we still enqueue
        # rather than silently drop a legitimate pre-req.
        if issue_is_closed(github or repo, number):
            return False, "pre-req already closed — not re-queued"

    enqueue_drive_queue(repo, number, position=position)
    where = "front of queue" if position == 0 else f"position {position}"
    return True, f"pre-req enqueued for drive (fresh root, {where})"


def _queue_upstream(
    upstream_repo: str,
    number: int,
    *,
    upstream_github: str | None = None,
    assume_open: bool = False,
) -> tuple[bool, str]:
    """Enqueue the auto-filed upstream issue as a fresh drive-queue root.

    Thin wrapper over :func:`queue_prereq`, pinned at ``position=0`` (front
    of its repo's queue), not the tail: this is a blocker — a downstream row
    is ``after=`` it and cannot launch until it lands — so sinking it below
    an existing backlog would de-prioritize the very issue holding the
    chain up. See :func:`queue_prereq` for the shared idempotency guards.
    """
    return queue_prereq(
        upstream_repo, number, position=0,
        github=upstream_github, assume_open=assume_open,
    )


def _block_queue_row(downstream_repo: str, issue_number: int, upstream_key: str) -> tuple[bool, str]:
    """Hold the downstream drive-queue row ``after=upstream_key``.

    Returns ``(applied, note)``. An issue with no queue row is left unqueued
    (nothing to block), and an edge that would close a cycle is refused
    rather than written. What "hold" takes depends on the row's state,
    because the tick only reads ``after=`` for a ``waiting`` row
    (``_resolve_prereqs`` is called from the waiting walk alone;
    ``_reconcile_running`` never looks at it):

    * ``waiting`` — the edge alone is enough; written in place.
    * ``running`` (the usual case: this runs as the work leg ends, mid-drive)
      and every other not-yet-landed state (``parked``/``blocked``/
      ``failed``) — an edge on such a row would be dead data: the drive goes
      on to Test → Review → Merge with the partial change, and a merge moves
      the row to ``done`` without the edge ever being read (review of #3676).
      So the row is taken out of flight: removed through
      :func:`coord.state.dequeue_drive_queue` — the #3282 seam that also
      kills the live ``coord drive --tmux`` session on the daemon host, the
      one place a drive session can be reached from — and re-added at the
      SAME queue position as a fresh ``waiting`` row carrying the edge. The
      tick then defers it "waiting on <upstream> (open, not queued)" and
      relaunches it when the upstream issue lands, attempts reset (being
      blocked upstream is not this issue's failed attempt).
    * ``done``/``merged-partial`` — already landed; there is nothing left to
      hold, so nothing is written.

    An edge already present is a no-op in every state — which is also what
    keeps a replayed transition from stopping a drive that was relaunched
    after the upstream issue landed (the requeued row still carries it).
    """
    from coord.drive_queue import (  # noqa: PLC0415
        STATE_DONE_LIKE,
        STATE_WAITING,
        QueueEntry,
        QueueError,
        validate_enqueue,
    )
    from coord.state import (  # noqa: PLC0415
        dequeue_drive_queue,
        enqueue_drive_queue,
        list_drive_queue,
    )

    entries = [QueueEntry.from_row(r) for r in list_drive_queue()]
    entry = next(
        (e for e in entries if e.repo == downstream_repo and e.issue == issue_number),
        None,
    )
    if entry is None:
        return False, "not in the drive queue — nothing to block"
    if upstream_key in entry.after:
        return True, f"already after {upstream_key}"
    if entry.state in STATE_DONE_LIKE:
        return False, (
            f"queue row already {entry.state} — landed before the gap was "
            f"read, nothing left to hold after {upstream_key}"
        )
    new_after = [*entry.after, upstream_key]
    try:
        validate_enqueue(entries, downstream_repo, issue_number, new_after)
    except QueueError as exc:
        return False, f"after={upstream_key} refused: {exc}"

    if entry.state == STATE_WAITING:
        enqueue_drive_queue(entry.repo, entry.issue, position=None,
                            **_enqueue_kwargs(entry, new_after))
        return True, f"queue row now after {upstream_key}"

    # #3944: `operator=False` — this remove is immediately followed by a
    # re-add below (same row, `after` extended), an automated self-heal, not
    # a human `drive-queue remove`.
    removal = dequeue_drive_queue(entry.repo, entry.issue, operator=False)
    if not removal.get("removed"):
        return False, (
            f"queue row ({entry.state}) vanished before it could be held "
            f"after {upstream_key}"
        )
    try:
        enqueue_drive_queue(entry.repo, entry.issue, position=entry.position,
                            **_enqueue_kwargs(entry, new_after))
    except Exception:
        # Never leave the issue dropped from the queue: put the row back as
        # it was declared (still out of flight), then surface the failure.
        try:
            enqueue_drive_queue(entry.repo, entry.issue, position=entry.position,
                                **_enqueue_kwargs(entry, list(entry.after)))
        except Exception:  # noqa: BLE001 — the original error is the one to report
            log.warning("upstream gap: could not restore queue row %s", entry.key)
        raise
    note = (
        f"in-flight queue row ({entry.state}) taken out of flight and "
        f"returned to waiting after {upstream_key}"
    )
    session = removal.get("driver_session")
    if session and removal.get("driver_ok", True):
        note += f"; stopped driver session {session}"
    elif session:
        note += (
            f"; driver session {session} could NOT be confirmed stopped "
            f"({removal.get('driver_detail') or 'unknown'}) — kill it by hand"
        )
    return True, note


def _file_one(
    gap: UpstreamGap,
    *,
    config: "Config",
    repo_name: str,
    issue_number: int,
    assignment_id: str | None,
) -> UpstreamGapOutcome:
    from coord.drive_queue import entry_key  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        add_issue_context_entry,
        comment_on_issue,
        create_issue,
    )

    upstream = _resolve_repo(config, gap.repo)
    if upstream is None:
        return UpstreamGapOutcome(
            gap, "unknown-repo",
            detail=f"{gap.repo!r} is not a repo in coordinator.yml — not filed",
        )
    downstream = _resolve_repo(config, repo_name)
    downstream_ref = (
        f"{downstream.github}#{issue_number}" if downstream is not None and downstream.github
        else f"{repo_name}#{issue_number}"
    )

    if _already_verified_not_a_gap(repo_name, issue_number, gap.key) is not None:
        return UpstreamGapOutcome(
            gap, "not-a-gap", upstream_repo=upstream.name,
            detail="already checked against upstream and confirmed not real — not re-checked",
        )

    existing = _already_filed(repo_name, issue_number, gap.key)
    if existing is not None:
        m = re.search(r"#(\d+)", existing.get("body") or "")
        number = int(m.group(1)) if m else None
        outcome = UpstreamGapOutcome(
            gap, "already-filed", upstream_repo=upstream.name, number=number,
            detail="already filed for this issue — not re-filed",
        )
        # Re-assert the queue edge: idempotent, and heals a first pass that
        # filed + recorded but died before blocking the row.
        if number is not None:
            try:
                outcome.queued_after, note = _block_queue_row(
                    repo_name, issue_number, entry_key(upstream.name, number)
                )
                outcome.notes.append(note)
            except Exception as exc:  # noqa: BLE001
                outcome.notes.append(f"queue block failed: {exc}")
            # Heal a first pass that filed but never queued the upstream (the
            # #1771 case): enqueue it now, idempotently. This path can replay
            # long after filing, so it must re-check live state — the issue may
            # have been resolved out-of-band (closed, row removed) since.
            try:
                outcome.upstream_queued, qnote = _queue_upstream(
                    upstream.name, number, upstream_github=upstream.github or None,
                )
                outcome.notes.append(qnote)
            except Exception as exc:  # noqa: BLE001
                outcome.notes.append(f"upstream enqueue failed: {exc}")
        return outcome

    # #3930: a cheap, mechanical check before filing anything — is the gap
    # actually real, against the upstream repo's own fetched default branch?
    verification = _verify_gap(gap, upstream, config)
    if not verification.real:
        # The ledger entry goes first, same discipline as a real filing
        # below: it is what makes a replay a no-op, so the window between
        # "decided" and "recorded" is kept as short as possible.
        add_issue_context_entry(
            repo_name,
            issue_number,
            f"Checked `{UPSTREAM_GAP_MARKER}: {gap.repo}: {gap.title}` against "
            f"{upstream.name} — {verification.detail}; not filed. "
            f"{_ledger_tag(gap.key)} {_NOT_A_GAP_MARK}",
            pinned=True,
            source=UPSTREAM_GAP_SOURCE,
        )
        try:
            comment_on_issue(
                repo_name,
                issue_number,
                f"Checked `{UPSTREAM_GAP_MARKER}: {gap.repo}: {gap.title}` "
                f"against {upstream.name} before filing: {verification.detail}. "
                "Not filed — build on what's already there rather than "
                "re-requesting it (#3930).",
                repo_github=downstream.github if downstream is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 — the ledger entry already records why
            log.warning("upstream gap %s: not-a-gap comment failed: %s", gap.key, exc)
        return UpstreamGapOutcome(
            gap, "not-a-gap", upstream_repo=upstream.name,
            detail=verification.detail,
        )

    created = create_issue(
        upstream.name,
        gap.title,
        _upstream_issue_body(
            gap, downstream_ref=downstream_ref, assignment_id=assignment_id,
            verify_note=verification.note,
        ),
        repo_github=upstream.github or None,
    )
    number = int(created["number"])
    url = created.get("url")
    upstream_ref = f"{upstream.github or upstream.name}#{number}"
    outcome = UpstreamGapOutcome(
        gap, "filed", upstream_repo=upstream.name, number=number, url=url,
    )

    # The ledger entry goes first — it is what makes a replay a no-op, so
    # the window between "filed" and "recorded" is kept as short as possible.
    add_issue_context_entry(
        repo_name,
        issue_number,
        f"Blocked on upstream {upstream.name}#{number}: {gap.title} — "
        f"auto-filed from a {UPSTREAM_GAP_MARKER} marker; build on it, don't "
        f"re-draft it. {_ledger_tag(gap.key)}",
        pinned=True,
        source=UPSTREAM_GAP_SOURCE,
    )
    try:
        comment_on_issue(
            repo_name,
            issue_number,
            f"Blocked on upstream {upstream_ref}: **{gap.title}**\n\n"
            f"Filed automatically from a `{UPSTREAM_GAP_MARKER}` marker in a "
            "worker's final message (#3676). This issue's drive-queue row "
            f"waits on {upstream.name}#{number}.",
            repo_github=downstream.github if downstream is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 — the context entry already links them
        outcome.notes.append(f"downstream comment failed: {exc}")
    try:
        outcome.queued_after, note = _block_queue_row(
            repo_name, issue_number, entry_key(upstream.name, number)
        )
        outcome.notes.append(note)
    except Exception as exc:  # noqa: BLE001
        outcome.notes.append(f"queue block failed: {exc}")
    # Enqueue the upstream itself so it actually gets worked — otherwise the
    # downstream row blocked above waits forever on an unqueued issue (#1771).
    # Just created, so skip the is-closed round-trip (assume_open).
    try:
        outcome.upstream_queued, qnote = _queue_upstream(
            upstream.name, number, upstream_github=upstream.github or None,
            assume_open=True,
        )
        outcome.notes.append(qnote)
    except Exception as exc:  # noqa: BLE001 — a failed enqueue must not sink the filing
        outcome.notes.append(f"upstream enqueue failed: {exc}")
    return outcome


def process_upstream_gaps(
    final_message: str,
    *,
    config: "Config",
    repo_name: str,
    issue_number: int,
    assignment_id: str | None = None,
) -> list[UpstreamGapOutcome]:
    """File, link and block on every gap in *final_message* (see module doc).

    Safe to call repeatedly with the same message: an already-recorded gap is
    reported as ``already-filed`` and never filed twice. Per-gap failures are
    reported as ``status="error"`` and never stop the remaining gaps.
    """
    outcomes: list[UpstreamGapOutcome] = []
    if not issue_number:
        return outcomes
    for gap in parse_upstream_markers(final_message):
        try:
            outcome = _file_one(
                gap, config=config, repo_name=repo_name,
                issue_number=issue_number, assignment_id=assignment_id,
            )
        except Exception as exc:  # noqa: BLE001 — one gap must not sink the rest
            log.warning(
                "upstream gap %r for %s#%s failed: %s",
                gap.title, repo_name, issue_number, exc,
            )
            outcome = UpstreamGapOutcome(gap, "error", detail=str(exc))
        log.info(
            "upstream gap %s#%s -> %s: %s %s %s",
            repo_name, issue_number, gap.repo, outcome.status,
            f"#{outcome.number}" if outcome.number else "",
            "; ".join([outcome.detail, *outcome.notes]).strip("; "),
        )
        outcomes.append(outcome)
    return outcomes
