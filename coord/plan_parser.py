"""Parse structured plan output from plan-only worker logs.

Plan workers (``type="plan"`` assignments) are instructed to output a fixed
set of section headings.  This module extracts those headings from either a
plain-text log or a stream-json log and returns a :class:`WorkerPlan`
dataclass.

Recognised headings (as defined in ``WORKER_PLAN_PROMPT`` in ``coord.agent``):

    PLAN:        (optional) short one-line summary of the overall plan
    FILES_READ:  comma-separated list of every file the worker examined
    FILES_MODIFY: comma-separated list of files that would need to change
    APPROACH:    concise implementation approach (multi-sentence)
    RISKS:       potential blockers, conflicts, or tricky areas
    ESTIMATE:    rough complexity: trivial | small | medium | large

Plus a fenced ``SMOKE_TESTS:`` / ``END_SMOKE_TESTS`` block — bullets the
human should run after the work lands.  Parsed the same way the work
worker's end-of-job block is (``coord.progress.parse_smoke_tests``) so
the two sources are interchangeable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# All section keywords the parser recognises.  Order matters for display but
# not for parsing.
SECTION_KEYWORDS = ["PLAN", "FILES_READ", "FILES_MODIFY", "APPROACH", "RISKS", "ESTIMATE"]

# Match a section header at the start of a line, capturing the keyword.
# Tolerates optional leading markdown (e.g. "### FILES_READ:" or "**FILES_READ:**").
# The trailing optional \*{1,2} handles bold-close after the colon: **KEYWORD:**
_SECTION_RE = re.compile(
    r"^(?:[#*]+\s+|\*{1,2})?(" + "|".join(SECTION_KEYWORDS) + r"):(?:\*{1,2})?\s*",
    re.MULTILINE,
)


@dataclass
class WorkerPlan:
    """Structured plan extracted from a plan-only worker log."""

    plan: str = ""
    files_read: list[str] = field(default_factory=list)
    files_modify: list[str] = field(default_factory=list)
    approach: str = ""
    risks: str = ""
    estimate: str = ""
    # SMOKE_TESTS block emitted by the plan worker.  Tri-state, same shape as
    # Assignment.smoke_tests / parse_smoke_tests_from_log:
    #   None — no block emitted (or block was empty/malformed)
    #   []   — explicit "(none — change is internal)"
    #   list — one bullet per item
    smoke_tests: list[str] | None = None
    raw_text: str = ""

    def is_empty(self) -> bool:
        """True iff no structured sections were found."""
        return not any(
            [
                self.plan,
                self.files_read,
                self.files_modify,
                self.approach,
                self.risks,
                self.estimate,
                self.smoke_tests is not None,
            ]
        )

    def to_dict(self) -> dict:
        return {
            "plan": self.plan,
            "files_read": self.files_read,
            "files_modify": self.files_modify,
            "approach": self.approach,
            "risks": self.risks,
            "estimate": self.estimate,
            "smoke_tests": self.smoke_tests,
            "raw_text": self.raw_text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkerPlan:
        return cls(
            plan=data.get("plan", ""),
            files_read=data.get("files_read", []),
            files_modify=data.get("files_modify", []),
            approach=data.get("approach", ""),
            risks=data.get("risks", ""),
            estimate=data.get("estimate", ""),
            smoke_tests=data.get("smoke_tests"),
            raw_text=data.get("raw_text", ""),
        )


# ── Text extraction ──────────────────────────────────────────────────────────


def _extract_text_from_stream_json(log_path: Path) -> str:
    """Concatenate all assistant text blocks from a stream-json log.

    #1710 inventory: kept as a direct ``coord.worker_events`` import rather
    than routed through ``provider.parse_log()`` — this decodes the generic
    Anthropic-Messages-API ``type: "assistant"`` / ``message.content``
    envelope (a wire-format detail, not claude business semantics), and
    ``WorkerSummary`` has no "raw assistant text" field to route through
    without adding a new abstract ``Provider`` method (which would force a
    change in every concrete provider, including the out-of-scope
    ``opencode.py``). Same reasoning as ``coord.review``'s equivalent
    helpers; see ``tests/test_provider_seam.py``.
    """
    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    parts: list[str] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                event = parse_event(line.rstrip("\n"))
                if event is None:
                    continue
                if event.type == "assistant":
                    text = _assistant_text(event)
                    if text:
                        parts.append(text)
    except OSError:
        pass
    return "\n".join(parts)


# ── Section parsing ──────────────────────────────────────────────────────────


def _parse_sections(text: str) -> dict[str, str]:
    """Split *text* on section headers and return a keyword → content dict."""
    if not text:
        return {}
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return {}
    result: dict[str, str] = {}
    for i, m in enumerate(matches):
        keyword = m.group(1)
        content_start = m.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[keyword] = text[content_start:content_end].strip()
    return result


def _parse_file_list(value: str) -> list[str]:
    """Split a comma-separated file list, stripping blank entries."""
    if not value:
        return []
    return [f.strip() for f in value.split(",") if f.strip()]


# ── Public API ───────────────────────────────────────────────────────────────


def parse_plan_text(text: str) -> WorkerPlan:
    """Parse structured plan sections from raw text.

    Returns a :class:`WorkerPlan` whose ``raw_text`` is always set to *text*,
    and whose section fields are populated from any recognised headings found.
    """
    from coord.progress import _extract_smoke_tests_from_text  # noqa: PLC0415

    sections = _parse_sections(text)
    return WorkerPlan(
        plan=sections.get("PLAN", ""),
        files_read=_parse_file_list(sections.get("FILES_READ", "")),
        files_modify=_parse_file_list(sections.get("FILES_MODIFY", "")),
        approach=sections.get("APPROACH", ""),
        risks=sections.get("RISKS", ""),
        estimate=sections.get("ESTIMATE", ""),
        smoke_tests=_extract_smoke_tests_from_text(text),
        raw_text=text,
    )


def parse_plan_from_log(log_path: str | Path) -> WorkerPlan | None:
    """Parse a :class:`WorkerPlan` from a worker log file.

    Handles both stream-json (``--output-format stream-json``) and plain-text
    log formats.  Returns ``None`` if the file does not exist or contains no
    recognised plan sections.

    #1710 inventory: ``is_stream_json`` is a generic on-disk-shape sniff, not
    claude-specific parsing — kept direct. See the note on
    ``_extract_text_from_stream_json`` above.
    """
    from coord.worker_events import is_stream_json  # noqa: PLC0415

    p = Path(log_path)
    if not p.exists():
        return None

    if is_stream_json(p):
        text = _extract_text_from_stream_json(p)
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    plan = parse_plan_text(text)
    return None if plan.is_empty() else plan


def fetch_raw_text_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> str | None:
    """Fetch a worker's assistant-text transcript via the agent's
    ``/logs/<id>`` endpoint, regardless of what the worker's own system
    prompt asked it to format the reply as.

    The extraction half of :func:`parse_plan_from_agent` (#3945 split):
    that function additionally parses ``WORKER_PLAN_PROMPT``'s own
    FILES_READ/FILES_MODIFY/APPROACH/RISKS/ESTIMATE headings and discards
    the result when none are found, which is exactly wrong for a
    ``type="plan"`` dispatch given a *different* system prompt (e.g. the
    board-doctor follow-through's own JSON-plan contract,
    :func:`coord.board_doctor_followthrough.build_plan_leg_prompt`) — this
    is the one both callers share. Returns ``None`` on network failure or
    an empty log, the text itself otherwise (empty string included).
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

    # The agent returns stream-json verbatim when the worker was launched
    # with --output-format stream-json (the standard).  Detect the same way
    # is_stream_json() does for files: first non-comment, non-blank line
    # starts with `{`.
    stream_json = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break

    if not stream_json:
        return text

    # Reuse the stream-json text extractor by writing to a temp file — the
    # existing extractor opens by path.  Cheap enough at log sizes we see
    # in practice (≤ a few MB).
    import tempfile  # noqa: PLC0415
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=True) as tf:
        tf.write(text)
        tf.flush()
        return _extract_text_from_stream_json(Path(tf.name))


def parse_plan_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> WorkerPlan | None:
    """Fetch a plan worker's log via the agent's ``/logs/<id>`` endpoint
    and parse it.

    Use this instead of :func:`parse_plan_from_log` when the plan worker
    ran on a remote agent and the log file isn't on the coordinator's
    local filesystem.  Mirrors :func:`coord.review.parse_review_from_agent`.
    Returns ``None`` on network failure, empty log, or no recognised plan
    sections.
    """
    extracted = fetch_raw_text_from_agent(
        host, assignment_id, port=port, timeout=timeout,
    )
    if extracted is None:
        return None
    plan = parse_plan_text(extracted)
    return None if plan.is_empty() else plan
