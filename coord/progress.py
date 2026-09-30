"""Parse worker progress signals from log output.

Two paths are supported:

* **stream-json**: when the worker was launched with
  ``--output-format stream-json --verbose`` (the new default), each log line
  is a structured event. We delegate to the assignment's resolved
  :class:`~coord.providers.base.Provider` (``provider.parse_log()`` — #1710),
  defaulting to :class:`~coord.providers.claude.ClaudeProvider` (which itself
  delegates to :mod:`coord.worker_events`) when no provider is known — so
  ``parse_progress`` produces byte-identical output to before #1710 for every
  existing (claude) caller that doesn't pass ``provider``/``provider_name``.
* **plain text** (legacy): we fall back to the old ``STATUS:``/``STUCK:``
  regex scan for backwards compatibility with logs from older agents and
  for non-claude worker commands used in tests. This path is
  provider-agnostic by construction — any worker that emits ``STATUS:``/
  ``STUCK:`` lines in plain text is handled the same way regardless of which
  provider ran it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coord.config import Config
    from coord.providers.base import Provider


STATUS_RE = re.compile(r"^STATUS:\s*(.+)$", re.MULTILINE)
STUCK_RE = re.compile(r"^STUCK:\s*(.+)$", re.MULTILINE)
CONFIDENCE_RE = re.compile(r"confidence:\s*(high|medium|low)", re.IGNORECASE)

# #252: workers emit a SMOKE_TESTS block before exiting.  The whole block
# is captured (greedy across newlines) and parsed below.  Optional `(none
# — change is internal)` form folds to an empty list.
_SMOKE_BLOCK_RE = re.compile(
    r"SMOKE_TESTS:\s*(.*?)\s*END_SMOKE_TESTS",
    re.DOTALL | re.IGNORECASE,
)
_SMOKE_NONE_RE = re.compile(
    r"^\(?\s*none\b.*?(?:internal|change)?\s*\)?\s*$",
    re.IGNORECASE,
)
_SMOKE_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")

# The three verdict markers the headless Test-stage smoke agent is told to
# print (`coord.smoke.SMOKE_SYSTEM_PROMPT`):
#
#   * `SMOKE: pass`
#   * `SMOKE: fail <reason>`
#   * `SMOKE: baseline-red <reason>` — #2170: the smoke command's own failures
#     reproduce identically on the merge-base, so this is a statement about
#     the MACHINE, not the branch.
#
# #2170 parsed only `baseline-red`; #2244 parses all three. Before that, the
# pass/fail verdict was taken from the session exit code instead — but a
# `claude -p` worker CANNOT signal through its exit code (an `exit 1` inside a
# Bash tool call ends that tool call, not the session, which still ends
# `end_turn` and exits 0). So a smoke run that found five real failures and
# printed `SMOKE: fail` was recorded as `test_state=passed`: the whole
# Test-green/CI-red class (#2091, #2182, #2143, #2230).
#
# Line-anchored, mirroring `coord.revalidate`'s `is_baseline_red_failure`
# marker discipline: never a bare substring search, so an assertion message,
# a briefing quote, or a prose sentence that merely mentions "SMOKE: fail"
# mid-line can't forge a verdict. Common inflections (`passed`, `failed`) are
# accepted because the marker is written by a language model, and rejecting
# `SMOKE: failed` would silently downgrade a real failure to "no verdict".
_SMOKE_VERDICT_RE = re.compile(
    r"^SMOKE:[ \t]*(baseline-red|pass(?:ed)?|fail(?:ed|ure|ing)?)\b[ \t]*(.*)$",
    re.MULTILINE | re.IGNORECASE,
)


#: #2244: `echo "SMOKE: fail <reason>"` inside a Bash tool call — the way the
#: marker is most often actually emitted. Captures the quoted literal so it
#: can be re-anchored as its own line (see `_smoke_verdict_texts_from_event`).
_ECHOED_SMOKE_RE = re.compile(
    r"""(?:^|[\n;&|]\s*)(?:echo|printf)\s+(?:-[eEn]+\s+)?["'](SMOKE:[^"']*)["']""",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass(frozen=True)
class SmokeVerdict:
    """#2244: a smoke worker's self-reported verdict line.

    ``kind`` is normalised to exactly one of ``"pass"``, ``"fail"`` or
    ``"baseline-red"``; ``reason`` is the (possibly empty) trailing text.
    """

    kind: str
    reason: str = ""


@dataclass
class WorkerProgress:
    updates: list[str] = field(default_factory=list)
    stuck: str | None = None
    warnings: list[str] = field(default_factory=list)
    latest_confidence: str | None = None

    def to_dict(self) -> dict:
        return {
            "updates": self.updates,
            "stuck": self.stuck,
            "warnings": self.warnings,
            "latest_confidence": self.latest_confidence,
        }


def parse_progress(
    log_path: str | Path,
    tail_bytes: int = 32_768,
    *,
    provider_name: str | None = None,
    provider: "Provider | None" = None,
    cfg: "Config | None" = None,
) -> WorkerProgress:
    """Parse progress from a worker log.

    Detects stream-json automatically and switches parsing strategies. Reads
    only the tail of the log to stay cheap on large files.

    Args:
        log_path: Path to the worker's log file.
        tail_bytes: Only the last *tail_bytes* of the file is read.
        provider_name: The assignment's resolved provider name (e.g.
            ``Assignment.provider_name``). ``None`` (the default) resolves to
            :class:`~coord.providers.claude.ClaudeProvider` — the pre-#1710
            behaviour — so existing callers that don't pass this are
            unaffected.
        provider: An already-constructed :class:`~coord.providers.base.Provider`
            to use directly, bypassing name resolution entirely. Takes
            precedence over *provider_name*. Mainly for tests that want to
            exercise a specific (e.g. fake, non-claude) provider without
            wiring up a full :class:`~coord.config.Config`.
        cfg: The coordinator's :class:`~coord.config.Config`, when the caller
            has one in scope. #2305: without this, ``provider_name`` only
            ever resolves against the built-in provider *types*
            (``claude``/``claude-pty``/``opencode``) — a **tier-named**
            ``providers.definitions`` entry (e.g. ``oc-mid``) silently
            degrades to :class:`~coord.providers.claude.ClaudeProvider`.
            Passed straight through to :func:`coord.providers.get_provider`;
            ``None`` (the default) preserves the pre-#2305 built-ins-only
            resolution, so existing callers are unaffected.
    """
    from coord.worker_events import detect_anomalies, is_stream_json

    p = Path(log_path)
    if not p.exists():
        return WorkerProgress()

    if is_stream_json(p):
        if provider is None:
            from coord.providers import get_provider  # noqa: PLC0415
            provider = get_provider(provider_name, cfg=cfg)
        summary = provider.parse_log(p, tail_bytes=tail_bytes)
        progress = WorkerProgress()
        # Synthesise a single rolling "update" line so coord status keeps
        # showing recent activity for stream-json workers.
        if summary.num_turns or summary.last_tool:
            tool_part = summary.last_tool or "thinking"
            progress.updates.append(f"Turn {summary.num_turns}: {tool_part}")
        # Surface anomaly patterns as warnings.
        progress.warnings.extend(detect_anomalies(p, tail_bytes=tail_bytes))
        if summary.stop_reason and summary.stop_reason not in (
            "end_turn",
            "stop_sequence",
            None,
        ):
            progress.warnings.append(f"unusual stop: {summary.stop_reason}")
        return progress

    # ── Plain-text fallback ───────────────────────────────────────────────
    size = p.stat().st_size
    with open(p) as f:
        if size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()  # skip partial line
        text = f.read()

    updates = STATUS_RE.findall(text)
    stuck_matches = STUCK_RE.findall(text)

    progress = WorkerProgress(
        updates=updates[-10:],
        stuck=stuck_matches[-1] if stuck_matches else None,
    )

    # Extract latest confidence
    if updates:
        conf = CONFIDENCE_RE.search(updates[-1])
        if conf:
            progress.latest_confidence = conf.group(1).lower()

    # Detect warning patterns
    _detect_warnings(progress, updates)

    return progress


def _extract_smoke_tests_from_text(text: str) -> list[str] | None:
    """#252: pull the SMOKE_TESTS block out of *text*.

    Returns:
      * ``None`` — no block emitted (graceful degradation; the TUI shows
        a "no smoke tests provided" placeholder).
      * ``[]`` — explicit "(none — change is internal)" form.
      * ``list[str]`` — one entry per bullet, stripped of leading "- "/"* ".

    Tolerant of leading/trailing whitespace and stray empty lines.  Picks
    the LAST block in the text — workers occasionally redo their summary
    if they reconsider the change.
    """
    matches = list(_SMOKE_BLOCK_RE.finditer(text))
    if not matches:
        return None
    block = matches[-1].group(1)

    # Try the "(none — change is internal)" short form on the first
    # non-empty line of the captured block.
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _SMOKE_NONE_RE.match(stripped):
            return []
        break

    bullets: list[str] = []
    for line in block.splitlines():
        m = _SMOKE_BULLET_RE.match(line)
        if m:
            item = m.group(1).strip()
            if item:
                bullets.append(item)
    # If the block existed but had no bullets and wasn't the "none" form
    # either, treat as "no smoke tests provided" (None) — the worker
    # didn't actually fill in the template.
    if not bullets:
        return None
    return bullets


def parse_smoke_tests_from_log(
    log_path: str | Path, tail_bytes: int = 65_536,
) -> list[str] | None:
    """#252: read the tail of *log_path* and extract any SMOKE_TESTS block.

    Handles both stream-json logs (decodes assistant text events first)
    and legacy plain-text logs.  Returns the same three-state result as
    :func:`_extract_smoke_tests_from_text`.
    """
    p = Path(log_path)
    if not p.exists():
        return None

    from coord.worker_events import is_stream_json  # noqa: PLC0415

    if is_stream_json(p):
        # Collect assistant text from the structured events.  Workers may
        # emit the block in a single assistant turn, so concatenating all
        # of them is enough.
        from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415
        texts: list[str] = []
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    event = parse_event(line.rstrip("\n"))
                    if event is None or event.type != "assistant":
                        continue
                    t = _assistant_text(event)
                    if t:
                        texts.append(t)
        except OSError:
            return None
        return _extract_smoke_tests_from_text("\n".join(texts))

    # Plain-text path: read the tail (large enough to catch the block
    # even when followed by many turns of build output).
    try:
        size = p.stat().st_size
        with open(p, encoding="utf-8", errors="replace") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # skip partial line
            text = f.read()
    except OSError:
        return None
    return _extract_smoke_tests_from_text(text)


def parse_smoke_tests_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> list[str] | None:
    """#252: fetch a worker's log via the agent's ``/logs/<id>`` endpoint
    and extract the SMOKE_TESTS block.

    Use this instead of :func:`parse_smoke_tests_from_log` when the worker
    ran on a remote agent and the log isn't on the coordinator's local
    filesystem.  Mirrors :func:`coord.review.parse_review_from_agent` and
    :func:`coord.plan_parser.parse_plan_from_agent`.  Returns the same
    three-state result as :func:`_extract_smoke_tests_from_text`.
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

    # Detect stream-json the same way is_stream_json() does for files.
    stream_json = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break

    if stream_json:
        from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415
        texts: list[str] = []
        for line in text.splitlines():
            event = parse_event(line.rstrip("\n"))
            if event is None or event.type != "assistant":
                continue
            t = _assistant_text(event)
            if t:
                texts.append(t)
        return _extract_smoke_tests_from_text("\n".join(texts))

    return _extract_smoke_tests_from_text(text)


