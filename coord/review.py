"""Adversarial code review — dispatch an independent reviewer when a worker finishes.

When `reviews.auto_dispatch` is enabled in `coordinator.yml`, completion of a
"work" assignment triggers a fresh `claude -p` session on a *different* machine
that reads the diff, runs tests, and posts a `gh pr review`. The reviewer has
zero shared context with the worker — that's the whole point.

Public entry points:

- `pick_reviewer_machine(...)`  — choose an idle machine different from the
  worker, with a single-machine fallback.
- `repo_focus_lines(...)`       — #3112: the `### Repo-specific focus` block
  for a repo's `reviews.repo_overrides`. Shared with `coord.dispatch.dispatch`
  so the worker's briefing and the reviewer's briefing are graded/shown the
  exact same rule text, and can never drift apart.
- `build_review_briefing(...)`  — assemble the reviewer's prompt from the
  repo's CLAUDE.md, the generic checklist, any repo-specific overrides
  (`repo_focus_lines`), and the worker's own claims (completion summary +
  commit messages, #3112).
- `dispatch_review(...)`        — full path: find/open PR, pick reviewer,
  build briefing, send to agent server, add a review `Assignment` to the
  board. Called from reconcile when a work assignment transitions to done.
- `dispatch_pending_pr_opens(...)` — #2844: open a PR the instant a work leg
  pushes its branch, rather than waiting for `dispatch_review` to do it once
  the Test/smoke leg finishes — lets the `pull_request` CI run overlap smoke
  and review instead of being serialised after both.

Why a separate module: the work-dispatch path (`coord/dispatch.py`) is shaped
around `Proposal` objects from the brain. Reviews are triggered by completion
events on the board and target an existing PR, so they share little of that
plumbing — keeping them apart avoids twisting both shapes.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
import uuid
from typing import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from coord import github_ops
from coord.config import Config, ReviewsConfig
from coord.dispatch import AGENT_PORT
from coord.models import (
    CLOSES_ISSUE_TYPES,
    SEALED_PATH_AUTHOR_TYPES,
    WORK_LIKE_TYPES,
    Assignment,
    Board,
    Machine,
    coordinator_owned_docs,
    trust_issue_closed_for,
)
from coord.refine_chat import MAX_CLAUDE_MD_CHARS

log = logging.getLogger(__name__)

# #3112 fix-review: the "Worker's own claims" section embeds full commit
# messages (headline + body), not just the headline — a repo-override rule
# that requires a specific *statement* (e.g. vimcode's "state the test was
# observed RED") is typically made in a commit body, not its first line.
# These two caps mirror the clamping every other embed in this module
# already does (MAX_CLAUDE_MD_CHARS, github_ops.truncate_diff_text) so a
# long-lived branch with many fix-review round trips can't blow up every
# future review's prompt with an unbounded number/size of commit messages.
MAX_COMMIT_MESSAGES = 20
MAX_COMMIT_MESSAGE_CHARS = 1000


# ── Review output parsing ────────────────────────────────────────────────────

@dataclass
class ReviewFindings:
    """Structured review output extracted from a reviewer worker log."""
    verdict: str  # "approve" or "request-changes"
    body: str


# Matches the structured block the reviewer is instructed to emit at end of session.
# Allows optional leading/trailing whitespace and tolerates both LF and CRLF.
# Accepts canonical verdicts (approve / request-changes) and short aliases
# (PASS → approve, FAIL → request-changes) for workers that use the shorter form.
#
# The `REVIEW_BODY:` marker is OPTIONAL (#608): reviewers commonly emit the
# verdict line followed directly by Markdown findings and `END_REVIEW`, omitting
# the `REVIEW_BODY:` header. When it's absent the body is everything between the
# verdict line and `END_REVIEW`. `END_REVIEW` stays the required terminator, so a
# stray "REVIEW_VERDICT:" in prose (with no terminator) still won't match.
#
# Markdown decoration around the markers is TOLERATED (#1346): reviewers write
# prose, and a non-trivial fraction of them emit the block as Markdown —
# `**REVIEW_VERDICT: request-changes**` / `**REVIEW_BODY:**` / `## END_REVIEW`,
# or bold only the value (`REVIEW_VERDICT: **approve**`). The original pattern
# required the verdict token to be followed by nothing but whitespace and a
# newline, so a single pair of trailing asterisks made a complete, correct
# review with a valid `END_REVIEW` terminator parse as "no review at all" — the
# verdict was then silently dropped on every consumer of this regex (the #606
# transcript-floor, `notify`, the auto-loop) and the operator was left with a
# blank verdict prompt. `_MD` absorbs emphasis/code-span/heading punctuation and
# surrounding whitespace on either side of each marker; `END_REVIEW` remains the
# required terminator, so the tolerance does not widen what counts as a review.
_MD = r"[*_`#\s]*"
_REVIEW_BLOCK_RE = re.compile(
    rf"REVIEW_VERDICT:{_MD}(approve|request-changes|pass|fail){_MD}[\r\n]+"
    rf"(?:{_MD}REVIEW_BODY:{_MD}[\r\n]+)?(.*?)[\r\n]*{_MD}END_REVIEW",
    re.DOTALL | re.IGNORECASE,
)

# Map short-form aliases to the canonical verdicts understood by post_pr_review.
_VERDICT_ALIASES: dict[str, str] = {
    "pass": "approve",
    "fail": "request-changes",
}


# ── #1348: strict-parse failure diagnostic ──────────────────────────────────
#
# When `_parse_review_text` / `parse_review_from_log` returns None, the caller
# cannot distinguish "text contains no review" from "text HAS a review marker
# but the strict parser rejected it" (e.g. bolded **REVIEW_VERDICT:** from the
# #1346 incident — a 6.2 KB request-changes review was silently dropped because
# the trailing `**` made the verdict group fail to match `_REVIEW_BLOCK_RE`).
#
# `detect_unparsed_review_marker` is a DIAGNOSTIC ONLY.  It MUST NOT be wired
# into `_parse_review_text` / `parse_review_from_log`, and MUST NOT be used to
# auto-record a verdict.  `END_REVIEW` remains the required terminator for a
# legitimate strict parse.  Call it only AFTER the strict parse has already
# returned `None` and the calling floor has confirmed attribution.
#
# #1348 round 3: `_parse_review_text` now also runs
# `_decode_transcript_for_diagnostic` before matching `_REVIEW_BLOCK_RE` (see
# below) — the NDJSON DECODE is shared between the strict parser and this
# diagnostic, because a stream-json log's JSON-escaped newlines defeat
# `[\r\n]+` regardless of which regex runs against it.  Sharing the decode is
# NOT the same as wiring the diagnostic's loose marker detection
# (`_REVIEW_MARKER_DETECT_RE`, which has no `END_REVIEW` requirement) into the
# strict path — that restraint above is unchanged.  `_REVIEW_BLOCK_RE` and its
# mandatory `END_REVIEW` terminator are exactly as strict as before; a
# malformed block (e.g. bolded `**REVIEW_VERDICT:**`, the #1346 shape) still
# fails `_parse_review_text` and must go through this diagnostic, same as
# always.

# Detect a REVIEW_VERDICT: line even when `_REVIEW_BLOCK_RE` cannot extract
# a clean block.  Captures everything on the marker line so the verdict word
# can be extracted after stripping Markdown decorators (e.g. "request-changes**"
# → "request-changes").  No word-boundary constraint before REVIEW_VERDICT: so
# this also fires on bolded lines like "**REVIEW_VERDICT: request-changes**".
_REVIEW_MARKER_DETECT_RE = re.compile(
    r"REVIEW_VERDICT:[^\S\r\n]*([^\r\n]*)",  # [^\S\r\n]* = horizontal whitespace only
    re.IGNORECASE,
)

#: Cap on the excerpt captured by :func:`detect_unparsed_review_marker`.  A few
#: KB is enough to show the operator the malformed block; transcripts can be
#: multi-MB and we must not hold the whole thing.
_DIAGNOSTIC_EXCERPT_MAX: int = 4096


@dataclass
class UnparsedReviewMarker:
    """Diagnostic returned when a transcript contains a ``REVIEW_VERDICT:``
    marker that the strict parser rejected (#1348).

    A strict-parse failure on a transcript that clearly contains a review must
    be loud, not silent — the operator cannot distinguish "reviewer forgot
    ``END_REVIEW``" from "there is nothing to recover" when both paths return
    ``None``.  This carries the raw excerpt and detected verdict word so the
    coordinator surface can:

    * Warn the operator with a greppable ``log.warning`` naming the host and
      transcript path.
    * Print output clearly distinct from "no verdict reported" — two different
      failures, two different fixes, and they must not look the same.
    * Seed the editor with the recovered excerpt so the operator edits /
      confirms what the reviewer wrote rather than typing from scratch.
    * Default the verdict prompt to the detected word when it is canonical.

    Attributes:
        verdict_word: Lowercased, Markdown-stripped word from the
            ``REVIEW_VERDICT:`` line, or ``None`` when the line was blank.
            When it matches a canonical verdict (``approve`` /
            ``request-changes``) or a known alias (``pass`` → approve,
            ``fail`` → request-changes) the operator prompt defaults to it
            instead of ``[s]kip``.
        excerpt: Bounded slice starting from the ``REVIEW_VERDICT:`` line,
            capped at :data:`_DIAGNOSTIC_EXCERPT_MAX` chars.  For stream-json
            logs this is the DECODED assistant text (real newlines, no JSON
            scaffolding) — see :func:`_decode_transcript_for_diagnostic`.
            Passed to ``_collect_review_body_via_editor`` as ``pre_body`` so
            the operator edits the real review text.
        transcript_path: Filesystem path of the transcript scanned.  For the
            remote-ssh path this is the path **on the remote host** (useful in
            a ``ssh <host> cat <path>`` hint).  ``None`` when unknown.
        host: SSH hostname the transcript was fetched from, or ``None`` for a
            local transcript.
    """

    verdict_word: str | None
    excerpt: str
    transcript_path: str | None = None
    host: str | None = None


def _decode_transcript_for_diagnostic(text: str) -> str | None:
    """Best-effort NDJSON (stream-json) decode of *text*, for #1348 round 2.

    ``claude -p --output-format stream-json`` (and Claude Code's own session
    transcripts under ``~/.claude/projects/``) emit one JSON object per line.
    Any real newline inside an assistant message's text is therefore stored
    on disk as the two-character escape ``\\n``, not a ``0x0A`` byte — valid
    JSON, but useless to a regex that anchors on ``[\\r\\n]``. Worse, line 1 of
    every agent log is a non-JSON ``# agent=... argv=...`` comment that embeds
    the reviewer's own ``--system-prompt`` argument verbatim (also
    newline-escaped onto one physical line by ``agent.py``'s
    ``.replace("\\n", "\\\\n")``) — and that system prompt CONTAINS the literal
    ``REVIEW_VERDICT: approve\\nREVIEW_BODY:\\n<your full review text in
    markdown>\\nEND_REVIEW`` template the reviewer is instructed to fill in.
    A one-shot ``text.replace("\\\\n", "\\n")`` over the whole raw log would
    turn that template into something that matches — and since it comes
    first in the file, a plain ``.search()`` would find it before the real
    verdict emitted later by the assistant.

    This decodes *text* using the same machinery as the strict parser
    (:func:`parse_event` / ``_assistant_text`` in :mod:`coord.worker_events`):
    only lines that parse as a JSON object contribute anything at all, and
    only ``"assistant"``-typed events contribute text — the non-JSON
    argv/header comment line (and any ``"system"``/``"user"`` event that
    might otherwise echo the system-prompt template back) is silently
    skipped, never concatenated into the decoded text. Returns the assistant
    texts joined by real ``"\\n"``, in emission order — or ``None`` when
    *text* contains no NDJSON at all (e.g. an old-format plain-text log, or
    plain prose in a test fixture), so the caller falls back to treating
    *text* as-is.

    #1348 round 3: this is now shared by :func:`_parse_review_text` (the
    STRICT parser) as well as :func:`detect_unparsed_review_marker` (the
    loose diagnostic) — see the comment above `detect_unparsed_review_marker`
    for why sharing the decode does not loosen the strict grammar.
    `parse_review_from_log`'s ``is_stream_json`` branch already ran
    individual lines through `parse_event`/`_assistant_text`, but its
    plain-text fallback branch (taken whenever `is_stream_json`'s
    first-non-comment-line heuristic misses, even on a log that IS valid
    NDJSON) handed the strict regex raw, undecoded text — silently dropping
    well-formed verdicts whose newlines were still JSON-escaped on disk.

    #1710 inventory: kept as a direct ``coord.worker_events`` import, not
    routed through ``provider.parse_log()``. This decodes the generic
    Anthropic-Messages-API ``type: "assistant"`` / ``message.content``
    envelope — a wire-format detail any Agent-SDK-shaped backend can share —
    not claude-*business* semantics, and ``Provider``/``WorkerSummary`` have
    no equivalent "raw assistant text" primitive to route through (adding
    one would mean a new abstract ``Provider`` method, touching every
    concrete provider including ``opencode.py`` — out of scope here). See
    ``tests/test_provider_seam.py::TestReviewExtractionForASecondProvider``
    for a second-provider log that this already decodes correctly today.
    """
    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    texts: list[str] = []
    saw_json = False
    for line in text.splitlines():
        event = parse_event(line)
        if event is None:
            continue
        saw_json = True
        if event.type == "assistant":
            t = _assistant_text(event)
            if t:
                texts.append(t)
    if not saw_json:
        return None
    return "\n".join(texts)


def detect_unparsed_review_marker(
    text: str,
    *,
    transcript_path: str | None = None,
    host: str | None = None,
) -> UnparsedReviewMarker | None:
    """Return diagnostic info when *text* contains a ``REVIEW_VERDICT:`` marker
    that the strict parser would reject (#1348).

    **Diagnostic only — never a parser.**  Must be called AFTER
    :func:`parse_review_from_log` has returned ``None``.  Never wire this into
    :func:`_parse_review_text` / :func:`parse_review_from_log`, and never use
    its return value to auto-record a verdict.  ``END_REVIEW`` is still the
    required terminator for a legitimate strict parse.

    *text* is decoded via :func:`_decode_transcript_for_diagnostic` before any
    matching happens (#1348 round 2) — callers pass the RAW file/transcript
    content, which for a stream-json log has every real newline inside the
    assistant's text escaped as ``\\n`` (two characters), not ``0x0A``; the
    strict-parser regexes below would never match that unless it's decoded
    first, same as the strict parser itself does via
    ``parse_event``/``_assistant_text``. When *text* isn't NDJSON at all (old
    plain-text logs) it's matched as-is, unchanged from before.

    Returns ``None`` when:

    * No ``REVIEW_VERDICT:`` marker is present — *text* is genuinely not a
      review; no false positives.
    * The strict parse actually SUCCEEDED — a defensive guard so a caller that
      forgets the "call after strict-parse" contract never double-reports.

    Otherwise returns an :class:`UnparsedReviewMarker` with a bounded excerpt
    (capped at :data:`_DIAGNOSTIC_EXCERPT_MAX` chars from the marker line) and
    the detected verdict word with Markdown decorators stripped.
    """
    decoded = _decode_transcript_for_diagnostic(text)
    search_text = text if decoded is None else decoded

    matches = list(_REVIEW_MARKER_DETECT_RE.finditer(search_text))
    if not matches:
        return None
    # Guard: strict parse succeeded on the SAME (decoded) text → return None,
    # never double-report (#1348).
    if _REVIEW_BLOCK_RE.search(search_text):
        return None
    # Take the LAST match, not the first (#1348 round 2). Decoding already
    # excludes the non-JSON argv/header comment line that embeds the
    # reviewer's own system-prompt TEMPLATE (see
    # _decode_transcript_for_diagnostic), but this also protects the
    # plain-text fallback path (decoded is None) where that template text
    # could still precede the real verdict in the raw log, and it mirrors
    # `_parse_review_text`'s own `matches[-1]`: a reviewer that second-guesses
    # itself mid-session emits the real verdict last.
    m = matches[-1]
    # Extract and normalize the verdict word.  Strip common Markdown decorators
    # (*_`#) so e.g. "**request-changes**" normalises to "request-changes".
    raw_line = m.group(1).strip()
    clean_word = re.sub(r"[*_`#]+", "", raw_line).strip().lower()
    verdict_word = clean_word if clean_word else None
    # Bounded excerpt: start at the beginning of the REVIEW_VERDICT: line,
    # capture up to _DIAGNOSTIC_EXCERPT_MAX chars so the operator can see
    # the full verdict block without holding the whole (possibly multi-MB) log.
    line_start = search_text.rfind("\n", 0, m.start()) + 1  # +1 skips the \n itself
    start = line_start
    end = min(len(search_text), start + _DIAGNOSTIC_EXCERPT_MAX)
    excerpt = search_text[start:end]
    return UnparsedReviewMarker(
        verdict_word=verdict_word,
        excerpt=excerpt,
        transcript_path=transcript_path,
        host=host,
    )


# ── #1956 ask 3: END_REVIEW present, REVIEW_VERDICT absent entirely ─────────
#
# quadraui#533's live incident: the reviewer wrote a complete, thorough
# review and ended with `END_REVIEW`, but never wrote `REVIEW_VERDICT:`
# ANYWHERE — grepping the raw log found the string exactly once, inside the
# briefing's own instructions, never in an assistant message. That is a
# DIFFERENT failure signature from #1348's "marker present but malformed"
# (e.g. a bolded `**REVIEW_VERDICT:**`): here the model followed the *tail*
# of the required format while dropping the *header* that carries the data
# entirely, rather than attempting the header and getting the syntax wrong.
# `detect_unparsed_review_marker` cannot see this case at all — it only
# fires when a `REVIEW_VERDICT:` marker exists to detect.

@dataclass
class EndReviewWithoutVerdict:
    """Diagnostic returned when *text* has an ``END_REVIEW`` terminator but
    NO ``REVIEW_VERDICT:`` marker anywhere (#1956 ask 3).

    Distinguishing this from a crashed/truncated session (which never
    reaches ``END_REVIEW`` at all — nothing to recover) matters
    operationally: a session that wrote ``END_REVIEW`` almost certainly
    reached a real verdict, it just never printed the machine-readable
    header for it. The verdict is very likely recoverable from ``excerpt``
    (the prose immediately before ``END_REVIEW``) by an operator reading it
    and re-running ``coord report-result --assignment <id> --verdict
    <approve|request-changes> --verdict-source recovered --verdict-reason
    "..." --body-file <extracted-review.md>``.

    **Diagnostic only — never a parser, same contract as
    :func:`detect_unparsed_review_marker`.** MUST NOT be used to
    auto-record a verdict, and must only be called AFTER
    :func:`parse_review_from_log` / :func:`_parse_review_text` has already
    returned ``None`` for this same text.
    """

    excerpt: str
    transcript_path: str | None = None
    host: str | None = None


_END_REVIEW_DETECT_RE = re.compile(rf"{_MD}END_REVIEW{_MD}", re.IGNORECASE)


def detect_end_review_without_verdict(
    text: str,
    *,
    transcript_path: str | None = None,
    host: str | None = None,
) -> EndReviewWithoutVerdict | None:
    """Return diagnostic info when *text* has ``END_REVIEW`` but no
    ``REVIEW_VERDICT:`` marker at all (#1956 ask 3).

    *text* is decoded via :func:`_decode_transcript_for_diagnostic` first,
    exactly like the strict parser and :func:`detect_unparsed_review_marker`
    — see that function's docstring for why (stream-json newline-escaping,
    and excluding the non-JSON argv/header comment line whose embedded
    system-prompt TEMPLATE would otherwise spuriously contain both markers).

    Returns ``None`` when:

    * No ``END_REVIEW`` terminator is present at all — this is NOT the
      #1956 signature; a session that never reached ``END_REVIEW`` more
      likely crashed or was truncated, a different failure with a different
      (probably unrecoverable) remedy.
    * A ``REVIEW_VERDICT:`` marker IS present somewhere, even a malformed
      one — that is :func:`detect_unparsed_review_marker`'s territory: the
      header was ATTEMPTED (and rejected), not omitted entirely. The two
      diagnostics are mutually exclusive by construction.
    * The strict parse actually SUCCEEDED on this same text — defensive
      guard mirroring :func:`detect_unparsed_review_marker`, so a caller
      that forgets the "call after strict-parse" contract never
      double-reports.

    The excerpt is the text immediately BEFORE the LAST ``END_REVIEW`` line
    (a reviewer that second-guesses itself mid-session writes the real one
    last — same convention as :func:`_parse_review_text`'s ``matches[-1]``),
    capped at :data:`_DIAGNOSTIC_EXCERPT_MAX` chars — there's no
    ``REVIEW_VERDICT:``/``REVIEW_BODY:`` line to anchor on instead, since by
    definition neither exists in *text*.
    """
    decoded = _decode_transcript_for_diagnostic(text)
    search_text = text if decoded is None else decoded

    if _REVIEW_MARKER_DETECT_RE.search(search_text):
        return None  # a REVIEW_VERDICT: marker exists — different diagnostic
    matches = list(_END_REVIEW_DETECT_RE.finditer(search_text))
    if not matches:
        return None
    if _REVIEW_BLOCK_RE.search(search_text):
        return None  # strict parse actually succeeded — never double-report

    m = matches[-1]
    end = m.start()
    start = max(0, end - _DIAGNOSTIC_EXCERPT_MAX)
    excerpt = search_text[start:end].strip()
    return EndReviewWithoutVerdict(
        excerpt=excerpt,
        transcript_path=transcript_path,
        host=host,
    )


# ── #248: machine-readable review header ────────────────────────────────────
#
# When the coordinator posts a review comment back to GitHub it prepends a
# short HTML comment carrying the verdict in machine-readable form.  The
# header is invisible to humans on the PR but lets the TUI render a verdict
# badge and lets the coordinator session check the verdict without reading
# the full prose body (which can be several KB).
#
# Format:
#     <!-- coord:review verdict=request-changes blocking=2 nonblocking=5 \
#          nits=2 reviewer=elitebook assignment=144ffa027a31 -->
#
# `verdict` is always present.  Counts are best-effort: when the prose
# body uses recognisable section headings, the coordinator counts items
# under each; when it can't, those tokens are omitted (parser tolerates
# missing tokens).
_REVIEW_HEADER_RE = re.compile(
    r"<!--\s*coord:review\s+([^>]+?)\s*-->",
    re.IGNORECASE,
)

# Maps human section-heading keywords (case-insensitive) to the count
# category they belong to.  The heuristic walks the prose body, splits
# on markdown headings, and bucketises bullet-list items under each.
_SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "blocking": ("blocking", "required change", "must fix", "must-fix",
                 "changes required"),
    "nonblocking": ("non-blocking", "non blocking", "concerns",
                    "should fix", "should-fix", "observations"),
    "nits": ("nits", "nit:", "polish", "minor", "style"),
}

# Check buckets in order of keyword specificity so that "Non-blocking
# concerns" doesn't accidentally match the `blocking` bucket first.
_ORDERED_BUCKETS: tuple[str, ...] = ("nonblocking", "nits", "blocking")

# Phrases that make an otherwise-prose line in a *blocking* section readable
# as "the reviewer explicitly raised nothing here" (#1456).  Only consulted
# for short lines — a long paragraph is prose the bullet counter cannot see,
# and therefore evidence that the section is NOT confirmed empty.
_NO_FINDINGS_PHRASES: tuple[str, ...] = (
    "none", "n/a", "nothing", "no blocking", "no issues", "no required",
    "no must-fix", "no must fix", "all clear",
)
_NO_FINDINGS_MAX_LEN = 60


def format_review_header(
    *,
    verdict: str,
    reviewer_machine: str | None = None,
    assignment_id: str | None = None,
    blocking: int | None = None,
    nonblocking: int | None = None,
    nits: int | None = None,
) -> str:
    """Build the HTML-comment header that machines parse.

    `verdict` is required; everything else is optional and only emitted
    when provided.  Returns a single line (no trailing newline).
    """
    parts = [f"verdict={verdict}"]
    if blocking is not None:
        parts.append(f"blocking={blocking}")
    if nonblocking is not None:
        parts.append(f"nonblocking={nonblocking}")
    if nits is not None:
        parts.append(f"nits={nits}")
    if reviewer_machine:
        parts.append(f"reviewer={reviewer_machine}")
    if assignment_id:
        parts.append(f"assignment={assignment_id}")
    return f"<!-- coord:review {' '.join(parts)} -->"


def parse_review_header(body: str) -> dict[str, str | int] | None:
    """Extract the coord:review header from *body*, or ``None`` when missing.

    Numeric tokens (``blocking``, ``nonblocking``, ``nits``) are returned as
    ``int``; everything else stays a ``str``.  Tolerates extra whitespace
    and unknown tokens.
    """
    m = _REVIEW_HEADER_RE.search(body)
    if not m:
        return None
    out: dict[str, str | int] = {}
    for token in m.group(1).split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.lower()
        if key in ("blocking", "nonblocking", "nits"):
            try:
                out[key] = int(value)
            except ValueError:
                continue
        else:
            out[key] = value
    return out if "verdict" in out else None


_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\S")


def _bucket_for_heading(heading_line: str) -> str | None:
    """Map a markdown heading line to a `_SECTION_KEYWORDS` bucket, or None."""
    heading_text = heading_line.lstrip("#").strip().lower()
    for bucket in _ORDERED_BUCKETS:
        if any(kw in heading_text for kw in _SECTION_KEYWORDS[bucket]):
            return bucket
    return None


def _iter_review_sections(body: str) -> Iterator[tuple[str | None, list[str]]]:
    """Yield ``(bucket, lines)`` for each markdown section of *body*.

    *bucket* is the `_SECTION_KEYWORDS` bucket the section's heading maps to,
    or ``None`` for the preamble (text before the first heading) and for
    headings that match no keyword.  *lines* are the right-stripped lines
    under that heading, up to the next heading.

    Shared by `estimate_review_counts` (which counts bullets) and
    `blocking_findings_confirmed_absent` (which inspects prose), so the two
    can never disagree about where a section starts and ends (#1456).
    """
    current: str | None = None
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            yield current, lines
            current = _bucket_for_heading(line)
            lines = []
            continue
        lines.append(line)
    yield current, lines


def estimate_review_counts(
    body: str,
) -> tuple[int | None, int | None, int | None]:
    """Best-effort count of (blocking, nonblocking, nits) bullets in *body*.

    Walks markdown sections.  A section is recognised when its heading
    contains one of `_SECTION_KEYWORDS`; counts are the number of `- ` /
    `* ` / `1. ` bullets directly under that section (until the next
    heading).  Returns ``(None, None, None)`` when no recognised
    sections appear — the heuristic refuses to guess.

    **``None`` means "could not determine", never "zero" (#1456).**  A caller
    that conflates the two turns a heuristic miss into a positive claim that
    the reviewer raised nothing — which is how a `request-changes` verdict got
    silently rewritten to `approve` on #1445.  Callers deciding *anything*
    about whether blocking findings exist must go through
    `blocking_findings_confirmed_absent`, not compare these values themselves.
    """
    counts: dict[str, int | None] = {"blocking": None, "nonblocking": None, "nits": None}
    for bucket, lines in _iter_review_sections(body):
        if bucket is None:
            continue
        # Initialise the count for this bucket so it shows as 0 (not None)
        # even when the section is empty.
        counts[bucket] = (counts[bucket] or 0) + sum(
            1 for line in lines if _BULLET_RE.match(line)
        )
    return counts["blocking"], counts["nonblocking"], counts["nits"]


def _is_no_findings_line(text: str) -> bool:
    """True when *text* reads as an explicit "nothing here" marker.

    Deliberately narrow: only short lines qualify, so a real finding written
    as prose ("No error path is handled when the worktree leaks, so …") is
    never mistaken for an empty section.
    """
    stripped = text.strip(" \t*_`~>-–—.:!()[]")
    if not stripped:
        return True
    if len(stripped) > _NO_FINDINGS_MAX_LEN:
        return False
    low = stripped.lower()
    return any(phrase in low for phrase in _NO_FINDINGS_PHRASES)


def blocking_findings_confirmed_absent(body: str) -> bool:
    """True only when *body* carries POSITIVE evidence of zero blocking findings.

    This is the evidence standard for overriding a reviewer's verdict (#1456).
    It is deliberately *fail-closed*: everything the heuristic cannot read
    returns ``False``, i.e. "assume the reviewer meant what it said".

    Returns ``True`` only when **all** of the following hold:

    1. A blocking section was actually located (``blocking is not None`` —
       a heading matching `_SECTION_KEYWORDS["blocking"]`).  A body with no
       such heading yields ``None`` = *unknown*, which must never be read as
       zero: that conflation is the #1456 defect, where a well-formed prose
       `request-changes` on #1445 was rewritten to `approve` because the
       *nits* bucket happened to parse as 0 while *blocking* parsed as None.
    2. That section contains no bullets (an explicit parsed zero).
    3. That section contains no substantive prose either — a reviewer who
       writes blocking findings as paragraphs under "## Blocking" would
       otherwise count as zero and fail open all the same.  Short "None" /
       "N/A" markers are allowed (that's the shape being looked for).
    """
    blocking, _nonblocking, _nits = estimate_review_counts(body)
    if blocking is None or blocking != 0:
        return False
    for bucket, lines in _iter_review_sections(body):
        if bucket != "blocking":
            continue
        for line in lines:
            if not _is_no_findings_line(line.strip()):
                return False
    return True


def extract_blocking_section(body: str) -> str:
    """Return the verbatim text of *body*'s "## Blocking findings" section(s).

    Reuses the same section parser as `estimate_review_counts` /
    `blocking_findings_confirmed_absent` (#1456), so this can never disagree
    with them about where the blocking section starts and ends. Multiple
    headings that map to the `blocking` bucket are joined in document order.

    Returns "" when no blocking heading is found, or when the section reads
    as an explicit "none" marker (`_is_no_findings_line` on every line) —
    callers should not carry forward an empty section as if it were a real
    finding.

    #2466: replaces a blind `body[:240]` truncation that was silently
    dropping every blocking finding past the first sentence from the #603
    per-issue context digest — the mechanism meant to carry a review's
    findings forward into the next re-review round.
    """
    chunks: list[str] = []
    for bucket, lines in _iter_review_sections(body):
        if bucket != "blocking":
            continue
        text = "\n".join(lines).strip("\n")
        if text.strip():
            chunks.append(text)
    joined = "\n\n".join(chunks).strip()
    if not joined:
        return ""
    if all(_is_no_findings_line(line.strip()) for line in joined.splitlines() if line.strip()):
        return ""
    return joined


def _parse_review_text(text: str) -> ReviewFindings | None:
    """Extract the last ReviewFindings block from *text*, or None.

    *text* is decoded via :func:`_decode_transcript_for_diagnostic` before
    `_REVIEW_BLOCK_RE` runs (#1348 round 3): a `claude -p --output-format
    stream-json` log stores every real newline inside the assistant's review
    text as the literal two-character escape ``\\n`` on disk, which
    `_REVIEW_BLOCK_RE`'s ``[\\r\\n]+`` can never match unless it's decoded
    first — and `parse_review_from_log`'s plain-text fallback branch (taken
    whenever `is_stream_json`'s first-non-comment-line heuristic misses, even
    on a log that IS valid NDJSON) was handing this function raw, undecoded
    text, silently dropping well-formed verdicts. Decoding also drops the
    non-JSON `# argv=...` header line, whose embedded system-prompt template
    would otherwise be a spurious match — hence `matches[-1]` (last match),
    not `.search()`, same defense as `detect_unparsed_review_marker`. When
    *text* isn't NDJSON at all (plain-text log, or an already-decoded single
    assistant-message chunk from the stream-json per-event path below), the
    decode is a no-op and *text* is matched as-is — this does NOT loosen the
    grammar: `END_REVIEW` is still the mandatory terminator, and a malformed
    block (e.g. bolded markers) still fails here exactly as before.
    """
    decoded = _decode_transcript_for_diagnostic(text)
    search_text = text if decoded is None else decoded
    matches = list(_REVIEW_BLOCK_RE.finditer(search_text))
    if not matches:
        return None
    m = matches[-1]
    verdict_raw = m.group(1).lower().strip()
    # Normalize aliases: PASS → approve, FAIL → request-changes.
    verdict = _VERDICT_ALIASES.get(verdict_raw, verdict_raw)
    body = m.group(2).strip()
    if verdict not in ("approve", "request-changes"):
        return None
    return ReviewFindings(verdict=verdict, body=body)


def _parse_review_from_lines(
    lines: Iterable[str],
    *,
    stream_json: bool,
) -> ReviewFindings | None:
    """Shared core: extract review findings from log lines.

    `lines` may be any iterable of strings (file iterator, ``str.splitlines()``,
    ``httpx.Response.text.splitlines()``). Used by both `parse_review_from_log`
    (local file) and `parse_review_from_agent` (HTTP fetch).

    #1710 inventory: kept direct — see the identical note on
    ``_decode_transcript_for_diagnostic`` above. `stream_json` here is the
    generic "is this NDJSON at all" detection (`is_stream_json`'s
    first-non-comment-line heuristic), not a claude-specific check.
    """
    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    if not stream_json:
        text = "\n".join(lines)
        return _parse_review_text(text)

    all_texts: list[str] = []
    for line in lines:
        event = parse_event(line.rstrip("\n"))
        if event is None:
            continue
        if event.type == "assistant":
            text = _assistant_text(event)
            if text:
                all_texts.append(text)
    # Search from the end — the reviewer emits the verdict last.
    for text in reversed(all_texts):
        findings = _parse_review_text(text)
        if findings is not None:
            return findings
    # Fallback: search the full concatenated text (handles multi-turn output).
    return _parse_review_text("\n".join(all_texts))


def parse_review_from_log(log_path: str | Path) -> ReviewFindings | None:
    """Parse review findings from a completed reviewer worker log.

    Handles both stream-json (``--output-format stream-json``) and plain-text
    log formats. Returns ``None`` if the file does not exist or contains no
    structured review output.

    #1710 inventory: ``is_stream_json`` here is a generic on-disk-shape sniff
    (does the first non-comment line start with ``{``?), not claude-specific
    parsing — kept direct rather than routed through a ``Provider``. See the
    note on ``_decode_transcript_for_diagnostic``.
    """
    from coord.worker_events import is_stream_json  # noqa: PLC0415

    p = Path(log_path)
    if not p.exists():
        return None

    if is_stream_json(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return _parse_review_from_lines(f, stream_json=True)
        except OSError:
            return None
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return _parse_review_from_lines(text.splitlines(), stream_json=False)


def parse_review_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> ReviewFindings | None:
    """Fetch a reviewer worker's log via the agent's ``/logs/<id>`` endpoint
    and parse the verdict.

    Use this instead of `parse_review_from_log` when the worker ran on a
    remote agent and the log file isn't on the coordinator's local
    filesystem. Returns ``None`` on network failure, empty log, or no
    structured review output.
    """
    import httpx  # noqa: PLC0415

    url = f"http://{host}:{port}/logs/{assignment_id}"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        text = resp.text
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    if not text:
        return None
    lines = text.splitlines()
    # Detect format the same way `is_stream_json` does for files: the first
    # non-comment, non-blank line starts with `{`.
    stream_json = False
    for line in lines:
        stripped = line.strip()
        if not stripped or line.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break
    return _parse_review_from_lines(lines, stream_json=stream_json)


def fetch_review_findings_from_github(
    repo_github: str,
    issue_number: int,
    assignment_id: str,
) -> ReviewFindings | None:
    """Recover a review's findings from the GitHub message bus.

    Interactive (claude-pty) reviews don't produce a parseable log; their full
    body is instead posted to the issue under a `coord:review-findings` marker
    by `report-result --body-file` (via the issue_store seam).  This reads those
    comments back, so a fix worker on ANY machine can recover the findings even
    when the review ran elsewhere and isn't in the local DB — GitHub is the one
    store every machine already reaches.  Returns ``None`` on any failure.

    Routed through :func:`coord.github_ops.get_issue_comments` (#1483) rather
    than shelling out to ``gh`` directly — ``github_ops`` is the single ``gh``
    sink so a GitLab/bare-DB backend has one seam to sit beside.
    """
    import subprocess as _sp  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord.comments import extract_findings_block  # noqa: PLC0415

    if not (repo_github and assignment_id):
        return None
    try:
        comments = github_ops.get_issue_comments(repo_github, issue_number)
    except (RuntimeError, _sp.TimeoutExpired, OSError, ValueError):
        return None
    # Newest-first so a re-review's findings win over an earlier iteration's.
    for c in reversed(comments):
        hit = extract_findings_block(c.get("body", ""), assignment_id)
        if hit is not None:
            verdict, body = hit
            return ReviewFindings(verdict=verdict or "request-changes", body=body)
    return None


# ── Test-gate verdict output parsing (#1351) ────────────────────────────────
#
# A human-attended Test (smoke) session had, before this, exactly ONE channel
# for its verdict to reach the board: the agent successfully running `coord
# test --passed|--fail` INSIDE the session. If `coord` wasn't on the
# session's PATH, the command errored, or the agent simply never got to it,
# the verdict was gone — no structured block, no transcript floor, no
# fallback. This is the same gap #651 closed for reviews (the
# ``REVIEW_VERDICT:``/``REVIEW_BODY:``/``END_REVIEW`` block, recovered by the
# #606 transcript-floor even when ``coord report-result`` never ran), applied
# to the Test gate: ``TEST_VERDICT: passed|failed`` / ``TEST_REASON:`` /
# ``END_TEST``, parsed exactly as tolerantly as ``_REVIEW_BLOCK_RE`` parses a
# review — Markdown decoration around the markers (bold, code-spans,
# headings) is absorbed by the same ``_MD`` pattern, for the same #1346
# reason: a reviewer/tester that bolds ``**TEST_VERDICT:**`` must not have an
# otherwise-complete, correctly-terminated block silently discarded.

@dataclass
class TestVerdictFindings:
    """Structured Test-gate verdict extracted from a smoke-session log."""
    __test__ = False  # not a pytest test class — the name just starts with "Test"
    verdict: str  # "passed" or "failed"
    reason: str


_TEST_BLOCK_RE = re.compile(
    rf"TEST_VERDICT:{_MD}(passed|failed|pass|fail){_MD}[\r\n]+"
    rf"(?:{_MD}TEST_REASON:{_MD}[\r\n]+)?(.*?)[\r\n]*{_MD}END_TEST",
    re.DOTALL | re.IGNORECASE,
)

# Map short-form aliases to the canonical verdicts, mirroring
# `_VERDICT_ALIASES` for the review block.
_TEST_VERDICT_ALIASES: dict[str, str] = {
    "pass": "passed",
    "fail": "failed",
}


def _parse_test_verdict_text(text: str) -> TestVerdictFindings | None:
    """Extract the last TestVerdictFindings block from *text*, or None.

    Mirrors :func:`_parse_review_text` exactly: *text* is decoded via
    :func:`_decode_transcript_for_diagnostic` first (a stream-json log stores
    every real newline inside the assistant's text as the two-character
    escape ``\\n``, which ``[\\r\\n]+`` can never match undecoded), and the
    LAST match wins (``matches[-1]``, not ``.search()``) so a tester that
    second-guesses itself mid-session has its final verdict win, and so the
    non-JSON argv/header comment line's embedded briefing template (which
    contains this exact block as an example — see ``_smoke_report_reminder``
    in ``coord/commands/dispatch_workers.py``) never wins over a real,
    later verdict.
    """
    decoded = _decode_transcript_for_diagnostic(text)
    search_text = text if decoded is None else decoded
    matches = list(_TEST_BLOCK_RE.finditer(search_text))
    if not matches:
        return None
    m = matches[-1]
    verdict_raw = m.group(1).lower().strip()
    verdict = _TEST_VERDICT_ALIASES.get(verdict_raw, verdict_raw)
    reason = m.group(2).strip()
    if verdict not in ("passed", "failed"):
        return None
    return TestVerdictFindings(verdict=verdict, reason=reason)


def _parse_test_verdict_from_lines(
    lines: Iterable[str],
    *,
    stream_json: bool,
) -> TestVerdictFindings | None:
    """Shared core: extract a Test-gate verdict from log lines.

    Mirrors :func:`_parse_review_from_lines` — used by both
    :func:`parse_test_verdict_from_log` (local file) and the remote
    transcript-floor's own file-fetch-then-parse path.
    """
    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    if not stream_json:
        text = "\n".join(lines)
        return _parse_test_verdict_text(text)

    all_texts: list[str] = []
    for line in lines:
        event = parse_event(line.rstrip("\n"))
        if event is None:
            continue
        if event.type == "assistant":
            text = _assistant_text(event)
            if text:
                all_texts.append(text)
    # Search from the end — the tester emits the verdict last.
    for text in reversed(all_texts):
        findings = _parse_test_verdict_text(text)
        if findings is not None:
            return findings
    # Fallback: search the full concatenated text (handles multi-turn output).
    return _parse_test_verdict_text("\n".join(all_texts))


def parse_test_verdict_from_log(log_path: str | Path) -> TestVerdictFindings | None:
    """Parse a Test-gate verdict from a completed smoke-session log (#1351).

    Sibling to :func:`parse_review_from_log`: same stream-json/plain-text
    on-disk-shape detection, same tolerant grammar. Returns ``None`` if the
    file does not exist or contains no structured ``TEST_VERDICT:`` block.
    """
    from coord.worker_events import is_stream_json  # noqa: PLC0415

    p = Path(log_path)
    if not p.exists():
        return None

    if is_stream_json(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return _parse_test_verdict_from_lines(f, stream_json=True)
        except OSError:
            return None
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return _parse_test_verdict_from_lines(text.splitlines(), stream_json=False)


REVIEWER_SYSTEM_PROMPT = """\
You are an independent code reviewer dispatched by the coordinator. \
Your job is to find problems — do NOT rubber-stamp.

Rules:
- You have a fresh session. You have NO context from the worker who wrote \
this code. Treat the diff as if you're reading it for the first time.
- You are NOT allowed to run any `gh` commands. The coordinator posts the \
review on your behalf after your session ends.
- DO NOT run the project's test suite, build, or any other command — a human \
reviewer reads the diff, they don't run the suite, and on some projects (e.g. \
headless GUI apps) running it hangs the session. You MAY read project files \
for context. Build/test validation is the separate pre-merge smoke gate's job.
- You are NOT allowed to push commits or modify the PR's code. You only \
review.

How to review:
1. Read the project's CLAUDE.md for project conventions.
2. Read the PR diff using `git diff` or the briefing instructions.
3. Check the diff against the review checklist in your briefing.
4. For each finding, cite the specific file:line and the rule it violates.
5. Before you end your session, record your verdict TWICE — belt and \
braces, neither step substitutes for the other:

   a. PRIMARY (do this FIRST, if you can): if the environment variable \
`COORD_ASSIGNMENT_ID` is set, write your full findings to a file and run:
      `coord report-result --assignment "$COORD_ASSIGNMENT_ID" --status done \
--verdict approve|request-changes --body-file <file>`
      This writes your verdict straight to the coordinator's board — the \
authoritative record. Check the command's output for a confirmation; if it \
errors, or `COORD_ASSIGNMENT_ID` is unset, or `coord` is not on your PATH, \
say so plainly and fall through to step b anyway — it is REQUIRED \
regardless.
   b. BACKUP (always do this too, even after a successful step a): at the \
END of your session, output your verdict in this exact format:

REVIEW_VERDICT: approve
REVIEW_BODY:
<your full review text in markdown>
END_REVIEW

Or for requesting changes:

REVIEW_VERDICT: request-changes
REVIEW_BODY:
<your full review text in markdown>
END_REVIEW

This printed block is the PATH-independent fallback recovered from your \
session transcript even when step a never ran or failed — it is REQUIRED \
every time, not just when `coord report-result` is unavailable.

Structure the markdown body with these three headings, in this order, ALWAYS \
all three even when a section is empty — write `None` under a heading with \
nothing in it, and write every finding as a `- ` bullet, never as a bare \
paragraph:

## Blocking findings
## Non-blocking concerns
## Nits

The coordinator reads these sections to decide whether a `request-changes` is \
a real must-fix or advisory-only. It is deliberately conservative: a body it \
cannot read is treated as blocking, so an omitted or prose-only \
`## Blocking findings` section costs a full extra fix+review round even when \
you raised nothing blocking. Put blocking findings ONLY under \
`## Blocking findings` — anything you would still merge over belongs in one of \
the other two sections.

`END_REVIEW` is a HARD REQUIREMENT, not a formatting flourish: the coordinator \
only records a verdict when it sees that exact line, so a review that is \
otherwise complete and correct but stops one line early is discarded in its \
entirety — the same as if you had never reviewed at all. The LAST LINE of your \
LAST MESSAGE must be exactly `END_REVIEW` on its own line, with nothing after \
it. Do not stop as soon as your review prose feels finished; write the \
`END_REVIEW` line and then stop. Before you end your session, re-read your \
final message and confirm its last line is `END_REVIEW`.

If the diff is clean, approve — but be thorough first.\
"""


# ── Machine selection ───────────────────────────────────────────────────────

@dataclass
class ReviewerChoice:
    machine: Machine
    same_as_worker: bool
    rationale: str


# #697: how far past a row's OWN needs-attention threshold a `pending`/
# `running` row must sit before reviewer selection stops counting its machine
# as busy.  One hour of margin on top of a threshold that is itself measured
# in tens of minutes: comfortably longer than any reap path needs
# (`coord.reconcile._reconcile_no_agent_record` on the daemon's 30s tick,
# `coord.diagnose.sweep_dead_running_rows` on `coord-notify.timer`'s 5min
# cadence), so a row still counted busy here is one every reaper has already
# had many chances at.
STALE_BUSY_BUFFER_SECONDS = 3600.0

# The same horizon for rows whose type has NO wall-clock threshold at all —
# `PipelineConfig.attention_threshold_for` returns `inf` for the attended
# chat/troubleshoot/audit family (#1133).  Those are exactly the rows #697
# observed at 51h and 70h, so "never expires" is not an option here; a flat,
# deliberately generous day is.  A human really attending a chat session for
# a full day is far rarer than a tmux session that died without being reaped.
STALE_BUSY_INTERACTIVE_SECONDS = 24 * 3600.0


def _is_stale_busy_row(a: Assignment, config: Config, now: float) -> bool:
    """True when *a* is too old to still be believed as occupying its machine (#697).

    Reviewer selection's ``busy`` set used to count **every** ``pending``/
    ``running`` board row, which is how a zombie row broke dispatch in #697:
    rows whose worker had been dead for hours-to-days kept inflating the set,
    so real machines looked occupied and selection was pushed onto whatever
    candidate happened to look "free" — on #669 that was an *offline* machine,
    and the review could not be dispatched at all.

    This is deliberately **not** a liveness probe, and deliberately **not** a
    reap.  The reapers (``coord.reconcile._reconcile_no_agent_record``,
    ``coord.interactive.reap_stale_interactive_sessions``,
    ``coord.diagnose.sweep_dead_running_rows``) own the "is it actually dead?"
    question, they answer it with a positive disproof or a real tmux/agent
    probe, and they are the only things allowed to write a terminal status.
    Guessing from age alone would be #1870's mistake if it gated a *write*.

    Here it gates only a *ranking*, where being wrong is cheap and
    self-correcting in both directions:

    * Wrong about a genuinely-live row → its machine is ranked as idle and may
      receive the review.  That is the SAME outcome fallback 1
      ("different machine, currently busy — review will queue") already
      produces on a one-other-machine fleet, and a genuinely-saturated agent
      answers with a rejection that ``_ranked_reviewer_candidates`` already
      falls through on (#904).
    * Wrong about a dead row (age not yet reached) → status quo ante: the
      machine stays "busy" and merely ranks lower.

    A row with no ``dispatched_at`` is never stale — there is nothing to
    compute an age from, and this path never guesses, mirroring
    ``sweep_dead_running_rows``.
    """
    dispatched_at = getattr(a, "dispatched_at", None)
    if not dispatched_at:
        return False
    try:
        dispatched = float(dispatched_at)
    except (TypeError, ValueError):
        return False

    threshold = config.pipeline.attention_threshold_for(
        a.type or "work",
        provider_name=a.provider_name,
        review_of_assignment_id=a.review_of_assignment_id,
    )
    if threshold == float("inf"):
        horizon = STALE_BUSY_INTERACTIVE_SECONDS
    else:
        horizon = threshold + STALE_BUSY_BUFFER_SECONDS
    return (now - dispatched) > horizon


def busy_machine_names(
    board: Board,
    config: Config,
    *,
    now: float | None = None,
) -> set[str]:
    """Machines with at least one *believable* in-flight row (#697).

    The shared ``busy`` set for :func:`pick_reviewer_machine` and
    :func:`_ranked_reviewer_candidates` — a ``pending``/``running`` row whose
    age is past :func:`_is_stale_busy_row`'s horizon is presumed dead and no
    longer holds its machine down.  See that function for why an age-only
    heuristic is the right instrument *for a ranking* and the wrong one for a
    reap.
    """
    if now is None:
        now = time.time()
    return {
        a.machine_name
        for a in board.active
        if a.status in ("pending", "running")
        and not _is_stale_busy_row(a, config, now)
    }


def pick_reviewer_machine(
    worker_machine_name: str,
    repo_name: str,
    board: Board,
    config: Config,
) -> ReviewerChoice | None:
    """Pick a reviewer machine — different from the worker if possible.

    Independence comes from a fresh session with no shared context, not from
    physical machine separation, so a same-machine fallback still produces a
    useful review — but we warn the caller via `same_as_worker=True`.

    Returns None when no machine can handle this repo.

    #2240: the pause set here is `follow_on_paused_set()`, NOT `paused_set()`
    — a review is the tail of work that is already running, so a release
    cordon ("route no NEW work here") must not filter its host out. It did,
    on 2026-08-14, and the result was a fleet-wide 70-minute deadlock: the
    cordon blocked the review, the unreviewable entry stayed `running`, the
    running entry deferred the roll, and the deferred roll left the cordon
    up. Explicit pauses and quiet hours still apply.

    #697: the ``busy`` set is :func:`busy_machine_names`, which drops
    ``pending``/``running`` rows too old to still be believed — a zombie row
    used to make a real machine look occupied indefinitely and push selection
    onto an offline "free" candidate.
    """
    from coord.machine_pause import follow_on_paused_set
    paused = follow_on_paused_set(config.machines)
    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name) and m.name not in paused
    ]
    if not candidates:
        return None

    busy = busy_machine_names(board, config)

    different = [
        m for m in candidates
        if m.name != worker_machine_name and m.name not in busy
    ]
    if different:
        return ReviewerChoice(
            machine=different[0],
            same_as_worker=False,
            rationale=(
                f"chose {different[0].name} — different machine from worker "
                f"({worker_machine_name})"
            ),
        )

    # Fallback 1: any different machine, even if busy.
    different_busy = [m for m in candidates if m.name != worker_machine_name]
    if different_busy:
        return ReviewerChoice(
            machine=different_busy[0],
            same_as_worker=False,
            rationale=(
                f"chose {different_busy[0].name} — different machine from "
                f"worker, currently busy (review will queue)"
            ),
        )

    # Fallback 2: same machine (only one available). Reduced independence.
    same = next((m for m in candidates if m.name == worker_machine_name), None)
    if same is None:
        return None
    return ReviewerChoice(
        machine=same,
        same_as_worker=True,
        rationale=(
            f"only {worker_machine_name} can handle {repo_name}; using same "
            f"machine — reviewer session is fresh but not on separate hardware"
        ),
    )


def _ranked_reviewer_candidates(
    worker_machine_name: str,
    repo_name: str,
    board: Board,
    config: Config,
) -> list[tuple[Machine, bool]]:
    """Return **all** candidate reviewer machines in priority order.

    Each element is ``(machine, same_as_worker)``.  Priority mirrors
    ``pick_reviewer_machine``:

    1. Different from the worker, currently **idle** — best independence, no
       queue delay.
    2. Different from the worker, currently **busy** — independence preserved;
       the review will queue on that agent.
    3. **Same** machine as the worker — last resort; fresh session but no
       hardware separation.

    Returns an empty list when no configured machine handles *repo_name*.
    Used by ``dispatch_review`` to iterate candidates instead of committing to
    a single pick, so a rejected agent (e.g. a 400 from config drift) can
    fall through to the next rather than silently failing (#904).

    #2240: cordon-blind, like ``pick_reviewer_machine`` above and for the
    same reason — this is the function whose empty return produced the
    literal "no eligible reviewer machine configured for repo
    'claude-coordinator'" that a cordoned fleet answered every review
    dispatch with for 70 minutes.

    #697: shares :func:`busy_machine_names` with ``pick_reviewer_machine``, so
    tier 1 vs tier 2 here is decided on *believable* in-flight rows only.
    """
    from coord.machine_pause import follow_on_paused_set  # noqa: PLC0415

    paused = follow_on_paused_set(config.machines)
    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name) and m.name not in paused
    ]
    if not candidates:
        return []

    busy = busy_machine_names(board, config)

    result: list[tuple[Machine, bool]] = []
    for m in candidates:
        if m.name != worker_machine_name and m.name not in busy:
            result.append((m, False))   # different + idle
    for m in candidates:
        if m.name != worker_machine_name and m.name in busy:
            result.append((m, False))   # different + busy (will queue)
    for m in candidates:
        if m.name == worker_machine_name:
            result.append((m, True))    # same machine — last resort
    return result


# ── Briefing construction ───────────────────────────────────────────────────

def read_repo_claude_md(repo_path: Path) -> str | None:
    """Return the contents of CLAUDE.md at the repo root, or None.

    #2818: the review briefing no longer embeds this verbatim — see
    :func:`build_review_briefing`'s ``review_head_sha`` handling, which
    points the reviewer at ``git show <sha>:CLAUDE.md`` instead so the
    briefing stays cheap *and* pinned to the exact commit under review. This
    reader now only feeds the existence check (does this repo even have a
    CLAUDE.md?) and the fallback clamped-embed path for when no SHA is
    available.

    Public (no leading underscore) because #2462 reuses it from
    :mod:`coord.agent` — since worker dispatch switched to ``--bare``,
    Claude Code's own CLAUDE.md auto-discovery no longer runs for
    work-shaped legs, so ``default_worker_command`` embeds this the same
    defensive way the review briefing used to (that call site is unaffected
    by #2818 — a work leg has no ``review_head_sha`` equivalent to pin to).
    """
    candidate = repo_path / "CLAUDE.md"
    if not candidate.exists():
        return None
    try:
        return candidate.read_text()
    except OSError:
        return None


def _path_is_sealed(path: str, sealed: str) -> bool:
    """Does *path* fall under the sealed entry *sealed*?

    #1552: the sealed set is no longer uniformly directory prefixes. An
    entry ending in ``/`` (``tests/acceptance/``) is a prefix — everything
    beneath it is sealed. Anything else is a driver ``entrypoint:`` naming
    exactly one FILE (``tui/tests/acceptance.rs``), and must match exactly:
    a bare ``startswith`` would also swallow ``tui/tests/acceptance.rs.bak``
    and ``tui/tests/acceptance.rs.orig``, quietly widening the one narrow
    allowance a `test-author` gets.
    """
    if sealed.endswith("/"):
        return path.startswith(sealed)
    return path == sealed


def _sealed_to_sealed_rename_exemptions(
    diff_text: str, sealed_paths: list[str]
) -> set[str]:
    """Paths *diff_text* touches only as one side of a pure (100%-similarity)
    rename whose OTHER side is also sealed (#2896 review).

    Relocating a sealed slice from one declared-sealed location to another
    (e.g. ``tests/acceptance/ms-65/board_tabs_2282.rs`` ->
    ``tui/tests/acceptance/ms-65/board_tabs_2282.rs``, both covered by
    :meth:`coord.config.AcceptanceConfig.sealed_paths` once the driver's
    ``entrypoint:`` sibling dir is declared) is not tampering — content is
    byte-identical, confirmed by git itself
    (:func:`coord.github_ops.diff_pure_renames`), not merely claimed by the
    diff. A rename that moves sealed content somewhere UNSEALED, or brings
    unsealed content INTO the sealed tree, still trips the check on the
    unsealed side — this only exempts the narrow case where both endpoints
    were already part of the oracle. A content-changing edit at either path
    (no ``rename from``/``rename to`` + ``similarity index 100%`` in the
    diff) is never exempted, no matter how similar the paths look.
    """
    exempt: set[str] = set()
    for old, new in github_ops.diff_pure_renames(diff_text):
        old_sealed = any(_path_is_sealed(old, s) for s in sealed_paths)
        new_sealed = any(_path_is_sealed(new, s) for s in sealed_paths)
        if old_sealed and new_sealed:
            exempt.add(old)
            exempt.add(new)
    return exempt


def _diff_touched_sealed_paths(diff_text: str, sealed_paths: list[str]) -> list[str]:
    """Return the sealed path prefixes actually touched by *diff_text*.

    Cheap, dependency-free tamper detection (#944 sealing v1). Pure function,
    easy to test. #2896: excludes a path that's only present as one side of
    a sealed-to-sealed pure rename (see
    :func:`_sealed_to_sealed_rename_exemptions`) — everything else about the
    check, including the exact-match rule for a driver ``entrypoint:`` file
    that's genuinely edited rather than renamed, is unchanged.
    """
    exempt = _sealed_to_sealed_rename_exemptions(diff_text, sealed_paths)
    touched: set[str] = set()
    for c in github_ops.diff_file_paths(diff_text):
        if c in exempt:
            continue
        for sealed in sealed_paths:
            if _path_is_sealed(c, sealed):
                touched.add(sealed)
    return sorted(touched)


def _diff_paths_outside_sealed(diff_text: str, sealed_paths: list[str]) -> list[str]:
    """Return diff file paths that fall OUTSIDE every sealed prefix.

    #1175: for a ``type="test-author"``/``"mock-author"`` PR, writing under
    *sealed_paths* (``tests/acceptance/ms-NN/**`` plus, #1552, each driver's
    declared ``entrypoint:``) is the assignment's entire job, not a
    violation — the oracle-tamper rule inverts for these types, so the
    reviewer needs the paths touched OUTSIDE the sealed prefix instead of
    the ones inside it.
    """
    return sorted(
        p for p in github_ops.diff_file_paths(diff_text)
        if not any(_path_is_sealed(p, sealed) for sealed in sealed_paths)
    )


# ── #2192: free pre-review "missing test" nudge ─────────────────────────────
#
# #2132 classified 27 request-changes verdicts on this repo and found 5/27
# (18.5%) were "missing required black-box test only" — the code was correct,
# but the diff changed user-visible behavior and shipped no test, which
# CLAUDE.md's "Testing — black-box coverage is the acceptance bar" section
# already requires. Each one cost a full paid review leg plus a fix +
# re-review round trip to catch a pattern a static check can see for free.
#
# This is intentionally a coarse, path-only heuristic — no diff semantics, no
# ``claude -p`` call, no LLM judgment, just the same cheap file-path
# inspection ``github_ops.diff_file_paths``/``_diff_touched_sealed_paths`` already do
# above. It is a NUDGE, not a gate: callers only ever log it, never act on it
# to change dispatch behavior, so a false positive costs nothing (the
# reviewer remains the sole authority and, per CLAUDE.md, already honors a
# PR that says it's a pure refactor / internal-only change).
#
# The caller (``dispatch_review``) logs the result at ``log.warning``, not
# ``log.info`` — this repo has no ``logging.basicConfig`` anywhere outside
# tests, so the root logger stays at the default WARNING floor with no
# handler attached and an INFO record never reaches anyone. WARNING clears
# that floor and reaches Python's handler-of-last-resort (stderr) with zero
# configuration. See the comment at the call site for the empirical check.

_TEST_BASENAME_PREFIXES = ("test_",)
_TEST_BASENAME_SUFFIXES = (
    "_test.py", "_test.rs",
    ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx",
)
_TEST_BASENAME_EXACT = ("conftest.py",)
# Matched against the path with a leading "/" prepended, so both a
# repo-root prefix ("tests/foo.py" -> "/tests/foo.py") and a nested
# directory ("tui/tests/foo.rs" -> "/tui/tests/foo.rs") are caught by the
# same substring check.
_TEST_PATH_SEGMENTS = ("/tests/", "/test/", "/__tests__/", "/e2e/")

# Paths under these prefixes are internal tooling/docs, never shipped
# product behavior — CLAUDE.md's "internal-only changes are exempt" carve-out,
# expressed as the only thing a pure diff inspection can see: file location.
_NON_USER_VISIBLE_PATH_PREFIXES = (
    "docs/", "scripts/", "deploy/", ".github/", ".claude/", "graphify-out/",
)
_USER_VISIBLE_SUFFIXES = (".py", ".rs", ".ts", ".tsx", ".js", ".jsx")


def _is_test_path(path: str) -> bool:
    """Does *path* look like a test file, by name/location alone?

    NOTE (#2192 review follow-up): ``coord/split_work.py``'s ``_is_test_file``
    answers a similarly-shaped question with a narrower, Python-only rule
    set, for chunk-splitting rather than this module's review nudge. See
    that function's docstring for why the two aren't unified yet.
    """
    lower = path.lower()
    base = lower.rsplit("/", 1)[-1]
    if base in _TEST_BASENAME_EXACT:
        return True
    if any(base.startswith(p) for p in _TEST_BASENAME_PREFIXES):
        return True
    if any(base.endswith(s) for s in _TEST_BASENAME_SUFFIXES):
        return True
    return any(seg in f"/{lower}" for seg in _TEST_PATH_SEGMENTS)


def _is_user_visible_path(path: str) -> bool:
    """Does *path* look like shipped source (not test, not internal tooling)?"""
    lower = path.lower()
    if _is_test_path(lower):
        return False
    if any(lower.startswith(p) for p in _NON_USER_VISIBLE_PATH_PREFIXES):
        return False
    return any(lower.endswith(s) for s in _USER_VISIBLE_SUFFIXES)


def diff_missing_test_coverage(diff_text: str | None) -> bool:
    """#2192: True when *diff_text* touches user-visible source and zero
    test files, the pattern behind 18.5% of #2132's blocking reviews.

    Pure, free, deterministic path inspection — reuses
    :func:`coord.github_ops.diff_file_paths`. Returns False (never flags)
    when the diff is empty, touches no recognized source file
    (docs-only/internal-tooling-only diffs — CLAUDE.md's
    internal-only exemption), or already includes a test-file change.

    The CLAUDE.md exemption is honored only at the granularity a path-only
    heuristic can see: whole file *types/locations* (``docs/``, ``scripts/``,
    non-source suffixes, ...), not genuine behavior-preserving refactors of
    shipped source. A true pure refactor of a ``.py``/``.rs``/``.ts`` file
    (e.g. renaming a local variable) with no test changes still flags —
    harmless today since the caller only ever logs this, never gates on it,
    but worth knowing if a caller ever starts acting on the result: at that
    point this function would honor a narrower exemption than CLAUDE.md's
    own text, which is about intent ("pure refactor / internal-only"), not
    file location.
    """
    if not diff_text or not diff_text.strip():
        return False
    paths = github_ops.diff_file_paths(diff_text)
    if not any(_is_user_visible_path(p) for p in paths):
        return False
    return not any(_is_test_path(p) for p in paths)


def repo_focus_lines(reviews_cfg: ReviewsConfig, repo_name: str) -> list[str]:
    """Return the ``### Repo-specific focus`` block for *repo_name*, or ``[]``
    if the repo has no ``reviews.repo_overrides`` configured (#3112).

    Shared by :func:`build_review_briefing` (the reviewer's copy) and
    :func:`coord.dispatch.dispatch`'s work-briefing chokepoint (the worker's
    copy) so the two can never drift. Before #3112, ``repo_overrides`` was
    read in exactly one module (``coord/review.py``) — workers were graded
    against rules they were never shown, and vimcode's most-failed rule
    (state that a black-box test was observed RED against unfixed develop)
    pointed at a PR body workers structurally cannot write. A worker that
    can read the exact rubric it is graded against can satisfy it in the
    surface it actually owns (its own commits/summary) instead.

    Returns a list starting with a blank line (matching every other
    optional-section append pattern in :func:`build_review_briefing`) so a
    caller can simply ``lines.extend(...)`` — and ``[]`` (no dangling blank
    line or empty heading) when there is nothing to say.
    """
    overrides = reviews_cfg.repo_overrides.get(repo_name, [])
    if not overrides:
        return []
    lines = ["", f"### Repo-specific focus ({repo_name})"]
    for item in overrides:
        lines.append(f"- {item}")
    return lines


def build_review_briefing(
    *,
    pr_number: int | None,
    pr_url: str | None,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    branch: str | None,
    worker_machine: str,
    same_as_worker: bool,
    reviews_cfg: ReviewsConfig,
    repo_claude_md: str | None,
    review_head_sha: str | None = None,
    default_branch: str = "main",
    review_iteration: int = 0,
    diff_text: str | None = None,
    sealed_paths: list[str] | None = None,
    sealed_entrypoints: list[str] | None = None,
    coordinator_doc_paths: list[str] | None = None,
    assignment_type: str = "work",
    provider_same_as_worker: bool = False,
    review_provider: str | None = None,
    completion_summary: str | None = None,
    commit_messages: list[str] | None = None,
) -> str:
    """Assemble the reviewer's prompt. Pure function — easy to test.

    *provider_same_as_worker* (#1811) is True when the review's resolved
    provider (``review_provider``) is the same name as the worker's own
    resolved provider — e.g. a repo pinned to ``opencode`` with no
    ``reviews.provider`` override, so the review inherits it too. Provider
    co-location is a *larger* loss of independence than machine co-location
    (``same_as_worker`` below): a fresh session removes shared context, but
    not shared blind spots. When True, a note is appended mirroring
    ``same_as_worker``'s — the reviewer is told to be extra rigorous for the
    same reason a same-machine reviewer is.

    When *review_iteration* > 0 the work is a re-review of a fix worker's
    commits (a prior round requested changes). The "What to do" section is
    then scoped to the fix delta instead of the whole PR (#476): re-reviewing
    the entire PR every round repeats work, wastes tokens, and surfaces fresh
    non-blocking nits that bounce an already-correct PR into another fix cycle.

    When *diff_text* is non-empty (#612) the coordinator has already computed
    the merge-base (three-dot) diff and it is embedded verbatim, so the
    reviewer reviews exactly the branch's own changes — there is nothing for it
    to get wrong. A reviewer that deviates to a two-dot/stale-base diff would
    surface code merged to the default branch *after* the branch was cut as
    spurious deletions and flag it as a regression (#546). When *diff_text* is
    None the existing three-dot ``git diff`` fallback instructions stand.

    *sealed_paths* (#944, docs/ORACLE_LOOP.md sealing v1) lists the paths the
    worker must never touch — ``tests/acceptance/`` plus, since #1552, each
    acceptance driver's declared ``entrypoint:``, derived from the driver
    definition by :meth:`coord.config.AcceptanceConfig.sealed_paths`. When
    non-empty a reviewer instruction is always appended; if *diff_text* is
    also given and actually touches one of the paths, a blocking "TAMPER
    DETECTED" banner is prepended instead of a soft reminder — this is the
    "reviewer flags any diff that touches tests/acceptance/**"
    tamper-detection policy.

    *repo_claude_md* (#2818) is only used as an existence probe now — the
    briefing no longer embeds it verbatim. When *review_head_sha* is given
    (the normal case: it's captured once per review dispatch before this is
    called), the CLAUDE.md section instead tells the reviewer to run
    ``git show <review_head_sha>:CLAUDE.md`` in its own worktree — the
    reviewer already has ``Read``/``Bash`` and sits in a checkout of this
    repo, so fetching by SHA costs one cheap tool call instead of ~6.4k
    tokens of immutable-prefix text repeated on every turn of every review
    round. It is also *more* correct than the old embed: it pins to the
    exact commit under review rather than to whatever the coordinator's own
    checkout happened to hold at dispatch time. When *review_head_sha* is
    unavailable (SHA fetch failed — see the fail-safe ``try/except`` around
    ``branch_sha_fetcher`` in :func:`dispatch_review`), this falls back to
    embedding *repo_claude_md* directly, clamped to
    :data:`coord.refine_chat.MAX_CLAUDE_MD_CHARS` the same way
    ``refine_chat``/``test_chat`` already do — cheaper than the old
    unclamped embed, and a functioning fallback beats no rules at all.

    *sealed_entrypoints* (#1552) is the subset of *sealed_paths* that are
    driver entry points rather than the sealed tree itself. They get a
    narrower rule in the author branch below: a slice file is invisible to
    an entry-point-linked runner (``cargo test --test acceptance``) until
    something registers it in the crate root, so a ``test-author`` ADDING a
    registration line there is doing its job, while rewriting or deleting
    what is already in the file is still tamper.

    *assignment_type* (#1175) gates which direction that rule runs. For
    :data:`coord.models.SEALED_PATH_AUTHOR_TYPES` (``"test-author"``,
    ``"mock-author"``) — whose entire job IS to write under *sealed_paths* —
    the rule inverts: mandatory ``request-changes`` fires only when the diff
    touches something OUTSIDE *sealed_paths*; touching only the sealed area
    is expected and non-blocking. Every other type (default ``"work"``) keeps
    the original rule unchanged: any touch to *sealed_paths* is mandatory
    ``request-changes``.

    *coordinator_doc_paths* (#2966) is the repo's coordinator-owned doc set —
    :func:`coord.models.coordinator_owned_docs`, the repo's own CLAUDE.md plus
    anything it additionally lists under ``coordinator_only_files``. CLAUDE.md
    already states "only the coordinator writes docs" for every worker, but
    that was prose-only: nothing structurally backstopped it, and two workers
    independently rewriting the same CLAUDE.md section produced a semantic
    merge conflict that stalled a five-issue chain (#2966). Unlike
    *sealed_paths*, this rule never inverts — no assignment type's job is
    ever editing the repo's own rulebook, so any diff that touches one of
    these paths is mandatory ``request-changes`` regardless of
    *assignment_type*.

    *completion_summary* and *commit_messages* (#3112) are the worker's own
    claims about its work — the prose extracted from its "### Summary" block
    (:data:`coord.models.Assignment.completion_summary`) and the PR's commit
    messages (:func:`coord.github_ops.get_pr_commit_messages`). Before #3112
    the reviewer had access to nothing the worker *said*, only the diff —
    so a repo-override rule that requires a specific claim (e.g. vimcode's
    "state that the new test was observed RED against unfixed develop")
    was unverifiable by the reviewer even when the worker made the claim
    somewhere, because nothing carried it into this briefing. Both are
    optional and rendered only when non-empty; when both are empty this adds
    no section at all (mirrors every other optional block here). Each commit
    message is embedded in full (headline + body, not just the headline) and
    clamped to :data:`MAX_COMMIT_MESSAGE_CHARS`; the list itself is capped to
    the newest :data:`MAX_COMMIT_MESSAGES` entries — the same "clamp every
    embed" discipline as ``MAX_CLAUDE_MD_CHARS``/``truncate_diff_text`` above,
    since a multi-round fix-review branch can otherwise grow this section
    without bound.
    """

    lines: list[str] = []
    lines.append(f"# Review assignment: {repo_github} PR #{pr_number}")
    lines.append("")
    lines.append(f"You are reviewing the worker's work on issue #{issue_number}: {issue_title}")
    lines.append("")
    lines.append("## Context")
    lines.append(f"- Repo: {repo_github} (local name: {repo_name})")
    lines.append(f"- Branch: {branch or '(unknown)'}")
    if pr_url:
        lines.append(f"- PR URL: {pr_url}")
    lines.append(f"- Worker machine: {worker_machine}")
    if same_as_worker:
        lines.append(
            "- NOTE: only one machine is configured for this repo, so you are "
            "running on the same machine as the worker. Your session is still "
            "fresh (no shared context), but be extra rigorous."
        )
    if provider_same_as_worker:
        lines.append(
            f"- NOTE: this review is running on the same provider "
            f"({review_provider or 'claude'}) as the worker's own dispatch. "
            "Your session is still fresh (no shared context), but a shared "
            "model family means shared blind spots — be extra rigorous."
        )
    lines.append("")

    lines.append("## Issue")
    lines.append(f"**#{issue_number}: {issue_title}**")
    if issue_body.strip():
        lines.append("")
        lines.append(issue_body.strip())
    lines.append("")

    if repo_claude_md:
        lines.append("## Project rules (from CLAUDE.md)")
        lines.append("")
        if review_head_sha:
            # #2818: fetch by SHA instead of embedding — cheaper (no 6.4k+
            # char copy sitting in the immutable prefix, re-paid every turn
            # and every re-review round) and more correct (pinned to the
            # exact commit under review, not to the coordinator's own
            # checkout at dispatch time).
            lines.append(
                f"Run `git show {review_head_sha}:CLAUDE.md` in this "
                "worktree and read the output before reviewing — those are "
                "the project rules pinned to the exact commit under review. "
                "(If that command errors, CLAUDE.md doesn't exist at this "
                "commit — proceed with no repo-specific rules.)"
            )
        else:
            # Fallback: the branch-head SHA fetch failed (see
            # dispatch_review's fail-safe try/except), so there's nothing to
            # pin a `git show` to. Embed the coordinator's own on-disk copy
            # instead, clamped the same way refine_chat/test_chat already
            # do for the same reason — an unclamped embed is exactly #2818.
            clamped = repo_claude_md.strip()
            if len(clamped) > MAX_CLAUDE_MD_CHARS:
                clamped = clamped[:MAX_CLAUDE_MD_CHARS] + "\n…[truncated]"
            lines.append(clamped)
        lines.append("")

    lines.append("## Review checklist")
    lines.append("")
    if reviews_cfg.checklist:
        for item in reviews_cfg.checklist:
            lines.append(f"- {item}")
    else:
        lines.append("- Does the diff actually solve issue #" + str(issue_number) + "?")
        lines.append("- Do tests pass? Any regressions?")
        lines.append("- Are there CLAUDE.md violations?")
        lines.append("- Did the worker stay within the assigned file scope?")
        lines.append("- Any security issues (injection, auth bypass, credential exposure)?")

    lines.extend(repo_focus_lines(reviews_cfg, repo_name))

    if reviews_cfg.reviewer_prompt.strip():
        lines.append("")
        lines.append("## Additional instructions")
        lines.append(reviews_cfg.reviewer_prompt.strip())

    _summary = (completion_summary or "").strip()
    _commits = [m.strip() for m in (commit_messages or []) if m and m.strip()]
    if _summary or _commits:
        # #3112: the worker's own claims about its work — unverified prose,
        # not evidence, but a place a repo-override rule (e.g. vimcode's
        # "state the test was observed RED") can actually be satisfied and
        # checked, instead of pointing at a PR body the worker cannot write.
        lines.append("")
        lines.append("## Worker's own claims (unverified — cross-check against the diff)")
        lines.append("")
        lines.append(
            "The worker cannot edit the PR description or post GitHub "
            "comments directly, so this is everything it said about its own "
            "work. Treat it as a claim to verify, not as evidence on its own "
            "— but a repo-specific rule that requires the worker to *state* "
            "something (e.g. that a new test was observed failing against "
            "unfixed code) is satisfied or violated here, not in the PR body."
        )
        if _summary:
            lines.append("")
            lines.append("### Completion summary")
            lines.append("")
            lines.append(_summary)
        if _commits:
            lines.append("")
            lines.append("### Commit messages")
            lines.append("")
            # get_pr_commit_messages returns commits in chronological order
            # (oldest first) — keep the *newest* ones when capping, since a
            # later commit (e.g. a fix-review round addressing a repo-override
            # rule) is more likely to carry the claim a reviewer needs than
            # the branch's earliest commit.
            omitted_commits = max(0, len(_commits) - MAX_COMMIT_MESSAGES)
            shown_commits = _commits[omitted_commits:]
            for msg in shown_commits:
                # Keep the full message (headline + body), not just the
                # headline — the body is exactly where a worker states a
                # multi-sentence claim (e.g. "Observed RED against unfixed
                # develop."). Render the headline as the bullet and the body
                # as an indented blockquote so multi-line messages stay
                # readable instead of one giant run-on bullet.
                clamped = msg
                if len(clamped) > MAX_COMMIT_MESSAGE_CHARS:
                    clamped = clamped[:MAX_COMMIT_MESSAGE_CHARS] + "\n…[truncated]"
                msg_lines = clamped.splitlines()
                headline, body_lines = msg_lines[0], msg_lines[1:]
                lines.append(f"- {headline}")
                for body_line in body_lines:
                    stripped = body_line.strip()
                    if stripped:
                        lines.append(f"  > {stripped}")
            if omitted_commits > 0:
                lines.append(
                    f"- …and {omitted_commits} earlier commit message(s) omitted "
                    "— inspect the branch's full log directly if needed."
                )

    if diff_text and diff_text.strip():
        # #612: embed the merge-base (three-dot) diff verbatim so the reviewer
        # has nothing to compute — a two-dot/stale-base diff would show
        # already-merged commits as spurious deletions (#546).
        lines.append("")
        lines.append("## Diff to review (authoritative)")
        lines.append(
            "This is the merge-base (three-dot) diff — exactly the branch's own "
            "changes, nothing else. Review THIS. Do NOT compute your own diff; a "
            "two-dot or stale-base diff would show unrelated already-merged "
            "commits as spurious deletions."
        )
        lines.append("")
        lines.append("```diff")
        lines.append(diff_text.strip())
        lines.append("```")

    if sealed_paths:
        lines.append("")
        if assignment_type in SEALED_PATH_AUTHOR_TYPES:
            # #1175: for test-author/mock-author, writing under sealed_paths
            # IS the job — the tamper rule inverts. Flag only a touch OUTSIDE
            # the sealed area; a diff confined to it is expected, not tamper.
            outside = _diff_paths_outside_sealed(diff_text, sealed_paths) if diff_text else []
            if outside:
                lines.append("## \U0001f6a8 SEALED ORACLE SCOPE VIOLATION")
                lines.append("")
                lines.append(
                    f"This is a `type={assignment_type!r}` assignment — its entire "
                    "job is authoring under this repo's sealed acceptance oracle "
                    + ", ".join(f"`{p}`" for p in sealed_paths)
                    + " (docs/ORACLE_LOOP.md), so touching those paths is expected "
                    "and NOT tamper. But this diff ALSO touches path(s) OUTSIDE the "
                    "sealed area: " + ", ".join(f"`{p}`" for p in outside) + ". "
                    "**request-changes is mandatory here**, regardless of anything "
                    "else in this diff — this assignment type must touch ONLY the "
                    "sealed acceptance tree and nothing else."
                )
            else:
                lines.append(
                    f"## Sealed paths (expected writes for type={assignment_type!r})"
                )
                lines.append("")
                lines.append(
                    f"This is a `type={assignment_type!r}` assignment: writing under "
                    + ", ".join(f"`{p}`" for p in sealed_paths)
                    + " (docs/ORACLE_LOOP.md) is its entire job, not a tamper "
                    "violation. Do **not** request-changes solely because this "
                    "diff touches the sealed acceptance tree — only flag it if the "
                    "diff also touches anything outside that tree."
                )
            if sealed_entrypoints:
                # #1552: the entry point is sealed, but the allowance on it is
                # narrower than on the suite dir — additive registration only.
                lines.append("")
                lines.append("### Driver entry point — additive registration only")
                lines.append("")
                lines.append(
                    ", ".join(f"`{p}`" for p in sealed_entrypoints)
                    + " is this repo's acceptance driver **entry point** "
                    "(declared as `entrypoint:` on the driver in "
                    "coordinator.yml, #1552) — the crate root the runner "
                    "links slices through, and part of the sealed oracle for "
                    "that reason. A slice file under the sealed tree is "
                    "INVISIBLE to the runner until it is registered there "
                    "(e.g. an `include!(...)` line), so a slice with no "
                    "registration line is dead code that never executes."
                )
                lines.append("")
                lines.append(
                    "- **Expected, do NOT flag:** this diff ADDS registration "
                    "lines for its own new slice files."
                )
                lines.append(
                    "- **request-changes:** the entry-point hunk does anything "
                    "more than that — rewriting, reordering, or deleting "
                    "existing lines, registering files that are not part of "
                    "this slice, or any other edit to that file."
                )
                lines.append(
                    "- **request-changes:** the diff adds slice files under the "
                    "sealed tree but does NOT register them in the entry "
                    "point. Deleting the registration line to make a diff look "
                    "clean is not a fix — it ships a suite that silently "
                    "contributes zero tests."
                )
        else:
            touched = _diff_touched_sealed_paths(diff_text, sealed_paths) if diff_text else []
            if touched:
                lines.append("## \U0001f6a8 SEALED ORACLE TAMPER DETECTED")
                lines.append("")
                lines.append(
                    "The diff modifies a path SEALED by this repo's acceptance "
                    "oracle (docs/ORACLE_LOOP.md sealing v1): "
                    + ", ".join(f"`{p}`" for p in touched)
                    + ". The suite under these paths is authored independently — "
                    "workers may only RUN it (`coord acceptance run`), never read "
                    "or edit it. **request-changes is mandatory here**, regardless "
                    "of anything else in this diff."
                )
            else:
                lines.append("## Sealed paths (do not touch)")
                lines.append("")
                lines.append(
                    "This repo's acceptance oracle is sealed by policy: "
                    + ", ".join(f"`{p}`" for p in sealed_paths)
                    + ". If the diff modifies any of them, **request-changes** — "
                    "this is a hard rule, not a suggestion (docs/ORACLE_LOOP.md)."
                )

    if coordinator_doc_paths:
        # #2966: "only the coordinator writes docs" was prose-only — nothing
        # backstopped it structurally, and two workers independently
        # rewrote the same CLAUDE.md section in the same week, producing a
        # semantic (prose) merge conflict that stalled a five-issue chain.
        # This rule never inverts by assignment_type (unlike sealed_paths
        # above): no dispatched worker type's job is ever editing the
        # repo's own rulebook.
        lines.append("")
        touched_docs = (
            _diff_touched_sealed_paths(diff_text, coordinator_doc_paths)
            if diff_text else []
        )
        if touched_docs:
            lines.append("## \U0001f6a8 COORDINATOR-OWNED DOC EDITED")
            lines.append("")
            lines.append(
                "The diff modifies a coordinator-owned doc: "
                + ", ".join(f"`{p}`" for p in touched_docs)
                + ". \"Only the coordinator writes docs\" — a worker must "
                "never edit the repo's own rulebook or other shared docs; "
                "parallel doc edits from independent workers collide "
                "(#2966). **request-changes is mandatory here**, regardless "
                "of anything else in this diff — even if the edit itself "
                "looks correct in isolation."
            )
        else:
            lines.append("## Coordinator-owned docs (do not touch)")
            lines.append("")
            lines.append(
                "This repo reserves "
                + ", ".join(f"`{p}`" for p in coordinator_doc_paths)
                + " for the coordinator (#2966). If the diff modifies any of "
                "them, **request-changes** — this is a hard rule, not a "
                "suggestion, regardless of assignment type."
            )

    lines.append("")
    lines.append("## What to do")
    lines.append("")
    if review_iteration > 0:
        # #476: re-review. A prior round requested changes and the worker
        # pushed fix commits. Scope to the fix delta — do NOT re-review the
        # whole PR from scratch, and do NOT raise NEW non-blocking nits on
        # already-accepted code. Only a genuine bug or an unaddressed
        # previously-requested change should block.
        lines.append(
            f"**This is re-review iteration {review_iteration}.** A previous "
            "review requested changes and the worker has pushed fix commits "
            "since then. Scope your review to those fixes — do NOT re-review "
            "the entire PR from scratch."
        )
        lines.append("")
        lines.append(
            "1. See what changed since the last review: "
            f"`git fetch origin && git log --oneline origin/{default_branch}..."
            f"origin/{branch or 'HEAD'}`. The most recent commit(s) are the fix "
            "for the last review round — concentrate there."
        )
        lines.append(
            "2. Verify the previously-requested changes were correctly made and "
            "that the fix commits introduce no regressions."
        )
        lines.append(
            "3. **Do NOT raise new non-blocking nits on unchanged, "
            "already-reviewed code.** Block (`request-changes`) ONLY for a "
            "genuine bug or a previously-requested change that was not "
            "addressed. If the fix is correct and you only have minor polish "
            "suggestions, **approve** and list them as non-blocking notes — "
            "the coordinator will not dispatch another fix round for "
            "non-blocking findings."
        )
        lines.append(
            "4. **A finding in the issue-context digest above marked "
            "`✅ RESOLVED` is settled, not outstanding** — a previous review "
            "round approved the issue with that item explicitly carried "
            "forward and waived (e.g. a worker-unfixable AC). It is no longer "
            "\"a previously-requested change that was not addressed\"; do "
            "not re-raise it as blocking unless you have independently found "
            "it broken again in the current diff."
        )
    elif pr_number is not None:
        if diff_text and diff_text.strip():
            lines.append(
                "1. Review the diff in the '## Diff to review' section above "
                "(already fetched for you — the merge-base diff)."
            )
        else:
            lines.append(
                f"1. Get the diff: `git fetch origin && git diff origin/{default_branch}..."
                f"origin/{branch or 'HEAD'}` or ask the coordinator for the diff."
            )
        lines.append("2. Run the project's test suite.")
        lines.append("3. Review the diff against the checklist above.")
    else:
        if diff_text and diff_text.strip():
            lines.append(
                "1. Review the diff in the '## Diff to review' section above "
                "(already fetched for you — the merge-base diff)."
            )
        else:
            lines.append(
                f"1. The worker pushed branch `{branch}` but no PR was opened. "
                f"Get the diff: `git fetch origin && git diff origin/{default_branch}..."
                f"origin/{branch or '<branch>'}`. Always diff against `origin/` after "
                "fetching — a local base ref may be stale and would sweep in unrelated "
                "already-merged commits."
            )
        lines.append("2. Run the project's test suite.")
        lines.append("3. Review the diff against the checklist above.")
    lines.append("")
    lines.append(
        "4. Before you end your session, record your verdict TWICE — belt "
        "and braces, neither step substitutes for the other. FIRST, if the "
        "environment variable `COORD_ASSIGNMENT_ID` is set, write your full "
        "findings to a file and run `coord report-result --assignment "
        '"$COORD_ASSIGNMENT_ID" --status done --verdict '
        "approve|request-changes --body-file <file>` — this writes straight "
        "to the coordinator's board and is the authoritative record. If "
        "`COORD_ASSIGNMENT_ID` is unset, `coord` errors, or it's not on "
        "your PATH, say so plainly and move on to the required backup "
        "below regardless. THEN, at the END of your session, ALWAYS ALSO "
        "output your findings in this exact format as the PATH-independent "
        "backup (the coordinator will post the review to GitHub on your "
        "behalf — do NOT run any `gh` commands):"
    )
    lines.append("")
    lines.append("```")
    lines.append("REVIEW_VERDICT: approve")
    lines.append("REVIEW_BODY:")
    lines.append("<your full review text in markdown>")
    lines.append("END_REVIEW")
    lines.append("```")
    lines.append("")
    lines.append("Use `REVIEW_VERDICT: request-changes` if changes are needed.")
    # #1456: the coordinator's #476 gate (an advisory-only request-changes must
    # not burn another fix round) counts bullets under the body's section
    # headings, and since #1456 it fails CLOSED — an unparseable body keeps the
    # reviewer's verdict verbatim. Say so here as well as in
    # REVIEWER_SYSTEM_PROMPT: without an explicit blocking section the gate can
    # never fire, so every advisory review costs a full fix+re-review round.
    lines.append("")
    lines.append(
        "BODY STRUCTURE — the markdown body MUST use these three headings, "
        "always all three, with every finding as a `- ` bullet under one of "
        "them: `## Blocking findings`, `## Non-blocking concerns`, `## Nits`. "
        "Write the single line `None.` under a heading with nothing under it. "
        "These sections are machine-counted: an explicitly empty blocking "
        "section is how you tell the coordinator your objections are advisory "
        "and no fix round is needed, and a body it cannot read is treated as "
        "blocking. Never state a blocking objection only in prose outside "
        "these sections."
    )
    # #1346: the three marker lines are a machine contract, not prose. The
    # surrounding briefing is Markdown and the body placeholder invites
    # Markdown, so reviewers have emitted `**REVIEW_VERDICT: request-changes**`
    # — which the parser rejected outright, silently dropping a complete
    # review. State the constraint and show the failing string; a negative
    # example is what actually stops the drift.
    lines.append("")
    lines.append(
        "FORMAT CONTRACT — the three marker lines "
        "(`REVIEW_VERDICT:`, `REVIEW_BODY:`, `END_REVIEW`) are parsed by "
        "machine. Each must start at the beginning of its own line as "
        "literal plain text, with NO Markdown decoration: no `**bold**`, no "
        "backticks, no `#` heading marks, no list bullet. "
        "`**REVIEW_VERDICT: request-changes**` is WRONG. "
        "`REVIEW_VERDICT: request-changes` is right. The review BODY between "
        "the markers may be Markdown — the marker lines may not. "
        "`END_REVIEW` is a HARD REQUIREMENT: an otherwise-complete, correct "
        "review with no `END_REVIEW` line is discarded in its entirety, not "
        "recorded with a best guess — so write `END_REVIEW` even if your "
        "review prose already feels finished. Before you finish, re-read "
        "your last message and confirm the verdict line begins with "
        "`REVIEW_VERDICT:` with nothing preceding it, AND that the very "
        "last line is `END_REVIEW`."
    )

    return "\n".join(lines)


# ── Dispatch ────────────────────────────────────────────────────────────────

def _find_or_open_pr(
    repo_github: str,
    *,
    branch: str,
    default_branch: str,
    issue_number: int,
    issue_title: str,
    assignment_type: str = "work",
) -> dict | None:
    """Return {number, url, existed} for a PR on `branch`, opening one if needed.

    Returns None when neither lookup nor open works — caller continues without
    a PR-targeted review (falls back to branch-diff review).

    *assignment_type* decides the PR-body keyword (#1077): for types in
    :data:`coord.models.CLOSES_ISSUE_TYPES` (``"work"``), ``issue_number`` is
    the issue this PR resolves, so the body carries the closing keyword
    ``Closes #N`` and GitHub auto-closes it on merge. For any other
    WORK_LIKE type — notably ``"mock-author"`` (Gate A), whose
    ``issue_number`` is the milestone's *tracking* issue, not something the
    PR resolves — the body uses the non-closing ``Refs #N`` so the tracking
    issue still gets a discoverable backlink but does not flip to closed
    when the contract PR merges.
    """
    try:
        existing = github_ops.find_pr_for_branch(repo_github, branch)
    except RuntimeError:
        existing = None
    if existing is not None:
        return {
            "number": existing["number"],
            "url": existing.get("url"),
            "existed": True,
        }
    keyword = "Closes" if assignment_type in CLOSES_ISSUE_TYPES else "Refs"
    try:
        return github_ops.create_pr(
            repo_github,
            base=default_branch,
            head=branch,
            title=f"#{issue_number}: {issue_title}",
            body=(
                f"{keyword} #{issue_number}\n\n"
                f"Automated PR opened by coordinator for review of issue #{issue_number}."
            ),
        )
    except RuntimeError:
        return None


def _resolve_pr_base_branch(
    completed: Assignment,
    repo,
    *,
    milestone_fetcher=None,
) -> str:
    """Resolve the PR base branch for `completed` (#1077's develop_branch /
    milestone routing).

    Shared by :func:`dispatch_review` and :func:`dispatch_pending_pr_opens`
    (#2844) so an early PR-open and the later review dispatch always agree
    on the base — opening against the wrong base would surface as a PR that
    `find_pr_for_branch` still finds, but whose diff nobody intended.
    """
    base_branch = repo.default_branch
    if getattr(repo, "develop_branch", None):
        from coord.branch_model import resolve_base_branch  # noqa: PLC0415

        fetch_milestone = milestone_fetcher or _fetch_issue_milestone_number
        milestone_number = fetch_milestone(repo.github, completed.issue_number)
        base_branch = resolve_base_branch(repo, milestone_number)
    return base_branch


def open_pr_for_completed_work(
    completed: Assignment,
    config: Config,
    *,
    pr_lookup=_find_or_open_pr,
    milestone_fetcher=None,
    commits_ahead_checker=None,
) -> dict | None:
    """Open (or find) the PR for `completed` as soon as its branch is pushed
    (#2844), instead of waiting until review dispatch — which itself waits
    for the Test/smoke leg to finish, ~20 minutes later.

    Opening this early is what lets GitHub's ``pull_request`` CI run overlap
    the smoke leg and the review leg instead of being serialised after both
    — the ~25-minute median dead time #2844 measured across 8 issues.

    Returns the ``{number, url, existed}`` dict `pr_lookup` (default
    :func:`_find_or_open_pr`) returns, or ``None`` when a PR should not be
    opened yet: no repo config, no branch, the branch has 0 commits ahead of
    its base (#1534's zero-commit gate, applied here too so an empty branch
    never gets a PR), or `pr_lookup` itself fails.

    Stores the resulting URL on ``completed.pr_url`` as a side effect, purely
    so a caller scanning the board can skip a row without a fresh GitHub
    round-trip (see :func:`dispatch_pending_pr_opens`). This is advisory
    only: `dispatch_review`'s own `pr_lookup` call and the merge phase's
    `create_pr` both re-resolve the PR from GitHub
    (``github_ops.find_pr_for_branch``) rather than trusting a cached field,
    so a stale or missing ``pr_url`` here can never cause a duplicate PR —
    it can only cost one redundant lookup.
    """
    repo = config.repo(completed.repo_name)
    if repo is None or not completed.branch:
        return None

    base_branch = _resolve_pr_base_branch(
        completed, repo, milestone_fetcher=milestone_fetcher
    )

    # #1534: same zero-commit gate `dispatch_review` applies — never open a
    # PR for a branch that (as far as GitHub can confirm) carries no commits
    # over its base. Fail-open: `commits_ahead_checker` returns `None` (never
    # a bare 0) on any lookup failure, so a transient `gh` blip can't block a
    # legitimate early PR-open.
    _ahead_check = commits_ahead_checker or github_ops.branch_commits_ahead
    _ahead = _ahead_check(repo.github, base_branch, completed.branch)
    if _ahead == 0:
        return None

    try:
        pr = pr_lookup(
            repo.github,
            branch=completed.branch,
            default_branch=base_branch,
            issue_number=completed.issue_number,
            issue_title=completed.issue_title,
            assignment_type=completed.type,
        )
    except Exception:  # noqa: BLE001 — best-effort; dispatch_review retries later
        log.warning(
            "[pr-open] %s: pr_lookup raised opening/finding a PR for branch "
            "%r — will retry from dispatch_review or the next pass",
            completed.assignment_id, completed.branch, exc_info=True,
        )
        return None

    if pr:
        completed.pr_url = pr.get("url")
    return pr


def dispatch_pending_pr_opens(
    board: Board,
    config: Config,
    *,
    pr_lookup=_find_or_open_pr,
    milestone_fetcher=None,
    commits_ahead_checker=None,
) -> list[Assignment]:
    """Bulk PR-open pass (#2844) — open a PR the instant a work leg pushes a
    branch, rather than waiting for review dispatch to do it after the
    Test/smoke leg finishes.

    Scans `board.completed` for work-like rows that are ``status="done"``
    (a work leg that never reaches "done" never appears here, so a failed
    work leg can never leave an orphan PR open — #2844's "failed work legs"
    requirement), carry a branch, and have no `pr_url` recorded yet. Both
    `reconcile()` and `coord notify` are expected to route through here every
    pass — the same bulk-choke-point shape as
    :func:`coord.smoke.dispatch_pending_smoke` and
    :func:`dispatch_pending_reviews` — so a row missed on an earlier pass (no
    repo config yet, a transient `gh` failure) is retried automatically.

    Gated on ``reviews.enabled``/``reviews.auto_dispatch``: this path exists
    solely to feed `dispatch_review`'s own PR lookup earlier, so when reviews
    are off there is no reason to open a PR ahead of anything.

    Idempotent by construction: `pr_lookup` (`_find_or_open_pr`) always
    checks `find_pr_for_branch` before creating, and both `dispatch_review`
    and the merge phase's `create_pr` do the same — so calling this every
    tick, including after review dispatch already opened the PR itself, only
    ever finds the existing one.

    Returns the `completed` `Assignment`s a PR was opened/found for this
    pass. The caller is responsible for persisting the board (only `pr_url`
    is mutated on `board`-owned rows).
    """
    if not (config.reviews.enabled and config.reviews.auto_dispatch):
        return []

    opened: list[Assignment] = []
    for completed in board.completed:
        if completed.type not in WORK_LIKE_TYPES:
            continue
        if completed.status != "done":
            continue
        if not completed.branch:
            continue
        if completed.pr_url:
            continue
        repo = config.repo(completed.repo_name)
        if repo is None:
            continue
        # #522-style chokepoint: never open a PR for work GitHub already
        # considers finished (issue closed / a PR already merged) — mirrors
        # dispatch_review's own terminal check so a stray late pass can't
        # reopen a PR against dead work.
        if github_ops.work_is_terminal(
            repo.github,
            completed.issue_number,
            completed.branch,
            trust_issue_closed=trust_issue_closed_for(completed.type),
        ):
            continue
        pr = open_pr_for_completed_work(
            completed,
            config,
            pr_lookup=pr_lookup,
            milestone_fetcher=milestone_fetcher,
            commits_ahead_checker=commits_ahead_checker,
        )
        if pr:
            opened.append(completed)

    return opened


def _fetch_agent_advertised_repos(
    host: str,
    port: int = AGENT_PORT,
    *,
    timeout: float = 2.0,
) -> list[str] | None:
    """Query an agent's ``/health`` endpoint and return the repos it handles.

    Returns a list of repo names (strings) when the agent is reachable and
    returns well-formed JSON; returns ``None`` on *any* failure so callers
    can **fail-open** — never exclude a machine solely because its health probe
    hiccuped or timed out.

    The short *timeout* (default 2 s) is intentional: this is a preventative
    pre-filter, not a blocking gate.  If the agent is slow to respond, skip
    the filter and rely on the fall-through loop in ``dispatch_review`` to
    surface a definitive rejection.
    """
    url = f"http://{host}:{port}/health"
    try:
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            repos = data.get("repos")
            if isinstance(repos, list):
                return [str(r) for r in repos]
    except Exception:  # noqa: BLE001 — fail-open: any network or parse error
        pass
    return None


def dispatch_review(
    completed: Assignment,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    pr_lookup=_find_or_open_pr,
    claude_md_reader=read_repo_claude_md,
    issue_body_fetcher=None,
    now: float | None = None,
    terminal_cache: dict | None = None,
    remote_branch_checker=None,
    branch_sha_fetcher=None,
    health_checker=None,
    milestone_fetcher=None,
    patch_id_computer=None,
    diff_fetcher=None,
    commits_ahead_checker=None,
    commit_messages_fetcher=None,
) -> Assignment | None:
    """Open a PR for `completed` and dispatch a review assignment.

    Returns the new review Assignment, or None if review couldn't be dispatched
    (no machine handles the repo, no branch on the completed assignment, etc.).
    The caller is responsible for persisting the board.

    *health_checker* is an optional ``(host: str) -> list[str] | None`` callable
    that returns the repo names a given agent advertises, or ``None`` to
    fail-open.  When not provided, ``_fetch_agent_advertised_repos`` is called
    directly.  Inject a stub in tests to avoid real network probes.

    *patch_id_computer* is an optional ``(diff_text: str | None) -> str |
    None`` callable (#1475) that fingerprints the merge-base diff being
    reviewed. Defaults to ``github_ops.compute_patch_id`` (a pure, no-network
    ``git patch-id --stable`` call); inject a stub in tests that don't want
    to shell out to git.

    *diff_fetcher* is an optional ``(repo_github: str, pr_number: int, *,
    max_chars: int | None) -> str | None`` callable (#1484) that fetches the
    merge-base diff embedded in the reviewer's briefing and hashed into
    ``review_patch_id``. Defaults to :func:`coord.github_ops.pr_diff` (a real
    ``gh pr diff`` subprocess call); inject a stub in tests so a PR-having
    dispatch never shells out to a live ``gh`` — mirrors
    :func:`dispatch_scoped_review`'s ``diff_fetcher`` for the same reason.

    *commits_ahead_checker* is an optional ``(repo_github: str, base: str,
    branch: str) -> int | None`` callable (#1534) used by the zero-commit gate
    below. Defaults to :func:`coord.github_ops.branch_commits_ahead` (a real
    ``gh api compare`` call); inject a stub in tests so the gate is exercised
    without network.

    *commit_messages_fetcher* is an optional ``(repo_github: str, pr_number:
    int) -> list[str]`` callable (#3112) that fetches the PR's own commit
    messages for the reviewer briefing's "worker's own claims" section.
    Defaults to :func:`coord.github_ops.get_pr_commit_messages` (a real
    ``gh pr view --json commits`` call); inject a stub in tests so dispatch
    never shells out to a live ``gh``. Fail-open: an exception here yields an
    empty list, never a blocked dispatch.
    """
    # #1627: every early-exit guard below used to be a bare `return None`,
    # collapsing 11 distinct outcomes into one signal the caller couldn't
    # distinguish (see coord/commands/plan_followup.py's `review` command,
    # which used to print "no eligible reviewer machine, or a guard ...
    # blocked it — see the coordinator log" for every one of them, even
    # though most never logged anything). `_deny` records *why* on the
    # assignment itself (`review_dispatch_reason`, transient/in-memory —
    # see its docstring in models.py) and logs at info level, then returns
    # None so call sites can keep writing `return _deny(...)`.
    #
    # #3113: `_claim_held` is flipped True the moment the atomic
    # `claim_review_dispatch` below succeeds. Every `_deny(...)` call from
    # that point on is a path that will NOT produce a dispatched review, so
    # the claim must be released here or a transient failure (agent
    # unreachable, TOS gate, zero-commit gate, ...) would permanently
    # strand this work assignment's review dispatch behind a claim nothing
    # will ever clear. Denials BEFORE the claim is taken (reviews disabled,
    # wrong type/status, no branch, already-in-flight) never held it, so
    # this is a no-op for those.
    _claim_held = False

    def _deny(reason: str) -> None:
        nonlocal _claim_held
        completed.review_dispatch_reason = reason
        log.info(
            "[review] not dispatching for %s: %s", completed.assignment_id, reason
        )
        if _claim_held:
            from coord.state import release_review_dispatch_claim  # noqa: PLC0415

            release_review_dispatch_claim(completed.assignment_id)
            _claim_held = False
        return None

    if not config.reviews.enabled or not config.reviews.auto_dispatch:
        return _deny(
            f"reviews disabled (reviews.enabled={config.reviews.enabled!r}, "
            f"reviews.auto_dispatch={config.reviews.auto_dispatch!r})"
        )
    if completed.type not in WORK_LIKE_TYPES:
        return _deny(
            f"assignment {completed.assignment_id} is type {completed.type!r}, "
            f"not reviewable work (reviewable types: {sorted(WORK_LIKE_TYPES)}). "
            "Did you mean the work assignment for this issue? Try: "
            f"coord diagnose {completed.repo_name} {completed.issue_number}"
        )
    if completed.status != "done":
        return _deny(
            f"assignment {completed.assignment_id} has status "
            f"{completed.status!r}, not 'done' — nothing to review yet"
        )
    if not completed.branch:
        # Without a branch we can't open a PR or diff. Skip silently — this
        # usually means the worker forgot to switch off main, which the
        # branch-capture code in agent._reap will have left as None.
        return _deny(
            f"assignment {completed.assignment_id} has no branch recorded — "
            "the worker may not have pushed yet"
        )

    # Dedupe: don't fire a second review if one's already in flight for this
    # completed work assignment. This in-memory check is a fast path only —
    # `board` is a snapshot that can already be stale by the time we reach
    # here, which is exactly how #3113 happened (two coordinator passes each
    # read "no review in flight" from their own snapshot and both dispatched
    # a metered review for the same completed assignment). The atomic claim
    # right below is the AUTHORITATIVE guard; this is just cheap enough to
    # skip a DB round-trip in the common case where the board already agrees.
    from coord.claim import has_active_followup, has_active_work_followup

    if has_active_followup(
        board, of_assignment_id=completed.assignment_id, assignment_type="review"
    ):
        return _deny(
            f"a review is already in flight for {completed.assignment_id}"
        )

    # #3113: DB-level conditional insert — atomic even across two separate
    # processes/machines racing this same function, unlike the board-snapshot
    # check above. Exactly one caller ever wins this for a given
    # `completed.assignment_id`; the loser denies here, before spending
    # anything on a candidate machine. Released by every subsequent
    # `_deny(...)` call in this function (see `_claim_held` above) and, once
    # this call succeeds through to a real dispatch, by the review
    # assignment's own terminal-status write (`coord.issue_store.
    # _update_local_state`) — never left permanently held.
    from coord.state import claim_review_dispatch  # noqa: PLC0415

    if not claim_review_dispatch(completed.assignment_id):
        return _deny(
            f"a review is already in flight for {completed.assignment_id} "
            "(lost the atomic dispatch-claim race — #3113)"
        )
    _claim_held = True

    try:
        # #459: skip review if a work or conflict-fix is actively rewriting the
        # branch for this issue (e.g. a coord-bounce fix iteration). Reviewing
        # stale code now would produce a verdict on code that's about to change.
        # Leave the caller's review_state as "pending" so the next reconcile pass
        # retries once the active fix finishes.
        #
        # #1553: compare on the *effective* issue (see
        # ``coord.models.effective_issue_number``), not the raw
        # ``completed.issue_number``. For an oracle-loop acceptance slice,
        # ``issue_number`` is the shared tracking issue, so keying on it here
        # would match ANY in-flight work/conflict-fix under that milestone (an
        # unrelated child) rather than only a live rewrite of THIS row's branch.
        # ``has_active_work_followup`` itself already keys its scan on the
        # effective issue; this call site has to match or the guard silently
        # stops firing for exactly the slices #1553 restored visibility for.
        from coord.models import effective_issue_number

        if has_active_work_followup(
            board,
            repo_name=completed.repo_name,
            issue_number=effective_issue_number(completed),
        ):
            return _deny(
                "a work or fix assignment is actively rewriting the branch for "
                f"issue #{completed.issue_number} in {completed.repo_name!r} — "
                "review deferred until it finishes. If nothing is actually "
                "running, this may be a phantom 'running' row left by a worker "
                "that died mid-fix; check with: coord diagnose "
                f"{completed.repo_name} {completed.issue_number}"
            )

        repo = config.repo(completed.repo_name)
        if repo is None:
            return _deny(f"repo {completed.repo_name!r} not found in config")

        # #522: the review chokepoint. Never (re)dispatch a review for work that
        # is already done on GitHub — issue closed OR PR merged. This is the second
        # flood vector (reviews of already-merged #349/#194) that the auto-loop
        # fix-dispatch guard alone didn't cover. Mark the row done so the pending-
        # review loop stops treating it as eligible. Fail-open inside
        # work_is_terminal, so a transient gh error never blocks a real review.
        #
        # #2639: `trust_issue_closed_for(completed.type)` — a test-author/
        # mock-author row's `issue_number` is the milestone's tracking issue,
        # not this row's own deliverable, so a closed tracking epic must not
        # read as "this row is already reviewed" (it would deny dispatch and
        # stamp review_state='done' with no real review ever run). Only
        # `pr_is_merged` (branch/commit-scoped, #1150) may decide for those.
        if github_ops.work_is_terminal(
            repo.github,
            completed.issue_number,
            completed.branch,
            cache=terminal_cache,
            trust_issue_closed=trust_issue_closed_for(completed.type),
        ):
            completed.review_state = "done"
            return _deny(
                f"issue #{completed.issue_number} is already closed or its PR "
                "already merged on GitHub — review is moot"
            )

        # #437: STRUCTURAL TOS-COMPLIANCE GATE — auto-dispatched reviews are
        # an unattended path, so refuse to route them through a provider
        # whose capabilities mark it ``human_attended_only``.  Deferred import
        # keeps the review module free of a module-level cycle with the
        # provider registry.  On refusal we return None (same as "auto_dispatch
        # off" / "machine unreachable") so callers leave review_state as
        # 'pending' and retry on the next notify call — consistent with how
        # _reassign handles the same guard in reconcile.py.
        #
        # #1811: ``spec_provider=config.reviews.provider`` — a review-only
        # override that outranks ``repo.provider`` in the same precedence chain
        # (spec > repo > providers.default) every other dispatch path already
        # uses. ``None`` (unset) resolves to exactly the same effective name as
        # before this field existed, so an unconfigured deployment sees no
        # behavior change. The guard still refuses a ``human_attended_only``
        # resolution regardless of which link in the chain supplied it — a
        # named ``reviews.provider`` gets no exemption from the #437 gate.
        from coord.providers import guard_unattended_dispatch  # noqa: PLC0415
        try:
            review_provider_name = guard_unattended_dispatch(
                spec_provider=config.reviews.provider,
                repo_provider=repo.provider,
                providers_cfg=config.providers,
                models_cfg=config.models,
                where="auto-dispatch review",
            )
        except ValueError as exc:
            print(f"[review] skipping auto-dispatch review: {exc}")
            return _deny(f"blocked by human-attended-only policy: {exc}")

        # #934: resolve this issue's base branch — `feature/ms-NN` when it
        # belongs to a milestone and the repo opted into the git model,
        # `repo.default_branch` (today's behavior) otherwise. Resolved once and
        # reused for the PR base, the diff-command text in the briefing, and the
        # `branch` payload field below, so they never disagree. The milestone
        # lookup itself is skipped entirely (no `gh` call) when the repo hasn't
        # opted in — a non-opted-in repo pays zero extra cost.
        base_branch = _resolve_pr_base_branch(
            completed, repo, milestone_fetcher=milestone_fetcher
        )

        # #1534: ZERO-COMMIT GATE.  Refuse to spend a metered review on a branch
        # that carries no commits over its base — there is literally nothing to
        # review, and every second of that reviewer's budget is wasted.  This is
        # the same reasoning as #946's merge enqueue gate, one stage earlier.
        #
        # The observed incident: a `test-author` killed by the Claude session
        # usage limit was recorded `done` with an empty branch, and a review was
        # auto-dispatched against it.  The reviewer diffed nothing against nothing
        # and (thanks to #873) returned a null verdict, so even that produced no
        # signal — the empty slice looked authored *and* reviewed for two days.
        #
        # Deliberately placed AFTER the `work_is_terminal` chokepoint (so an
        # already-merged branch keeps its existing `review_state="done"`
        # resolution) but BEFORE `pr_lookup` (which would otherwise open a PR for
        # the empty branch as a side effect of the check).
        #
        # FAIL-OPEN: `branch_commits_ahead` returns None — never 0 — on any gh
        # failure, so a network blip can never strand a real review.  Only a
        # definite `ahead_by == 0` from GitHub blocks.
        _ahead_check = commits_ahead_checker or github_ops.branch_commits_ahead
        _ahead = _ahead_check(repo.github, base_branch, completed.branch)
        if _ahead == 0:
            log.warning(
                "[review] branch %r for %s has 0 commits ahead of %s — refusing to "
                "auto-dispatch a review against an empty diff (#1534). The work "
                "assignment did not produce anything; re-dispatch it instead.",
                completed.branch, completed.assignment_id, base_branch,
            )
            completed.review_state = "zero_commits"
            return _deny(
                f"branch {completed.branch!r} has 0 commits ahead of {base_branch} "
                "— refusing to review an empty diff; re-dispatch the work instead"
            )

        pr = pr_lookup(
            repo.github,
            branch=completed.branch,
            default_branch=base_branch,
            issue_number=completed.issue_number,
            issue_title=completed.issue_title,
            assignment_type=completed.type,
        )

        # #904 (fix #1): build a ranked list of ALL eligible reviewer machines so
        # we can fall through to the next if one rejects the dispatch.  This
        # replaces the previous single-pick → silent-return-None path that could
        # park a work row at the merge gate forever when config drift caused a
        # "does not handle repo" 400 from the first (and only tried) machine.
        candidates = _ranked_reviewer_candidates(
            completed.machine_name, completed.repo_name, board, config
        )
        if not candidates:
            return _deny(
                f"no eligible reviewer machine configured for repo "
                f"{completed.repo_name!r}"
            )

        # #586: if the branch isn't on the remote, only the original worker machine
        # has it locally — any cross-machine reviewer would crash on git-fetch.
        # Narrow the candidate list to just that machine; if it's unavailable too,
        # stall visibly with "branch_not_on_remote".
        any_cross_machine = any(not same for _, same in candidates)
        if any_cross_machine and completed.branch:
            _check_remote = remote_branch_checker or github_ops.branch_exists_on_remote
            if not _check_remote(repo.github, completed.branch):
                log.warning(
                    "[review] branch %r not on remote for %s — routing review back "
                    "to original worker machine %s to avoid cross-machine fetch failure",
                    completed.branch, completed.assignment_id, completed.machine_name,
                )
                # #2240: same cordon-blind set as the candidate ranking above —
                # this branch NARROWS to the worker machine, so reading a
                # cordoned worker as "unavailable" here would strand the review
                # at `branch_not_on_remote` for exactly the reason #2240 names.
                from coord.machine_pause import follow_on_paused_set  # noqa: PLC0415
                paused = follow_on_paused_set(config.machines)
                worker_machine = next(
                    (m for m in config.machines if m.name == completed.machine_name),
                    None,
                )
                if (
                    worker_machine is not None
                    and worker_machine.can_work_on(completed.repo_name)
                    and worker_machine.name not in paused
                ):
                    # Restrict to just the worker machine — it has the branch locally.
                    candidates = [(worker_machine, True)]
                else:
                    # Original machine also unavailable — stall visibly.
                    log.error(
                        "[review] branch %r not on remote for %s and original machine "
                        "%s is unavailable (paused or not configured) — "
                        "review BLOCKED until branch is pushed to origin",
                        completed.branch, completed.assignment_id, completed.machine_name,
                    )
                    completed.review_state = "branch_not_on_remote"
                    return _deny(
                        f"branch {completed.branch!r} not on remote and original "
                        f"worker machine {completed.machine_name!r} is unavailable "
                        "(paused or not configured) — push the branch to origin "
                        "or unpause the worker machine"
                    )

        # Compute the parts that are constant across all candidate machines.

        # #612: merge-base diff — embedded verbatim so the reviewer reviews exactly
        # the branch's own changes (a stale-base diff sweeps in already-merged
        # commits as spurious deletions, #546).  Best-effort: None keeps the
        # fallback three-dot git-diff instructions in the briefing.
        # #1475: fetch the full, untruncated diff once — it's the input to the
        # content-hash (`review_patch_id` below) and must never be the mutated,
        # truncated-with-a-trailer string (hashing that gives a patch-id that can
        # never match the merge-time `branch_patch_id`, which is computed from an
        # uncapped compare-API diff). The display copy shown to the reviewer is
        # then truncated locally from the same fetch — no second `gh` call.
        _diff = diff_fetcher or github_ops.pr_diff
        full_diff_text = _diff(repo.github, pr["number"], max_chars=None) if pr else None
        diff_text = (
            github_ops.truncate_diff_text(full_diff_text) if full_diff_text is not None else None
        )

        fetch_body = issue_body_fetcher or _fetch_issue_body
        issue_body = fetch_body(repo.github, completed.issue_number)

        # #3112: the worker's own commit messages — one half of the "worker's
        # own claims" section (the other half is completed.completion_summary,
        # already on the Assignment). Fail-open like every other best-effort
        # GitHub read in this function: a fetch failure just means the review
        # briefing has fewer worker-said claims to cross-check, never a
        # blocked dispatch.
        _fetch_commits = commit_messages_fetcher or github_ops.get_pr_commit_messages
        commit_messages: list[str] = []
        if pr:
            try:
                commit_messages = _fetch_commits(repo.github, pr["number"])
            except Exception:  # noqa: BLE001 — fail-open, see docstring
                commit_messages = []

        # #2192: free pre-review nudge (see diff_missing_test_coverage docstring).
        # Logged only, ahead of the paid reviewer dispatch below — never gates,
        # never denies, never mutates `completed`. A false positive here must
        # never cost a round trip, so nothing downstream reads this.
        #
        # Deliberately `log.warning`, not `log.info` (#2192 review follow-up):
        # this repo has zero `logging.basicConfig`/`setLevel`/`addHandler` calls
        # outside tests (see coord/interactive.py:888-898's #865 note on the same
        # trap), so the root logger sits at Python's default WARNING floor with
        # no handler attached — an `INFO` record is filtered before it reaches
        # anywhere and is a silent no-op under every real entry point (`coord
        # serve`, `coord notify`, `reconcile()`). `WARNING` clears that floor and
        # is picked up by `logging`'s handler-of-last-resort, which prints
        # straight to stderr with zero configuration — confirmed empirically:
        # `logging.getLogger("coord.review").warning(...)` in a bare subprocess
        # writes to stderr; `.info(...)` writes nothing.
        if diff_missing_test_coverage(full_diff_text):
            log.warning(
                "[review] %s: diff touches user-visible source with zero test "
                "files changed — matches #2132's 'missing test only' pattern "
                "(free static check, non-blocking; dispatching review as normal)",
                completed.assignment_id,
            )

        # #1811: does the resolved review provider share the worker's model
        # family? ``completed.provider_name`` is the *resolved* name recorded at
        # work-dispatch time (spec > repo > providers.default); ``None`` means a
        # row predating #324 or a path that doesn't set it, which the rest of
        # the codebase (e.g. coord/gates.py's TUI rendering) treats as the
        # implicit "claude" default. Provider co-location is a larger loss of
        # independence than machine co-location (a fresh session removes shared
        # context, but not shared blind spots) — surfaced below in the
        # reviewer's own briefing, mirroring ``same_as_worker``.
        worker_provider_name = completed.provider_name or "claude"
        provider_same_as_worker = review_provider_name == worker_provider_name
        if provider_same_as_worker:
            log.info(
                "[review] %s: reviewer provider %r matches worker provider — "
                "reduced independence (shared model family)",
                completed.assignment_id, review_provider_name,
            )

        # Pin the reviewer's model to avoid the agent defaulting to Opus (#911).
        # #1430: deliberately not consulting models.labels — the reviewer's
        # effort scales with diff size, not the original work issue's tier
        # label, and #911 already pins this deliberately.
        review_model_alias = config.models.default
        review_model_wire = config.models.resolve(review_model_alias)

        # #821: capture branch HEAD SHA once; staleness detected post-review.
        _get_sha = branch_sha_fetcher or github_ops.get_branch_sha
        review_head_sha: str | None = None
        try:
            review_head_sha = _get_sha(repo.github, completed.branch)
        except Exception:  # noqa: BLE001 — fail-safe: missing SHA is not blocking
            pass

        # #1475: fingerprint the *full* merge-base diff (`full_diff_text`, computed
        # above) — not the display-truncated `diff_text` — so this matches the
        # merge-time counterpart (`get_branch_patch_id`, also uncapped) for any PR
        # whose diff exceeds the display truncation threshold. Stored alongside
        # review_head_sha so a later commit-bound staleness check (a rebase moving
        # the SHA) can carry the approval forward when the content is byte-identical.
        _compute_patch_id = patch_id_computer or github_ops.compute_patch_id
        review_patch_id: str | None = None
        try:
            review_patch_id = _compute_patch_id(full_diff_text)
        except Exception:  # noqa: BLE001 — fail-safe: missing patch-id is not blocking
            pass

        # #603: per-issue context digest (cross-repo deps / prior findings).
        from coord.state import issue_context_block  # noqa: PLC0415
        context_prefix = issue_context_block(completed.repo_name, completed.issue_number)

        # #944 sealing v1: flag tests/acceptance/ as sealed when this repo has an
        # oracle-loop acceptance driver configured — the reviewer must reject any
        # diff that touches it (docs/ORACLE_LOOP.md).
        #
        # #1552: the set is DERIVED from the driver definition rather than
        # hardcoded to that one literal. `tests/acceptance/` alone fits a
        # directory-discovered suite (`pytest tests/acceptance/{ms}`) and is
        # structurally unsatisfiable for an entry-point-linked one
        # (`cargo test --test acceptance` sees nothing until `tui/tests/
        # acceptance.rs` include!s the slice) — under #1175's blanket refusal a
        # `test-author` on the Rust route could only wire its slice in and be
        # bounced, or leave it unwired and ship dead code. Each route now
        # declares its own `entrypoint:`.
        sealed_paths = config.acceptance.sealed_paths(completed.repo_name)
        sealed_entrypoints = config.acceptance.entrypoints(completed.repo_name)

        # #2966: coordinator-owned docs (repo's own CLAUDE.md plus anything it
        # additionally lists under coordinator_only_files) — see
        # coordinator_owned_docs' docstring for why this doesn't depend on the
        # repo actually configuring coordinator_only_files.
        coordinator_doc_paths = coordinator_owned_docs(repo)

        client = http_client or httpx

        # Iterate candidates in priority order.  On agent rejection (4xx from a
        # misconfigured agent, health-check filter on a drifted config, etc.) we
        # log a warning and try the next candidate instead of giving up silently.
        # Only definitive rejections (4xx responses or health-check exclusions) set
        # had_rejection=True; transient network failures leave the row as "pending"
        # so the next reconcile/notify pass retries automatically.
        had_rejection = False
        for machine, same_as_worker in candidates:
            # Fix #2 (PREVENTATIVE): pre-filter against the agent's /health
            # ``repos`` list so a drifted local config can't pick a machine that
            # will 400.  Fail-open: None means "probe failed, include anyway".
            # #1485: an empty list is NOT the same as None here — it means "this
            # agent has no local coordinator.yml at all" (the expected, correct
            # state for a worker-only machine — coordinator.yml lives on
            # dellserver only), which matches the agent's own interpretation in
            # AgentServer.assign (`if self.repos and spec.repo_name not in
            # self.repos`, coord/agent.py) where an empty list is falsy and means
            # "no restriction, accept everything." Treat `[]` the same way here —
            # only a *non-empty* advertised list that omits the repo is a genuine
            # drift signal worth skipping the candidate for.
            _hc = health_checker if health_checker is not None else _fetch_agent_advertised_repos
            advertised = _hc(machine.host)
            if advertised and completed.repo_name not in advertised:
                log.warning(
                    "[review] skipping candidate %s: /health advertises repos %r "
                    "but repo %r is not listed — possible config drift",
                    machine.name, advertised, completed.repo_name,
                )
                had_rejection = True
                continue

            repo_path = machine.repo_path(completed.repo_name)
            if repo_path is None:
                log.warning(
                    "[review] skipping candidate %s: no repo_path for %r",
                    machine.name, completed.repo_name,
                )
                continue

            claude_md = claude_md_reader(Path(repo_path).expanduser())

            # #476 / #612: briefing is rebuilt per candidate because same_as_worker
            # (warning note in the briefing) and claude_md path can differ between
            # machines.
            briefing = context_prefix + build_review_briefing(
                pr_number=pr["number"] if pr else None,
                pr_url=pr["url"] if pr else None,
                repo_github=repo.github,
                repo_name=repo.name,
                issue_number=completed.issue_number,
                issue_title=completed.issue_title,
                issue_body=issue_body,
                branch=completed.branch,
                worker_machine=completed.machine_name,
                same_as_worker=same_as_worker,
                provider_same_as_worker=provider_same_as_worker,
                review_provider=review_provider_name,
                reviews_cfg=config.reviews,
                repo_claude_md=claude_md,
                review_head_sha=review_head_sha,
                default_branch=base_branch,
                # #476: a fix worker carries review_iteration > 0; its re-review is
                # scoped to the fix delta rather than re-reviewing the whole PR.
                review_iteration=getattr(completed, "review_iteration", 0) or 0,
                diff_text=diff_text,
                sealed_paths=sealed_paths,
                sealed_entrypoints=sealed_entrypoints,
                coordinator_doc_paths=coordinator_doc_paths,
                assignment_type=completed.type,
                completion_summary=completed.completion_summary,
                commit_messages=commit_messages,
            )

            payload = {
                "repo_name": completed.repo_name,
                "repo_path": repo_path,
                "issue_number": completed.issue_number,
                "issue_title": f"[review] {completed.issue_title}",
                "briefing": briefing,
                "files_allowed": [],
                "files_forbidden": [],
                "pull_repos": [],
                "type": "review",
                "model": review_model_wire,
                "system_prompt": REVIEWER_SYSTEM_PROMPT,
                "review_target": str(pr["number"]) if pr else completed.branch,
                # #255: review checkout uses the PR branch, but the agent's worktree
                # setup still consults `branch` as the integration base when no PR
                # branch exists locally yet.  Match the work-dispatch path.
                "branch": base_branch or "main",
            }
            # #1811: carry the resolved review provider onto the wire the same
            # way coord.dispatch.dispatch() does for work — without this the
            # agent's own AssignmentSpec.provider stays None and it silently
            # runs its legacy default worker command regardless of what
            # guard_unattended_dispatch resolved above, which is exactly the
            # "configuration appears to work while doing nothing" trap #1811
            # calls out. Gated by the same helper dispatch() uses so a vanilla,
            # uncustomized "claude" resolution keeps an unconfigured
            # deployment's wire payload byte-identical to before this field
            # existed.
            from coord.dispatch import _wire_payload_needs_provider_field  # noqa: PLC0415

            if review_provider_name and _wire_payload_needs_provider_field(
                review_provider_name, config,
            ):
                payload["provider"] = review_provider_name

            url = f"http://{machine.host}:{AGENT_PORT}/assign"
            try:
                resp = client.post(url, json=payload, timeout=15)
                resp.raise_for_status()
                agent_response = resp.json()
            except httpx.HTTPStatusError as exc:
                # Fix #1 (PRIMARY): the agent definitively rejected the dispatch
                # (e.g. 400 "does not handle repo 'x'").  Try the next candidate
                # instead of silently returning None and leaving review_state as
                # 'pending' (#904).
                #
                # #904 (fix #2): only a 4xx is a *definitive* rejection — it means
                # the agent looked at the request and refused it (bad repo, bad
                # payload, etc.), which is a config-drift signal.  A 5xx means the
                # agent's own handler blew up (mid-restart, disk full, unhandled
                # exception) and says nothing about whether this agent/repo pairing
                # is valid — treat it like the transient network branch below so
                # the row stays "pending" and retries next pass instead of
                # permanently stalling as "no_eligible_reviewer".
                if exc.response.is_client_error:
                    log.warning(
                        "[review] agent %s rejected dispatch with HTTP %d — "
                        "trying next reviewer candidate",
                        machine.name, exc.response.status_code,
                    )
                    had_rejection = True
                else:
                    log.warning(
                        "[review] agent %s returned server error HTTP %d (transient) — "
                        "trying next reviewer candidate",
                        machine.name, exc.response.status_code,
                    )
                continue
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                # Transient network failure — try next candidate, and if all
                # fail transiently, leave review_state unchanged so the next
                # reconcile/notify pass retries automatically.
                log.warning(
                    "[review] agent %s unreachable (%s) — trying next reviewer candidate",
                    machine.name, exc,
                )
                continue

            # Dispatch accepted — record the review assignment and return.
            review_assignment = Assignment(
                machine_name=machine.name,
                repo_name=completed.repo_name,
                issue_number=completed.issue_number,
                issue_title=f"[review] {completed.issue_title}",
                files_allowed=[],
                files_forbidden=[],
                briefing=briefing,
                assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
                status="running",
                branch=completed.branch,
                pr_url=pr.get("url") if pr else None,
                dispatched_at=now if now is not None else time.time(),
                type="review",
                review_target=str(pr["number"]) if pr else completed.branch,
                review_of_assignment_id=completed.assignment_id,
                model=review_model_alias,
                # #1811: record the resolved review provider the same way
                # coord.dispatch.dispatch() records the work provider — so the
                # TUI/audit trail can distinguish a review that ran through
                # `reviews.provider`/`repo.provider` from one that fell through
                # to `providers.default`, instead of guessing "claude" for every
                # review row the way a `None` here used to force.
                provider_name=review_provider_name,
                review_head_sha=review_head_sha,
                review_patch_id=review_patch_id,
                # #1553: a review of an oracle-loop acceptance slice is work for
                # the CHILD issue, not for the milestone's tracking issue that
                # `completed.issue_number` carries. Inherit the slice attribution
                # so the child's Pipeline row shows the review as activity and
                # its cost rolls up to the child. None for every ordinary review
                # (the parent has no `for_issue_number`), so nothing changes for
                # non-slice work. See `coord.models.effective_issue_number`.
                for_issue_number=completed.for_issue_number,
            )
            board.active.append(review_assignment)

            from coord.state import record_dispatched_assignment  # noqa: PLC0415
            record_dispatched_assignment(
                assignment=review_assignment,
                repo_github=repo.github,
            )

            return review_assignment

        # All candidates exhausted.  Distinguish definitive rejection (config
        # drift, drifted agent config) from transient network failures.
        if had_rejection:
            # At least one agent definitively rejected the repo — stall visibly
            # with a named state so `coord status` can surface an actionable error
            # and the pending-review loop stops silently retrying (#904).
            log.error(
                "[review] all reviewer candidates rejected dispatch for %s "
                "(repo=%r, branch=%r) — setting review_state='no_eligible_reviewer'. "
                "Check that every agent's repos list includes %r.",
                completed.assignment_id, completed.repo_name, completed.branch,
                completed.repo_name,
            )
            completed.review_state = "no_eligible_reviewer"
            completed.review_dispatch_reason = (
                f"all reviewer candidates rejected dispatch for repo "
                f"{completed.repo_name!r} (config drift — check every agent's "
                "repos list)"
            )
        else:
            # Only transient failures — leave review_state unchanged so the next
            # reconcile/notify pass retries automatically.
            log.warning(
                "[review] all reviewer candidates unreachable for %s "
                "(repo=%r) — will retry on next reconcile/notify pass",
                completed.assignment_id, completed.repo_name,
            )
            completed.review_dispatch_reason = (
                f"all reviewer candidates unreachable for repo "
                f"{completed.repo_name!r} — transient, will retry automatically"
            )
        # #3113: this tail doesn't go through `_deny` (it sets `review_state`
        # directly, distinguishing the rejected/unreachable cases) but it is
        # still a path that produced no dispatched review, so the claim taken
        # above must be released the same way every `_deny(...)` call releases
        # it — otherwise a transient "all candidates unreachable" pass would
        # permanently strand this work assignment behind a claim nothing else
        # will ever clear.
        if _claim_held:
            from coord.state import release_review_dispatch_claim  # noqa: PLC0415

            release_review_dispatch_claim(completed.assignment_id)
            _claim_held = False
        return None
    except Exception:
        # #3113: any unhandled exception in the ~500 lines between the
        # claim above and a successful dispatch (pr_lookup, briefing
        # assembly, JSON handling of agent_response, ...) used to
        # propagate straight past every `_deny(...)` release path,
        # permanently stranding this work assignment's review dispatch
        # behind a claim nothing would ever clear (no row is created for
        # a raised exception, so the terminal-status release hook in
        # `coord.issue_store._update_local_state` never fires either).
        # This is the safety net: release-then-reraise, so the caller
        # still sees the original failure (fail-open, like every other
        # transient-error path in this function) but the claim is never
        # left held by a session that no longer exists to release it.
        if _claim_held:
            from coord.state import release_review_dispatch_claim  # noqa: PLC0415

            release_review_dispatch_claim(completed.assignment_id)
            _claim_held = False
        raise


def dispatch_pending_reviews(board, config, *, test_gate_active: bool = False, now=None):
    """Bounded bulk review dispatch — the flood guard (incident 2026-06-08).

    Gather every completed-work row eligible for a review, then dispatch
    reviews subject to two limits that prevent the review-flood failure mode —
    a backlog "unmasking" firing hundreds of metered ``claude -p`` reviews in a
    single reconcile/notify pass:

    1. **Surge gate.** If the number of eligible rows exceeds
       ``reviews.flood_threshold`` (and the threshold is > 0), dispatch
       *nothing* and log loudly. A sudden surge is the unmasking signature, so
       we halt and require a human to either clear the stale backlog (mark it
       reviewed/skipped) or opt in via ``reviews.allow_review_flood: true`` /
       ``COORD_ALLOW_REVIEW_FLOOD=1``.
    2. **Per-pass cap.** Otherwise dispatch at most
       ``reviews.max_auto_dispatch_per_pass`` reviews this pass (0 = unbounded);
       the remainder stay ``"pending"`` and are picked up next pass, so even a
       moderate batch bleeds out at a bounded rate instead of all at once.

    A row is eligible when its ``review_state`` is ``None``/``"pending"``, its
    ``type`` is in :data:`coord.models.WORK_LIKE_TYPES` (``"work"`` or
    ``"mock-author"``, #930), the (optional) test gate is satisfied, and #459's
    ``has_active_work_followup`` is False (don't review code a live fix is
    rewriting). Both ``reconcile()`` and ``coord notify`` route bulk dispatch
    through here so the cap, surge gate, and #459 dedupe are enforced on every
    automatic path. Sets ``review_state="dispatched"`` on each row it
    dispatches and returns the dispatched review ``Assignment``s. The caller
    persists the board.

    #1565: before the eligibility filter runs, any row whose ``review_state``
    reads ``pending``/``None`` but that already has a *terminal* verdict on a
    completed ``type="review"`` assignment targeting it is excluded and
    self-healed (``review_state`` set to ``"done"``) rather than trusted at
    face value — see the guard immediately below. This is the backstop for a
    row whose ``review_state`` regressed to ``pending`` after a review already
    rendered a verdict (a stale whole-board ``save_board()`` clobber, or a
    verdict that was never propagated to the parent row).
    """
    import logging
    import os

    from coord.claim import has_active_work_followup
    from coord.models import effective_issue_number

    logger = logging.getLogger("coord.review")

    # Test-before-Review reorder: when the pipeline orders Test ahead of Review,
    # hold automatic review dispatch until the work carries a passed/skipped
    # test verdict, so the headless auto-loop matches the displayed
    # Work → Test → Review order (and never burns a metered review on code the
    # smoke test hasn't validated yet). Explicit callers can still force the
    # gate on via ``test_gate_active``; the explicit ``coord review``/``coord
    # pr`` paths (→ ``dispatch_review`` directly) stay ungated so a human can
    # always request a review deliberately.
    gate_test = test_gate_active or (
        getattr(config, "pipeline", None) is not None
        and config.pipeline.test_precedes_review()
    )

    # #1076/#1152: a `type="mock-author"` (Gate A contract/fixture diff) or
    # `type="test-author"` (per-issue JIT acceptance-slice authoring, #931)
    # completion is a fixture/test-only diff — it matches no
    # `smoke_tests.capability_rules` rule by construction, so nothing ever
    # produces a Test-gate verdict for it and `test_state` stays NULL forever.
    # Under an active test gate that means the row is silently and
    # permanently excluded from `eligible` below — no error, no stuck
    # indicator, just a row that never gets reviewed (the #1076 repro,
    # assignment 9960b957ff3f; the #1152 repro, assignment 2e93ee72071c).
    # There is nothing to smoke-test for either shape of completion, so
    # "skipped" is always the correct verdict, not a judgment call — backfill
    # it here, the single choke point both reconcile() and `coord notify`
    # (`_dispatch_board_pending_reviews`) route bulk review dispatch through,
    # so this also retroactively unsticks any row that went "done" before
    # this fix shipped. `type="work"` rows are untouched — the test gate
    # still applies to them exactly as before (do NOT widen this to
    # `WORK_LIKE_TYPES`, which also contains `"work"`).
    _AUTO_SKIP_TEST_GATE_TYPES = ("mock-author", "test-author")
    if gate_test:
        from coord.state import record_test_verdict

        for c in board.completed:
            if (
                c.type in _AUTO_SKIP_TEST_GATE_TYPES
                and c.review_state in (None, "pending")
                and c.test_state is None
                and c.assignment_id is not None
            ):
                record_test_verdict(
                    assignment_id=c.assignment_id,
                    test_state="skipped",
                    test_reason=(
                        f"Gate A {c.type}: contract/fixture-only diff, "
                        "nothing to smoke-test (#1076/#1152)"
                    ),
                )
                c.test_state = "skipped"

    # #1565: dispatch-side backstop. review_state is supposed to be the
    # single source of truth for "does this row still need a review", but
    # it has been observed to regress to "pending" out from under a row that
    # already carries a real, terminal verdict on a completed review
    # assignment (a stale whole-board save_board() clobber, or a code path
    # that forgot to propagate the verdict onto the parent — see
    # `_advance_pipeline`'s #1565 fix). Before trusting review_state, check
    # for that shape directly and refuse to burn a second metered review
    # re-deriving a verdict that already exists — log loudly (a guard that
    # trips silently teaches nobody) and self-heal the row instead of
    # leaving it to trip this same guard every pass.
    for c in board.completed:
        if (
            c.review_state not in (None, "pending")
            or c.type not in WORK_LIKE_TYPES
            or c.assignment_id is None
        ):
            continue
        prior_verdict = next(
            (
                r.review_verdict
                for r in board.active + board.completed
                if r.type == "review"
                and r.review_of_assignment_id == c.assignment_id
                and r.review_verdict is not None
            ),
            None,
        )
        if prior_verdict is None:
            continue
        logger.warning(
            "dispatch guard (#1565): %s (%s #%s) already has a terminal "
            "review verdict %r recorded on a prior review assignment, but "
            "its own review_state=%r would make it eligible for another "
            "metered review — refusing to re-dispatch and self-healing "
            "review_state='done' instead.",
            c.assignment_id, c.repo_name, c.issue_number,
            prior_verdict, c.review_state,
        )
        from coord.state import record_work_review_verdict

        c.review_state = "done"
        c.review_verdict = prior_verdict
        record_work_review_verdict(c.assignment_id, prior_verdict)

    # #1612 step 2: enforce max_review_iterations here too, not just in
    # run_for_fix_transition. A fix row whose test verdict isn't in yet gets
    # deferred to this function (its review_state is set back to "pending"
    # rather than dispatching directly — see run_for_fix_transition's #1612
    # fix), so by the time it reaches the eligibility filter below its
    # review_iteration has normally already been checked once, upstream. This
    # is the defense-in-depth duplicate of that guard: without it, a row that
    # reaches review_state="pending" through any other path (a future code
    # path, a manual edit) would silently bypass the fix-loop cap instead of
    # stopping it here the same way the other bulk-path guards are duplicated
    # (claude-pty, terminal-work).
    _max_review_iter = config.pipeline.max_review_iterations
    for c in board.completed:
        if (
            c.review_state in (None, "pending")
            and c.type in WORK_LIKE_TYPES
            and c.status == "done"
            and (c.review_iteration or 0) >= _max_review_iter
        ):
            logger.warning(
                "dispatch_pending_reviews cap guard (#1612): %s (%s #%s) has "
                "review_iteration=%d >= max_review_iterations=%d — not "
                "dispatching another review.",
                c.assignment_id, c.repo_name, c.issue_number,
                c.review_iteration or 0, _max_review_iter,
            )
            from coord.auto_loop import _post_max_iterations_notice

            _post_max_iterations_notice(c, config)
            c.review_state = "cap_hit"

    eligible = [
        c
        for c in board.completed
        if c.review_state in (None, "pending")
        and c.type in WORK_LIKE_TYPES
        # #1534: only a genuinely SUCCESSFUL completion is review-eligible.
        # `dispatch_review` has always refused a non-`done` row internally,
        # but the bulk loop used to feed it every `failed`/`advisory` row on
        # the board on every pass (they carry `review_state=None`), which
        # made this loop's own eligibility list read as "review is pending
        # for these" when it was not — and made the surge/flood counters
        # below count rows that could never dispatch. Stating the invariant
        # here keeps the loop and the chokepoint agreeing.
        and c.status == "done"
        # #555: NEVER auto-dispatch a headless `claude -p` review for an
        # *interactive* (`provider_name="claude-pty"`) work completion. The
        # interactive Work→Review handoff is human-attended (TUI confirm →
        # interactive review); a metered headless review must not silently
        # follow it. This guard lives only in the automatic bulk path — the
        # explicit `coord review <id>` escape hatch (→ dispatch_review) still
        # lets a human deliberately request a headless review if they want one.
        and c.provider_name != "claude-pty"
        and (not gate_test or c.test_state in ("passed", "skipped"))
        # #1553: effective issue, not raw — see the matching comment on the
        # ``dispatch_review`` call site above; both must key on the same
        # thing has_active_work_followup itself keys on internally.
        and not has_active_work_followup(
            board, repo_name=c.repo_name, issue_number=effective_issue_number(c)
        )
    ]
    if not eligible:
        return []

    threshold = config.reviews.flood_threshold
    override = (
        config.reviews.allow_review_flood
        or os.environ.get("COORD_ALLOW_REVIEW_FLOOD") == "1"
    )
    if threshold and len(eligible) > threshold and not override:
        logger.warning(
            "review flood guard: %d work rows are pending review (> "
            "reviews.flood_threshold=%d). Refusing bulk dispatch to avoid a "
            "metered review flood. Clear the stale backlog (mark reviewed/"
            "skipped), or set reviews.allow_review_flood: true (or "
            "COORD_ALLOW_REVIEW_FLOOD=1) to override.",
            len(eligible),
            threshold,
        )
        return []

    cap = config.reviews.max_auto_dispatch_per_pass
    # #522: one terminal-state cache for this whole pass, so a backlog full of
    # already-merged rows (the #349 ×4 case) costs one gh lookup per issue, not
    # one per row revisited.
    terminal_cache: dict = {}
    dispatched: list = []
    for completed in eligible:
        if cap and len(dispatched) >= cap:
            break
        review = dispatch_review(
            completed, board, config, now=now, terminal_cache=terminal_cache
        )
        if review is not None:
            completed.review_state = "dispatched"
            dispatched.append(review)
        # On failure leave review_state as "pending" so the next pass retries.
        # Terminal rows are marked review_state="done" inside dispatch_review
        # (#522), dropping them from `eligible` on the next pass.

    held = sum(1 for c in eligible if c.review_state in (None, "pending"))
    if held:
        logger.info(
            "review dispatch cap: dispatched %d this pass, %d held for next "
            "pass (reviews.max_auto_dispatch_per_pass=%d).",
            len(dispatched),
            held,
            cap,
        )
    return dispatched


# ── Scoped re-review (#1476) ─────────────────────────────────────────────────
#
# When a conflict-fix rebase changes content under an already-`approve`d
# review (the branch's patch-id no longer matches the one the review covered
# — see #1475), `has_approved_review` correctly voids the stale approval, but
# the only way to get an approval back today is a FULL re-review of the whole
# PR — even when the conflict-fix resolution touched a handful of lines. The
# functions below dispatch a review scoped to just that resolution delta
# instead: the reviewer is told the PR was already approved, handed a diff
# *of diffs* showing exactly what the rebase changed, and asked to rule on
# that alone. `coord.merge_queue.find_scoped_review_candidate` /
# `only_conflict_fix_since_review` gate when this path is eligible; a
# request-changes verdict here is a completely ordinary `type="review"`
# assignment (same `review_of_assignment_id` chain, same REVIEW_VERDICT
# parsing), so the existing fix/re-review auto-loop drives it identically to
# a full review — nothing downstream needs to know the review was scoped.


def compute_resolution_delta(old_diff_text: str | None, new_diff_text: str | None) -> str | None:
    """Return a unified diff *between* two full unified diffs (#1476).

    A diff of diffs: treats each of *old_diff_text* (the diff a prior review
    approved) and *new_diff_text* (the branch's current diff) as a plain text
    blob and runs :mod:`difflib` over them. The result shows exactly the
    lines a conflict-fix resolution touched — typically a couple of hunks —
    instead of forcing a reviewer to re-read the whole PR to find them.

    Returns ``None`` when either input is missing/blank (nothing to scope a
    review around — the caller must fall back to a full review) or when the
    two diffs are textually identical (no delta to show — shouldn't happen
    once the caller has already confirmed the patch-ids differ, but this
    fails safe rather than dispatching a review with an empty "what changed"
    section).
    """
    if not old_diff_text or not old_diff_text.strip():
        return None
    if not new_diff_text or not new_diff_text.strip():
        return None
    old_lines = old_diff_text.splitlines(keepends=True)
    new_lines = new_diff_text.splitlines(keepends=True)
    delta = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="previously-reviewed diff",
        tofile="current diff (post conflict-fix)",
    ))
    if not delta:
        return None
    return "".join(delta)


def build_scoped_review_briefing(
    *,
    pr_number: int | None,
    pr_url: str | None,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    issue_title: str,
    branch: str | None,
    resolution_delta: str,
    default_branch: str = "main",
) -> str:
    """Assemble a SCOPED re-review briefing (#1476). Pure function — testable.

    Dispatched when a conflict-fix rebase changed content under an
    already-approved review with no other intervening work/fix commit
    (``coord.merge_queue.only_conflict_fix_since_review``). The reviewer is
    told the PR was already approved — it must not re-derive a verdict
    already reached — and is handed ONLY the resolution delta
    (:func:`compute_resolution_delta`) to rule on. This is the whole point:
    a ~1300-line PR whose conflict-fix resolution was two hunks costs a
    ~15-line read, not a full re-review (the #1453 motivating case).
    """
    lines: list[str] = []
    lines.append(f"# Scoped re-review: {repo_github} PR #{pr_number}")
    lines.append("")
    lines.append(
        f"You are re-reviewing issue #{issue_number}: {issue_title}. This PR "
        "was **already approved** by a previous review. Since then, an "
        "automated conflict-fix worker rebased the branch onto its target "
        "branch to resolve a merge conflict, and that rebase changed "
        "content — so the branch's content-fingerprint no longer matches "
        "what the prior review covered, and the approval was voided."
    )
    lines.append("")
    lines.append(
        "**You do NOT need to re-review the whole PR.** Everything the "
        "prior review already approved stands — do not re-litigate it and "
        "do not raise new findings about code the resolution delta below "
        "doesn't touch. Your ONLY job is to judge whether the conflict-fix "
        "resolution itself introduced a real bug or silently dropped "
        "something either side of the conflict needed."
    )
    lines.append("")
    lines.append("## Context")
    lines.append(f"- Repo: {repo_github} (local name: {repo_name})")
    lines.append(f"- Branch: {branch or '(unknown)'}")
    if pr_url:
        lines.append(f"- PR URL: {pr_url}")
    lines.append("")
    lines.append("## Resolution delta (review THIS)")
    lines.append("")
    lines.append(
        "This is a diff *of diffs* — the difference between the diff the "
        "prior review approved and the branch's current diff, computed "
        "with `git patch-id`-fingerprinted content that changed under the "
        "conflict-fix rebase. A line starting with `-` was in the "
        "previously-approved diff and is gone now; a line starting with "
        "`+` is new since the approval. This is exactly what the "
        "conflict-fix resolution changed — nothing else in the PR did."
    )
    lines.append("")
    lines.append("```diff")
    lines.append(resolution_delta.strip())
    lines.append("```")
    lines.append("")
    lines.append("## What to do")
    lines.append("")
    lines.append(
        "1. Read the resolution delta above. If a hunk doesn't make sense "
        "standalone, `git fetch origin && git diff origin/"
        f"{default_branch}...origin/{branch or 'HEAD'}` gets you the full "
        "current diff for extra context — but you should rarely need it."
    )
    lines.append(
        "2. `approve` unless the resolution itself introduces a genuine bug "
        "or silently drops content either side of the conflict needed. "
        "Do NOT block on style/nit findings about code outside the delta — "
        "that code was already approved."
    )
    lines.append(
        "3. Before you end your session, record your verdict TWICE — belt "
        "and braces, neither step substitutes for the other. FIRST, if the "
        "environment variable `COORD_ASSIGNMENT_ID` is set, write your full "
        "findings to a file and run `coord report-result --assignment "
        '"$COORD_ASSIGNMENT_ID" --status done --verdict '
        "approve|request-changes --body-file <file>` — this writes straight "
        "to the coordinator's board and is the authoritative record. If "
        "`COORD_ASSIGNMENT_ID` is unset, `coord` errors, or it's not on "
        "your PATH, say so plainly and move on to the required backup "
        "below regardless. THEN, at the END of your session, ALWAYS ALSO "
        "output your findings in this exact format as the PATH-independent "
        "backup (the coordinator posts the review to GitHub on your "
        "behalf — do NOT run any `gh` commands):"
    )
    lines.append("")
    lines.append("```")
    lines.append("REVIEW_VERDICT: approve")
    lines.append("REVIEW_BODY:")
    lines.append("<your full review text in markdown>")
    lines.append("END_REVIEW")
    lines.append("```")
    lines.append("")
    lines.append(
        "Use `REVIEW_VERDICT: request-changes` if the resolution introduced "
        "a real bug."
    )
    lines.append(
        "BODY STRUCTURE — same three headings as a normal review, always "
        "all three: `## Blocking findings`, `## Non-blocking concerns`, "
        "`## Nits`. Write the single line `None.` under a heading with "
        "nothing under it."
    )
    lines.append(
        "FORMAT CONTRACT — `REVIEW_VERDICT:`, `REVIEW_BODY:`, and "
        "`END_REVIEW` are parsed by machine: plain text at the start of "
        "their own line, no Markdown decoration. `END_REVIEW` is a HARD "
        "REQUIREMENT — an otherwise-complete review missing that exact "
        "line is discarded in its entirety."
    )
    return "\n".join(lines)


def dispatch_scoped_review(
    entry,
    prior_review: Assignment,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    now: float | None = None,
    diff_fetcher=None,
    branch_sha_fetcher=None,
    patch_id_computer=None,
    terminal_cache: dict | None = None,
) -> Assignment | None:
    """Dispatch a SCOPED re-review (#1476) for a merge entry whose approval
    was voided ONLY by a content-changing conflict-fix rebase.

    Caller contract: only call this after
    :func:`coord.merge_queue.find_scoped_review_candidate` (its result is
    *prior_review*) and :func:`coord.merge_queue.only_conflict_fix_since_review`
    (the guardrail) have both confirmed this path applies — mirrors how
    :func:`dispatch_review` trusts :func:`dispatch_pending_reviews`'s
    eligibility filter rather than re-deriving it. *entry* is a
    ``coord.merge_queue.QueuedMerge``.

    *diff_fetcher* defaults to :func:`coord.github_ops.get_compare_diff`
    (``(repo, base, ref) -> str | None``); inject a stub in tests. Fetches
    the diff *prior_review* covered (``target_branch...prior_review.
    review_head_sha``) and the branch's current diff (``target_branch...
    entry.branch``), computes the resolution delta between them, and — only
    when that delta is non-empty — dispatches a review briefed on just the
    delta. Returns ``None`` (caller falls back to a full :func:`dispatch_review`)
    when either diff can't be fetched, the delta comes back empty, no
    reviewer machine is available, or every candidate agent rejects the
    dispatch — never guesses at scope from partial information.

    Returns the new review Assignment (already appended to ``board.active``,
    with ``review_scoped=True`` and ``review_scope_base_sha=prior_review.
    review_head_sha`` for the #1476 audit trail) on success.

    Applies the same two structural guards as :func:`dispatch_review` before
    doing any work: the #522 terminal-work chokepoint (issue closed / PR
    merged — pass a shared *terminal_cache* dict across a bulk pass the same
    way :func:`dispatch_pending_reviews` does) and the #437 TOS-compliance
    gate (refuses a ``human_attended_only`` provider). Reviewer-candidate
    ranking excludes the machine that actually authored *entry*'s branch
    (looked up on *board* via ``entry.assignment_id`` — the work assignment,
    not the prior reviewer) so the scoped review stays independent of the
    code it's judging, mirroring :func:`dispatch_review`'s
    ``completed.machine_name`` contract.

    Does NOT run the #2192 "missing test coverage" nudge that
    :func:`dispatch_review` does ahead of its dispatch — intentional, not an
    oversight: this path only fires after a conflict-fix rebase where a full
    review (nudge included) already happened against the whole PR, and the
    delta reviewed here is scoped to just the rebase's own resolution, not
    the PR's original feature diff.
    """
    if not config.reviews.enabled or not config.reviews.auto_dispatch:
        return None
    if not prior_review.review_head_sha:
        return None

    repo = config.repo(entry.repo_name)
    if repo is None:
        return None

    # #522 (mirrored from dispatch_review): never (re)dispatch a review for
    # work that's already done on GitHub — issue closed OR PR merged. Best
    # effort — a small race window remains before the merge-queue entry is
    # cleaned up, same as the full-review path.
    #
    # #2639: mirror dispatch_review's trust_issue_closed derivation — *entry*
    # is a QueuedMerge, whose `assignment_type` carries the originating
    # assignment's `type` (#1077). A test-author/mock-author entry's
    # `issue_number` is the tracking issue, not its own deliverable.
    if github_ops.work_is_terminal(
        repo.github,
        entry.issue_number,
        entry.branch,
        cache=terminal_cache,
        trust_issue_closed=trust_issue_closed_for(entry.assignment_type),
    ):
        return None

    # #437: STRUCTURAL TOS-COMPLIANCE GATE — mirrored from dispatch_review
    # (coord/review.py ~1463). Scoped reviews are dispatched from the same
    # unattended paths (reconcile()/coord notify) as a full review, so they
    # must be refused exactly the same way when the effective provider is
    # `human_attended_only` (interactive Claude Code via PTY, ToS §3.7).
    # Without this gate a repo/provider configured that way could have a
    # scoped review silently routed to it — the #1476 findings called this
    # out explicitly as a gap versus dispatch_review.
    # #1811: same review-only provider override as dispatch_review — see
    # its call site's comment for the precedence/no-op-when-unset rationale.
    from coord.providers import guard_unattended_dispatch  # noqa: PLC0415
    try:
        review_provider_name = guard_unattended_dispatch(
            spec_provider=config.reviews.provider,
            repo_provider=repo.provider,
            providers_cfg=config.providers,
            models_cfg=config.models,
            where="auto-dispatch scoped review",
        )
    except ValueError as exc:
        log.warning("[review] skipping auto-dispatch scoped review: %s", exc)
        return None

    base_branch = entry.target_branch or repo.default_branch

    _diff = diff_fetcher or github_ops.get_compare_diff
    try:
        old_diff = _diff(repo.github, base_branch, prior_review.review_head_sha)
    except Exception:  # noqa: BLE001 — fail-safe: unfetchable old diff → no scope
        old_diff = None
    try:
        new_diff = _diff(repo.github, base_branch, entry.branch)
    except Exception:  # noqa: BLE001
        new_diff = None

    delta = compute_resolution_delta(old_diff, new_diff)
    if delta is None:
        log.warning(
            "[review] scoped review for merge entry %s: could not compute a "
            "resolution delta (old/new diff unavailable or identical) — "
            "caller should fall back to a full review",
            entry.assignment_id,
        )
        return None

    # #1476 fix: rank candidates against the machine that authored the code
    # under review — the WORK assignment behind *entry* — not the prior
    # reviewer's machine. ``QueuedMerge`` doesn't carry the worker's machine
    # name directly, so look up the work assignment on *board* by
    # ``entry.assignment_id``, exactly mirroring how ``dispatch_review``
    # passes ``completed.machine_name`` (``completed`` *is* the work
    # assignment there). Fall back to the prior reviewer's machine only if
    # the work assignment can no longer be found on the board (defensive;
    # keeps this fail-open rather than raising).
    worker_assignment = board.find_by_id(entry.assignment_id)
    worker_machine_name = (
        worker_assignment.machine_name if worker_assignment is not None
        else prior_review.machine_name
    )
    candidates = _ranked_reviewer_candidates(
        worker_machine_name, entry.repo_name, board, config
    )
    if not candidates:
        return None

    review_model_alias = config.models.default
    review_model_wire = config.models.resolve(review_model_alias)

    _get_sha = branch_sha_fetcher or github_ops.get_branch_sha
    review_head_sha: str | None = None
    try:
        review_head_sha = _get_sha(repo.github, entry.branch)
    except Exception:  # noqa: BLE001 — fail-safe: missing SHA is not blocking
        pass

    _compute_patch_id = patch_id_computer or github_ops.compute_patch_id
    review_patch_id: str | None = None
    try:
        review_patch_id = _compute_patch_id(new_diff)
    except Exception:  # noqa: BLE001
        pass

    client = http_client or httpx
    for machine, _same_as_worker in candidates:
        repo_path = machine.repo_path(entry.repo_name)
        if repo_path is None:
            continue

        briefing = build_scoped_review_briefing(
            pr_number=entry.pr_number,
            pr_url=entry.pr_url,
            repo_github=repo.github,
            repo_name=repo.name,
            issue_number=entry.issue_number,
            issue_title=entry.issue_title,
            branch=entry.branch,
            resolution_delta=delta,
            default_branch=base_branch,
        )

        payload = {
            "repo_name": entry.repo_name,
            "repo_path": repo_path,
            "issue_number": entry.issue_number,
            "issue_title": f"[scoped-review] {entry.issue_title}",
            "briefing": briefing,
            "files_allowed": [],
            "files_forbidden": [],
            "pull_repos": [],
            "type": "review",
            "model": review_model_wire,
            "system_prompt": REVIEWER_SYSTEM_PROMPT,
            "review_target": str(entry.pr_number) if entry.pr_number else entry.branch,
            "branch": base_branch or "main",
        }
        # #1811: mirror dispatch_review's wire-provider threading — see its
        # payload comment for why omitting this silently strands the
        # resolved provider at the TOS-gate check above.
        from coord.dispatch import _wire_payload_needs_provider_field  # noqa: PLC0415

        if review_provider_name and _wire_payload_needs_provider_field(
            review_provider_name, config,
        ):
            payload["provider"] = review_provider_name

        url = f"http://{machine.host}:{AGENT_PORT}/assign"
        try:
            resp = client.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            agent_response = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            log.warning(
                "[review] scoped-review agent %s unreachable/rejected (%s) — "
                "trying next candidate",
                machine.name, exc,
            )
            continue

        review_assignment = Assignment(
            machine_name=machine.name,
            repo_name=entry.repo_name,
            issue_number=entry.issue_number,
            issue_title=f"[scoped-review] {entry.issue_title}",
            files_allowed=[],
            files_forbidden=[],
            briefing=briefing,
            assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
            status="running",
            branch=entry.branch,
            pr_url=entry.pr_url,
            dispatched_at=now if now is not None else time.time(),
            type="review",
            review_target=str(entry.pr_number) if entry.pr_number else entry.branch,
            # Same parent as the review being superseded — keeps the
            # existing work-chain / fix-loop machinery (has_approved_review,
            # auto_loop's request-changes dispatch) working unmodified.
            review_of_assignment_id=prior_review.review_of_assignment_id,
            model=review_model_alias,
            provider_name=review_provider_name,
            review_head_sha=review_head_sha,
            review_patch_id=review_patch_id,
            # #1476 audit trail.
            review_scoped=True,
            review_scope_base_sha=prior_review.review_head_sha,
        )
        board.active.append(review_assignment)

        from coord.state import record_dispatched_assignment  # noqa: PLC0415
        record_dispatched_assignment(
            assignment=review_assignment,
            repo_github=repo.github,
        )

        return review_assignment

    return None


def dispatch_scoped_reviews_for_queue(
    board: Board,
    config: Config,
    *,
    queue_items: list | None = None,
    http_client: httpx.Client | None = None,
    now: float | None = None,
    diff_fetcher=None,
    branch_sha_fetcher=None,
    branch_patch_id_fetcher=None,
    patch_id_computer=None,
) -> list[Assignment]:
    """Scan the merge queue for entries eligible for a #1476 SCOPED
    re-review and dispatch one for each, instead of leaving them blocked on
    "review required but not approved" until a human notices and manually
    forces a full re-review.

    Mirrors :func:`dispatch_pending_reviews`'s "bounded pass, caller
    persists the board" shape so it slots into the same
    ``reconcile()``/``coord notify`` polling sites; unlike that function it
    also owns the merge-queue read/write itself (``queue_items`` defaults to
    :func:`coord.merge_queue.load_queue`, saved back at the end) since the
    scoped/full distinction is a property of the queue entry, not the board.
    It also mirrors that function's two review-flood-incident (2026-06-08)
    safety mechanisms — the ``reviews.flood_threshold`` surge gate and the
    ``reviews.max_auto_dispatch_per_pass`` per-pass cap — so a batch of
    conflict-fix rebases completing together can't fire an unbounded burst
    of metered ``claude -p`` reviews in a single pass.

    An entry is eligible when: it's ``PENDING`` and review-gated
    (:func:`coord.merge_queue.requires_review`); it does NOT already have an
    approved review (:func:`coord.merge_queue.has_approved_review` — a
    content-identical rebase already carries the approval forward and needs
    nothing further);
    :func:`coord.merge_queue.find_scoped_review_candidate` finds a prior
    `approve`d review voided ONLY by a content-changing rebase; and
    :func:`coord.merge_queue.only_conflict_fix_since_review` confirms no
    other work/fix commit intervened. Entries failing any of these are left
    untouched for the existing full-review paths to handle. A dedupe check
    skips entries where a review dispatched after the prior approval is
    already in flight or completed, so a slow reconcile loop can't fire two
    scoped reviews for the same voided approval.

    Returns the dispatched review Assignments (already on ``board.active``).
    """
    import os

    from coord import merge_queue as mq  # noqa: PLC0415

    if not config.reviews.enabled or not config.reviews.auto_dispatch:
        return []

    items = queue_items if queue_items is not None else mq.load_queue()
    _get_sha = branch_sha_fetcher or github_ops.get_branch_sha
    _get_branch_patch_id = branch_patch_id_fetcher or github_ops.get_branch_patch_id

    eligible: list[tuple] = []  # (entry, prior_review)
    mutated = False
    for entry in items:
        if entry.state != mq.PENDING:
            continue
        if not mq.requires_review(entry, config):
            continue

        if entry.branch_head_sha is None:
            try:
                entry.branch_head_sha = _get_sha(entry.repo_github, entry.branch)
                mutated = True
            except Exception:  # noqa: BLE001 — fail-safe: leave unset
                pass
        if entry.branch_patch_id is None:
            try:
                entry.branch_patch_id = _get_branch_patch_id(
                    entry.repo_github, entry.target_branch, entry.branch
                )
                mutated = True
            except Exception:  # noqa: BLE001
                pass

        if mq.has_approved_review(entry, board):
            continue  # not stale, or a content-identical rebase covers it (#1475)

        prior_review = mq.find_scoped_review_candidate(entry, board)
        if prior_review is None:
            continue  # no scoped candidate — needs a full review, not this path

        if not mq.only_conflict_fix_since_review(entry, board, prior_review):
            continue  # guardrail: another commit intervened — full review required

        pool = list(board.active) + list(board.completed)
        already_handled = any(
            a.type == "review"
            and a.review_of_assignment_id == prior_review.review_of_assignment_id
            and a.assignment_id != prior_review.assignment_id
            and (a.dispatched_at or 0) > (prior_review.dispatched_at or 0)
            for a in pool
        )
        if already_handled:
            continue

        eligible.append((entry, prior_review))

    def _persist() -> None:
        if mutated and queue_items is None:
            mq.save_queue(items)

    if not eligible:
        _persist()
        return []

    # Surge gate — same shape as dispatch_pending_reviews. A sudden surge is
    # the review-flood unmasking signature, so halt entirely and require a
    # human to clear the backlog or opt in.
    threshold = config.reviews.flood_threshold
    override = (
        config.reviews.allow_review_flood
        or os.environ.get("COORD_ALLOW_REVIEW_FLOOD") == "1"
    )
    if threshold and len(eligible) > threshold and not override:
        log.warning(
            "[review] scoped-review flood guard: %d merge-queue entries are "
            "eligible for a scoped re-review (> reviews.flood_threshold=%d). "
            "Refusing bulk dispatch to avoid a metered review flood. Clear "
            "the stale backlog, or set reviews.allow_review_flood: true (or "
            "COORD_ALLOW_REVIEW_FLOOD=1) to override.",
            len(eligible), threshold,
        )
        _persist()
        return []

    # Per-pass cap — the remainder stay PENDING and are picked up next pass.
    cap = config.reviews.max_auto_dispatch_per_pass
    # #522: one terminal-state cache for this whole pass, mirrored from
    # dispatch_pending_reviews, so a backlog of already-merged entries costs
    # one gh lookup per issue, not one per entry revisited.
    terminal_cache: dict = {}
    dispatched: list[Assignment] = []
    for entry, prior_review in eligible:
        if cap and len(dispatched) >= cap:
            break
        review = dispatch_scoped_review(
            entry, prior_review, board, config,
            http_client=http_client,
            now=now,
            diff_fetcher=diff_fetcher,
            branch_sha_fetcher=branch_sha_fetcher,
            patch_id_computer=patch_id_computer,
            terminal_cache=terminal_cache,
        )
        if review is not None:
            dispatched.append(review)

    _persist()
    return dispatched


def _fetch_issue_body(repo_github: str, issue_number: int) -> str:
    """Best-effort fetch of the issue body for context. Empty on failure."""
    try:
        import json
        raw = github_ops._gh(
            "issue", "view", str(issue_number),
            "--repo", repo_github,
            "--json", "body",
        )
        return json.loads(raw).get("body", "") or ""
    except (RuntimeError, ValueError):
        return ""


def _fetch_issue_milestone_number(repo_github: str, issue_number: int) -> int | None:
    """Best-effort fetch of the issue's GitHub Milestone number, or ``None``
    if it has none or the fetch fails (fail-open: #934's ``resolve_base_branch``
    falls back to ``default_branch`` when the milestone is unknown, same as
    when it's genuinely absent). Delegates to ``coord.branch_model.
    fetch_issue_milestone_number`` so every call site fails open the same way.
    """
    from coord.branch_model import fetch_issue_milestone_number  # noqa: PLC0415

    return fetch_issue_milestone_number(repo_github, issue_number)


# ── Headless fix dispatch (dashboard / phone API) ────────────────────────────


def dispatch_headless_fix(
    work: Assignment,
    board: Board,
    config: "Config",
    *,
    parent_type: str = "work",
    http_client=None,
) -> Assignment | None:
    """Dispatch a headless (``claude -p``) fix worker for a stalled pipeline item.

    Called from ``POST /api/pipeline/action action=dispatch_fix`` so the phone
    can unstick a test-fail or request-changes item without attending an
    interactive terminal session.

    ``work`` must be a ``type='work'`` assignment that already has a branch.
    ``parent_type`` selects which failure to address:

    * ``"work"`` — fix a test-gate failure.  The briefing is built from
      ``work.test_reason`` (recorded via ``coord test --fail --reason``).
    * ``"review"`` — fix a request-changes review verdict.  The linked review
      assignment is located on the board and its findings are loaded via the
      multi-source chain in ``_load_review_findings`` (DB cache → local log →
      agent HTTP → GitHub message bus).

    The fix worker is dispatched with ``target_branch=work.branch`` in the
    agent payload so it adds commits to the **existing** ``issue-N-*`` branch
    rather than branching fresh off main.

    Returns the new fix ``Assignment`` (already added to ``board.active``),
    or ``None`` on failure (no capable machine, branch missing, findings
    unresolvable, or iteration limit reached).
    """
    from types import SimpleNamespace as _NS  # noqa: PLC0415

    # Deferred imports to avoid a circular-import cycle:
    # review.py is imported at module level by auto_loop.py, so we cannot
    # import auto_loop at review.py's module level.
    from coord.auto_loop import (  # noqa: PLC0415
        _build_fix_briefing,
        _dispatch_fix,
        _fix_model_for_iteration,
        _load_review_findings,
        _work_is_terminal,
    )
    from coord.state import issue_context_block  # noqa: PLC0415

    if not work.branch:
        return None

    if _work_is_terminal(work, config):
        return None

    next_iteration = (work.review_iteration or 0) + 1
    max_iter = config.pipeline.max_review_iterations
    if next_iteration > max_iter:
        return None

    if parent_type == "review":
        # Find the review assignment linked to this work and load its findings.
        all_assignments = list(board.active) + list(board.completed)
        review_a: Assignment | None = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id
                and a.type == "review"
            ),
            None,
        )
        if review_a is None:
            return None

        repo = config.repo(work.repo_name)
        repo_github = repo.github if repo is not None else None
        findings = _load_review_findings(
            review_a,
            None,          # no local log path on the dashboard machine
            None,          # no remote agent host — let GitHub fallback handle it
            repo_github=repo_github,
        )
        if findings is not None:
            findings_obj = findings
        else:
            # Fallback: generic pointer so the worker can still proceed.
            verdict = getattr(review_a, "review_verdict", None) or "request-changes"
            findings_obj = _NS(body=(
                f"(No structured findings were captured for review "
                f"{review_a.assignment_id}.) "
                f"The review verdict was {verdict!r}. "
                "Read the reviewer's feedback on the PR / issue comments and "
                "address every blocking item before pushing."
            ))
    else:
        # parent_type == "work": test-gate failure.
        test_story = (getattr(work, "test_reason", None) or "").strip()
        if test_story:
            findings_obj = _NS(body=(
                "The manual smoke test FAILED.  The operator reported:\n\n"
                f"> {test_story}\n\n"
                "Reproduce the failure, fix the root cause, and re-validate "
                "before pushing."
            ))
        else:
            findings_obj = _NS(body=(
                "The manual smoke test FAILED (no reason text was recorded). "
                "Pull the branch, reproduce the failure the operator hit, "
                "and fix the root cause before pushing."
            ))

    briefing = (
        issue_context_block(work.repo_name, work.issue_number)
        + _build_fix_briefing(work, findings_obj, next_iteration, max_iter)
    )
    model = _fix_model_for_iteration(config, next_iteration)
    return _dispatch_fix(
        work, briefing, board, config, next_iteration,
        model=model, http_client=http_client,
    )