# #874: workers emit a "### Summary" markdown heading before finishing.
# The block extends to the next "###" heading or to end-of-text.  We
# pick the LAST occurrence (workers may recap after final fixes).
#
# NOTE: use [ \t]* (horizontal whitespace only) in the prefix — NOT \s* —
# so the blank line after the heading is NOT consumed by the prefix match.
# With re.DOTALL, \s* would greedily eat the blank \n, shifting the
# captured group to start at the *next* ### heading's content instead.
_SUMMARY_BLOCK_RE = re.compile(
    r"###[ \t]+Summary[ \t]*\n(.*?)(?=\n###[ \t]|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _extract_completion_summary_from_text(text: str) -> str | None:
    """#874: pull the ### Summary block out of *text*.

    Returns:
      * ``None`` — no "### Summary" heading found.
      * ``str``  — the prose text immediately under the heading, stripped
        of leading/trailing whitespace.  Empty string is folded to None
        (an empty heading is treated as absent).

    Picks the LAST block in the text in case the worker re-emitted the
    section after further edits.
    """
    matches = list(_SUMMARY_BLOCK_RE.finditer(text))
    if not matches:
        return None
    prose = matches[-1].group(1).strip()
    return prose if prose else None


def parse_completion_summary_from_log(
    log_path: str | Path, tail_bytes: int = 65_536,
) -> str | None:
    """#874: read the tail of *log_path* and extract any ### Summary block.

    Handles both stream-json logs (decodes assistant text events first)
    and legacy plain-text logs.  Returns the same two-state result as
    :func:`_extract_completion_summary_from_text`.
    """
    p = Path(log_path)
    if not p.exists():
        return None

    from coord.worker_events import is_stream_json  # noqa: PLC0415

    if is_stream_json(p):
        from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415
        texts: list[str] = []
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    event = parse_event(line.rstrip("\n"))
                    if event is None or event.type != "assistant":
                        continue
                    t = _assistant_text(event)
                    if t:
                        texts.append(t)
        except OSError:
            return None
        return _extract_completion_summary_from_text("\n".join(texts))

    try:
        size = p.stat().st_size
        with open(p, encoding="utf-8", errors="replace") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # skip partial line
            text = f.read()
    except OSError:
        return None
    return _extract_completion_summary_from_text(text)


def parse_completion_summary_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> str | None:
    """#874: fetch a worker's log via the agent's ``/logs/<id>`` endpoint
    and extract the ### Summary block.

    Use this instead of :func:`parse_completion_summary_from_log` when the
    worker ran on a remote agent and the log isn't on the coordinator's local
    filesystem.  Mirrors :func:`parse_smoke_tests_from_agent`.
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

    # Detect stream-json the same way is_stream_json() does for files.
    stream_json = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break

    if stream_json:
        from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415
        decoded: list[str] = []
        for line in text.splitlines():
            event = parse_event(line.rstrip("\n"))
            if event is None or event.type != "assistant":
                continue
            t = _assistant_text(event)
            if t:
                decoded.append(t)
        return _extract_completion_summary_from_text("\n".join(decoded))

    return _extract_completion_summary_from_text(text)


def _extract_smoke_verdict_from_text(text: str) -> "SmokeVerdict | None":
    """#2244: pull the last `SMOKE: pass|fail|baseline-red <reason>` line out
    of *text*.

    Returns ``None`` when the worker printed no verdict line at all — the same
    "None means absent" contract as every other extractor here. Absent is NOT
    a pass: the caller (`coord.notify`) fails closed on it.

    Picks the LAST match of ANY kind, matching every other block-extraction
    parser in this module: a worker that restates its verdict (or corrects
    itself — "I said baseline-red, but re-running shows a real failure")
    means the final line, not the first.
    """
    matches = list(_SMOKE_VERDICT_RE.finditer(text))
    if not matches:
        return None
    raw_kind = matches[-1].group(1).lower()
    if raw_kind.startswith("baseline"):
        kind = "baseline-red"
    elif raw_kind.startswith("pass"):
        kind = "pass"
    else:
        kind = "fail"
    return SmokeVerdict(kind=kind, reason=matches[-1].group(2).strip())


def _smoke_verdict_texts_from_event(event: object) -> list[str]:
    """#2244: every place a `SMOKE:` verdict line can appear in ONE
    stream-json event.

    A smoke worker rarely says its verdict in prose — the log this issue was
    filed from ends with the worker running

        echo "SMOKE: fail 5 failed + 3 errors ..." >&2; exit 1

    in a Bash **tool call**. That text lives in the assistant event's
    ``tool_use`` input and again in the following ``tool_result``; it is in no
    assistant *text* block at all, so the assistant-text-only decode every
    other parser in this module uses would see an empty transcript and report
    "no verdict" for a run that clearly stated one. Hence all four sources:

    * assistant ``text`` blocks (the worker states the verdict in prose),
    * assistant ``tool_use`` Bash ``command`` strings (it echoes it),
    * ``tool_result`` payloads (what the command actually printed),
    * the final ``result`` event's text.

    Deliberately NOT included: a ``user`` event's plain-string content. That
    is the initial briefing, which quotes the marker names as instructions —
    reading it back would let the coordinator's own prompt forge a verdict.
    (Backtick-quoting in the prompt already defeats the line anchor, but not
    depending on that is cheaper than depending on it.)
    """
    from coord.worker_events import _iter_content_blocks  # noqa: PLC0415

    raw = getattr(event, "raw", None) or {}
    etype = getattr(event, "type", "")
    out: list[str] = []

    if etype == "result":
        result = raw.get("result")
        if isinstance(result, str):
            out.append(result)
        return out

    if etype not in ("assistant", "user"):
        return out

    message = raw.get("message") or {}
    for block in _iter_content_blocks(message):
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text")
            if isinstance(txt, str):
                out.append(txt)
        elif btype == "tool_use":
            command = (block.get("input") or {}).get("command") \
                if isinstance(block.get("input"), dict) else None
            if isinstance(command, str):
                out.append(command)
                # `echo "SMOKE: fail ..."` puts the marker mid-line inside a
                # shell command, where the line anchor can't see it. The
                # command's own OUTPUT (the tool_result above) normally
                # carries it properly anchored, but stderr redirection and
                # truncated results make that less than certain — so lift the
                # echoed literal out too. Only a quoted argument to
                # echo/printf qualifies; nothing else in a command line can be
                # mistaken for the worker's own verdict.
                out.extend(_ECHOED_SMOKE_RE.findall(command))
        elif btype == "tool_result":
            content = block.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        out.append(part["text"])
    return out


def _smoke_text_from_log(log_path: str | Path, tail_bytes: int) -> str | None:
    """Read a worker log as plain text, decoding stream-json events first when
    that's what it is. ``None`` on any read failure."""
    p = Path(log_path)
    if not p.exists():
        return None

    from coord.worker_events import is_stream_json  # noqa: PLC0415

    if is_stream_json(p):
        from coord.worker_events import parse_event  # noqa: PLC0415
        texts: list[str] = []
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    event = parse_event(line.rstrip("\n"))
                    if event is None:
                        continue
                    texts.extend(_smoke_verdict_texts_from_event(event))
        except OSError:
            return None
        return "\n".join(texts)

    try:
        size = p.stat().st_size
        with open(p, encoding="utf-8", errors="replace") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # skip partial line
            return f.read()
    except OSError:
        return None


def _smoke_text_from_agent(
    host: str, assignment_id: str, port: int, timeout: float,
) -> str | None:
    """Fetch a worker log through the agent's ``/logs/<id>`` endpoint and
    return it as plain text (stream-json decoded). ``None`` on any failure."""
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

    stream_json = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break

    if not stream_json:
        return text

    from coord.worker_events import parse_event  # noqa: PLC0415
    decoded: list[str] = []
    for line in text.splitlines():
        event = parse_event(line.rstrip("\n"))
        if event is None:
            continue
        decoded.extend(_smoke_verdict_texts_from_event(event))
    return "\n".join(decoded)


def parse_smoke_verdict_from_log(
    log_path: str | Path, tail_bytes: int = 65_536,
) -> "SmokeVerdict | None":
    """#2244: read the tail of *log_path* and extract the worker's
    `SMOKE: pass|fail|baseline-red` verdict line, if it printed one.

    Handles both stream-json logs (decoded by
    :func:`_smoke_verdict_texts_from_event`, which looks in tool calls and
    tool results as well as assistant prose) and legacy plain-text logs.
    ``None`` means "no verdict in the transcript" — never "passed".
    """
    text = _smoke_text_from_log(log_path, tail_bytes)
    if text is None:
        return None
    return _extract_smoke_verdict_from_text(text)


def parse_smoke_verdict_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> "SmokeVerdict | None":
    """#2244: fetch a worker's log via the agent's ``/logs/<id>`` endpoint and
    extract its `SMOKE:` verdict line.

    Use this instead of :func:`parse_smoke_verdict_from_log` when the worker
    ran on a remote agent and the log isn't on the coordinator's local
    filesystem. Mirrors :func:`parse_smoke_tests_from_agent`.
    """
    text = _smoke_text_from_agent(host, assignment_id, port, timeout)
    if text is None:
        return None
    return _extract_smoke_verdict_from_text(text)


def _extract_smoke_baseline_red_from_text(text: str) -> str | None:
    """#2170: the baseline-red reason from *text*, or ``None`` if the last
    `SMOKE:` verdict line wasn't a baseline-red one.

    Kept as the narrow #2170 accessor on top of the #2244 general parser, so
    the two can never disagree about which line is the verdict.
    """
    verdict = _extract_smoke_verdict_from_text(text)
    if verdict is None or verdict.kind != "baseline-red":
        return None
    return verdict.reason


def parse_smoke_baseline_red_from_log(
    log_path: str | Path, tail_bytes: int = 65_536,
) -> str | None:
    """#2170: read the tail of *log_path* and extract a `SMOKE: baseline-red`
    verdict line, if the worker printed one."""
    verdict = parse_smoke_verdict_from_log(log_path, tail_bytes)
    if verdict is None or verdict.kind != "baseline-red":
        return None
    return verdict.reason


def parse_smoke_baseline_red_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> str | None:
    """#2170: fetch a worker's log via the agent's ``/logs/<id>`` endpoint and
    extract a `SMOKE: baseline-red` verdict line."""
    verdict = parse_smoke_verdict_from_agent(host, assignment_id, port, timeout)
    if verdict is None or verdict.kind != "baseline-red":
        return None
    return verdict.reason


def _detect_warnings(progress: WorkerProgress, all_updates: list[str]) -> None:
    if progress.stuck:
        progress.warnings.append("worker is STUCK and waiting for guidance")

    # Two consecutive low-confidence updates
    confidences = []
    for u in all_updates[-5:]:
        m = CONFIDENCE_RE.search(u)
        if m:
            confidences.append(m.group(1).lower())
    if len(confidences) >= 2 and confidences[-1] == "low" and confidences[-2] == "low":
        progress.warnings.append("confidence dropped to low on consecutive updates")
