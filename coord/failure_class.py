"""Environmental-vs-work failure classification, resume scheduling, and the
pre-relaunch liveness gate (#1590).

WHY THIS EXISTS.  A terminal failure has two very different causes that today
look identical to every consumer:

* **work** — the tests failed, the review rejected the branch, no commits were
  produced, the worker errored out.  Retrying unchanged is pointless; the
  right end state is BLOCKED, in front of a human.
* **environmental** — the Claude API returned 529/overloaded, the account hit
  its usage limit mid-session, the network dropped.  The work is untouched and
  probably fine; the right end state is "wait for the weather, then resume",
  and it must never consume the work-attempt budget.

Three environmental hits currently BLOCK a node whose code is perfectly fine,
and record *"drive died 3x without closing the issue"* — which sends the next
person looking at the work instead of the weather.

WHAT THIS MODULE IS.  Three primitives, in dependency order:

1. :func:`classify_failure` (plus the log/result-event conveniences) — the
   load-bearing decision.  Everything else consumes it.
2. :func:`plan_usage_limit_resume` — turn the ``reset_at_raw`` that
   :class:`coord.worker_events.UsageLimitKill` already parses (and currently
   throws away) into an absolute *resume at* instant.
3. :func:`probe_environment_liveness` / :func:`gate_relaunch` — don't spend a
   retry into a service that is still down; back off with a ceiling in the
   tens of minutes, not 60s.

THE TAXONOMY IS DELIBERATELY LOPSIDED.  Misclassifying a genuine work failure
as ``environmental`` means it retries forever instead of surfacing — strictly
worse than the bug this fixes.  So:

* ``environmental`` requires a **positive, specific** signal from an explicit
  allow-list (usage limit / 5xx-or-429 API status / overloaded / a named
  transport error).  There is no catch-all branch.
* Everything else — including "we have no idea" — is ``work``.
* The text-scanning patterns are anchored on wire-format tokens
  (``api_error_status``, ``overloaded_error``, ``ECONNRESET``) that cannot
  plausibly appear in a coordinator-authored work-failure summary.  A bare
  ``529`` or the word "overloaded" is **not** a signal.

FEED IT TERMINAL SUMMARIES, NOT TRANSCRIPTS.  ``failure_reason`` /
``terminal_reason`` are short strings the coordinator wrote; they are scanned
unconditionally.  ``result_text`` is worker-authored prose and is scanned
**only** when ``is_error`` is truthy — otherwise a worker that merely
*discusses* an outage (this issue's own transcript, for instance) would
classify as environmental.  Never pass a whole log tail as ``result_text``;
use :func:`classify_log`, which pulls the last ``result`` event for you.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#1710 inventory — these four stay as direct ``coord.worker_events`` imports
# rather than routed through a ``Provider``:
# * ``is_usage_limit_reason``/``USAGE_LIMIT_REASON_PREFIX`` are a trivial
#   string-prefix predicate over an already-derived ``failure_reason`` value,
#   not a log-format parse (mirrors the identical note in ``coord.notify``).
# * ``detect_usage_limit_kill``/``detect_usage_limit_kill_in_log`` scan for
#   the literal ``claude`` CLI's own subscription "You've hit your session
#   limit" wording — inherently claude-specific business text with no
#   generic equivalent; ``Capabilities``/``WorkerSummary`` don't model a
#   generic "usage limit kill" concept, and inventing one is out of scope
#   (non-goal: rewriting ``coord.worker_events``).
# ``classify_log`` (below) is the one function here that DOES read a
# provider's own log shape, and it routes through ``provider.parse_log()``.
from coord.worker_events import (
    detect_usage_limit_kill,
    detect_usage_limit_kill_in_log,
    is_usage_limit_reason,
    USAGE_LIMIT_REASON_PREFIX,
)

if TYPE_CHECKING:
    from coord.providers.base import Provider

# ── the two classes ─────────────────────────────────────────────────────────

ENVIRONMENTAL = "environmental"
WORK = "work"

# Environmental sub-kinds. Stable strings — downstream (the sequencer's
# `environment-degraded` bookkeeping) may key off them.
KIND_USAGE_LIMIT = "usage_limit"
KIND_API_ERROR = "api_error"
KIND_NETWORK = "network"
KIND_WORK = "work"

#: HTTP statuses from the Claude API that mean "the provider, not the work".
#: An explicit allow-list, never a range test on "not 2xx": 4xx other than 429
#: (400 bad request, 401/403 auth, 404) are *our* bug or *our* config and must
#: surface, not retry forever.
ENVIRONMENTAL_API_STATUSES = frozenset({429, *range(500, 600)})

#: ``coord.network`` classified states that mean the transport, not the work.
#: ``HTTP_ERROR`` and ``UNKNOWN`` are deliberately absent — an HTTP 400 from an
#: agent server is a coordinator bug and must surface.
ENVIRONMENTAL_NETWORK_STATES = frozenset(
    {"timeout", "dns_error", "offline", "rate_limited"}
)


# ── backoff / park tuning ───────────────────────────────────────────────────

#: First environmental backoff step. Matches ``concurrency.backoff_base`` so
#: the two knobs read the same, but see the ceiling below for why this alone is
#: not enough.
DEFAULT_BACKOFF_BASE_SECS = 60.0

#: Ceiling on the environmental backoff. The issue is explicit: three retries
#: at a 60s base covers a blip, not a provider incident. 20 minutes of
#: degradation is well within normal, so the ceiling belongs in the *tens of
#: minutes*. The probe that gates each attempt costs ~700ms and zero tokens,
#: so a 30-minute wait between probes is nearly free to hold.
DEFAULT_BACKOFF_CEILING_SECS = 1800.0

#: How long to park a usage-limit kill whose reset time we could not parse.
#: The 5-hour window's worst case is 5h, but a blind re-probe after an hour is
#: cheap and self-correcting (the relaunch is gated on a live probe anyway), so
#: this errs toward re-checking too early rather than sleeping through the
#: reset.
DEFAULT_USAGE_LIMIT_PARK_SECS = 3600.0

#: How many CONSECUTIVE environmental deaths in a row (a Claude API 429/5xx,
#: a dropped connection — this module's own `is_environmental` verdict, never
#: a genuine code defect) are tolerated before parking the row instead of
#: clearing it for yet another automatic retry.
#:
#: Shared by every stage that bounds this same kind of retry against this
#: same kind of failure: the WORK stage (`coord.drive._ENVIRONMENTAL_WORK_
#: RETRY_BUDGET`, #2360) and the Test stage (`coord.smoke.ENVIRONMENTAL_
#: SMOKE_RETRY_BUDGET`, `coord.reconcile.propagate_smoke_terminal_failure`,
#: #3315). Living here — the leaf module both `coord.drive` and `coord.smoke`
#: already depend on for `classify_failure` — means the two never drift into
#: independently-tuned knobs that happen to agree today and silently diverge
#: the next time either is retuned (#3315 review): one number, one place,
#: reused rather than duplicated.
ENVIRONMENTAL_RETRY_BUDGET = 5


# ── classification ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FailureClassification:
    """Why a terminal failure happened, in the only two flavours that change
    what the coordinator should do next.

    ``failure_class`` is the load-bearing field (:data:`ENVIRONMENTAL` or
    :data:`WORK`); ``kind`` narrows an environmental hit to the sub-kind that
    decides *how* to wait. ``reason`` is the human-facing text — it always
    names the class in words, so a blocked/paused row read at 3am points at
    the right thing.
    """

    failure_class: str
    kind: str
    reason: str
    signal: str | None = None
    api_status: int | None = None
    reset_at_raw: str | None = None

    @property
    def is_environmental(self) -> bool:
        return self.failure_class == ENVIRONMENTAL

    @property
    def is_work(self) -> bool:
        return self.failure_class == WORK

    @property
    def is_usage_limit(self) -> bool:
        return self.kind == KIND_USAGE_LIMIT

    def to_dict(self) -> dict:
        return {
            "failure_class": self.failure_class,
            "kind": self.kind,
            "reason": self.reason,
            "signal": self.signal,
            "api_status": self.api_status,
            "reset_at_raw": self.reset_at_raw,
        }


def counts_against_work_budget(classification: FailureClassification) -> bool:
    """Should this failure increment the node's *work*-attempt counter?

    The one-liner the sequencer consumes (#1590 part 2, wired outside this
    repo): only a work failure spends the budget that leads to BLOCKED, so a
    node that has never had a work failure can never reach it.
    """
    return classification.is_work


# Wire-format tokens for a Claude API server error. Anchored on the literal
# field/type names the CLI and the SDK emit, so a work-failure summary that
# happens to contain a three-digit number never matches.
#
# Deliberately NOT here: a generic `status: 5xx` / `"status":529` match. A
# worker fixing an HTTP handler legitimately reports "expected status: 200, got
# status: 503" as a *test* failure, and parking that node forever is worse than
# the bug this module fixes. The anthropic error body that would carry a bare
# status also carries `overloaded_error`/`api_error`, which `_API_TOKEN_RE`
# catches, so nothing real is lost.
_API_STATUS_RES = (
    re.compile(r"api_error_status[\"'\s]*[:=]\s*[\"']?(\d{3})", re.IGNORECASE),
    re.compile(r"\bAPI\s+Error:?\s*(\d{3})\b", re.IGNORECASE),
)

_API_TOKEN_RE = re.compile(
    r"\b(overloaded_error|rate_limit_error|api_error|internal_server_error)\b",
    re.IGNORECASE,
)

# Named transport failures. Every one of these is a token an OS, libc, undici
# or httpx emits verbatim — none is a phrase a coordinator would write about a
# failing test suite.
_NETWORK_RE = re.compile(
    r"\b("
    r"ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|EHOSTUNREACH"
    r"|Connection\s+reset\s+by\s+peer"
    r"|Temporary\s+failure\s+in\s+name\s+resolution"
    r"|Name\s+or\s+service\s+not\s+known"
    r"|[Nn]etwork\s+is\s+unreachable"
    r"|Connection\s+error"
    r"|fetch\s+failed"
    r"|socket\s+hang\s+up"
    r")\b"
)

# How much worker-authored terminal prose to consider. An error `result` is a
# one-liner plus a JSON blob; anything past this is a transcript that should
# not have been passed here in the first place.
_RESULT_TEXT_LIMIT = 4000


def _scan_api_status(text: str) -> int | None:
    """First allow-listed API status found in *text*, else ``None``.

    A status that parses but is *not* in :data:`ENVIRONMENTAL_API_STATUSES`
    (e.g. 400, 403) is not a match — it must surface as a work/config failure,
    not retry into eternity.
    """
    for pattern in _API_STATUS_RES:
        for m in pattern.finditer(text):
            try:
                status = int(m.group(1))
            except (TypeError, ValueError):  # pragma: no cover - regex is \d{3}
                continue
            if status in ENVIRONMENTAL_API_STATUSES:
                return status
    return None


def _environmental_api(text: str) -> tuple[int | None, str] | None:
    """``(status, signal)`` if *text* carries an API-server-error signal."""
    status = _scan_api_status(text)
    if status is not None:
        return status, f"api_error_status={status}"
    m = _API_TOKEN_RE.search(text)
    if m:
        return None, m.group(1).lower()
    return None


def _environmental_network(text: str) -> str | None:
    """The matched transport-error token, if *text* carries one."""
    m = _NETWORK_RE.search(text)
    return m.group(1) if m else None


def _work(detail: str | None = None) -> FailureClassification:
    suffix = f": {detail.strip()}" if detail and detail.strip() else (
        ": no environmental signal in the terminal state"
    )
    return FailureClassification(
        failure_class=WORK,
        kind=KIND_WORK,
        reason=f"work failure{suffix}",
        signal=None,
    )


def _usage_limit(reset_at_raw: str | None, signal: str) -> FailureClassification:
    where = f", resets {reset_at_raw}" if reset_at_raw else ""
    return FailureClassification(
        failure_class=ENVIRONMENTAL,
        kind=KIND_USAGE_LIMIT,
        reason=(
            "environmental (usage limit): the account's session budget was "
            f"exhausted mid-run{where} — the provider's budget, not the work"
        ),
        signal=signal,
        reset_at_raw=reset_at_raw,
    )


def _reset_at_from_reason(reason: str | None) -> str | None:
    """Pull the raw reset string back out of a stamped ``failure_reason``."""
    if not is_usage_limit_reason(reason):
        return None
    assert reason is not None  # is_usage_limit_reason implies truthy
    raw = reason[len(USAGE_LIMIT_REASON_PREFIX):].strip()
    return raw or None


def classify_failure(
    *,
    failure_reason: str | None = None,
    terminal_reason: str | None = None,
    usage_limit_reason: str | None = None,
    api_error_status: int | None = None,
    network_error_state: str | None = None,
    is_error: bool | None = None,
    result_text: str | None = None,
) -> FailureClassification:
    """Classify a terminal failure as :data:`ENVIRONMENTAL` or :data:`WORK`.

    Every parameter is an independent *evidence channel*; pass whichever ones
    the call site has. Precedence runs most-specific-first, because the
    remediation differs: a usage limit must wait for a reset time, an API 5xx
    must wait for a liveness probe.

    1. ``usage_limit_reason`` / ``failure_reason`` carrying the
       :data:`coord.worker_events.USAGE_LIMIT_REASON_PREFIX` — exact prefix
       match, no heuristics.
    2. ``api_error_status`` in :data:`ENVIRONMENTAL_API_STATUSES`.
    3. ``network_error_state`` in :data:`ENVIRONMENTAL_NETWORK_STATES` (the
       states :func:`coord.network.classify_error` returns).
    4. An anchored wire-token match in ``failure_reason`` / ``terminal_reason``
       (always scanned — coordinator-authored summaries) or in ``result_text``
       (scanned **only** when ``is_error`` is truthy — see the module
       docstring).

    Anything that matches none of those is :data:`WORK`, including the case
    where no evidence was supplied at all. ``is_error=True`` on its own is not
    an environmental signal — it is exactly as consistent with a failing test
    suite.
    """
    # 1. usage limit — exact, and takes precedence over everything: the reset
    #    time is the only remediation that matters, and re-dispatching onto a
    #    different machine burns the same account-wide budget.
    for candidate in (usage_limit_reason, failure_reason, terminal_reason):
        if is_usage_limit_reason(candidate):
            return _usage_limit(_reset_at_from_reason(candidate), "usage_limit_reason")

    # 2. an explicitly-reported API status.
    if api_error_status is not None:
        try:
            status = int(api_error_status)
        except (TypeError, ValueError):
            status = None
        if status is not None and status in ENVIRONMENTAL_API_STATUSES:
            return FailureClassification(
                failure_class=ENVIRONMENTAL,
                kind=KIND_API_ERROR,
                reason=(
                    f"environmental (Claude API {status}): the provider "
                    "returned a server error — not a defect in the work"
                ),
                signal=f"api_error_status={status}",
                api_status=status,
            )

    # 3. an explicitly-classified transport failure.
    if network_error_state and str(network_error_state).lower() in ENVIRONMENTAL_NETWORK_STATES:
        state = str(network_error_state).lower()
        return FailureClassification(
            failure_class=ENVIRONMENTAL,
            kind=KIND_NETWORK,
            reason=(
                f"environmental (network {state}): the transport failed — "
                "not a defect in the work"
            ),
            signal=f"network_error_state={state}",
        )

    # 4. anchored token scan. Coordinator-authored summaries always; the
    #    worker's own terminal prose only when the result really was an error.
    haystacks: list[str] = [t for t in (failure_reason, terminal_reason) if t]
    if is_error and result_text:
        haystacks.append(result_text[:_RESULT_TEXT_LIMIT])

    for text in haystacks:
        # Usage-limit kill message embedded in prose (the CLI's own wording,
        # not our stamped prefix) — same remediation as branch 1.
        kill = detect_usage_limit_kill(text)
        if kill is not None:
            return _usage_limit(kill.reset_at_raw, "usage limit kill message")

        api = _environmental_api(text)
        if api is not None:
            status, signal = api
            label = f"Claude API {status}" if status is not None else f"Claude API {signal}"
            return FailureClassification(
                failure_class=ENVIRONMENTAL,
                kind=KIND_API_ERROR,
                reason=(
                    f"environmental ({label}): the provider returned a server "
                    "error — not a defect in the work"
                ),
                signal=signal,
                api_status=status,
            )

        token = _environmental_network(text)
        if token is not None:
            return FailureClassification(
                failure_class=ENVIRONMENTAL,
                kind=KIND_NETWORK,
                reason=(
                    f"environmental (network {token}): the transport failed — "
                    "not a defect in the work"
                ),
                signal=token,
            )

    detail = next((t for t in (failure_reason, terminal_reason) if t), None)
    return _work(detail)


def classify_result_event(raw: dict | None) -> FailureClassification:
    """Classify a stream-json terminal ``result`` event's raw payload.

    Reads ``is_error``, ``subtype``, and the prose ``result`` / ``error``
    fields. A ``result`` event with ``is_error`` falsy classifies :data:`WORK`
    — a *successful* run is not this function's problem, and callers should not
    be asking; returning WORK keeps the "default to work" invariant intact.
    """
    if not isinstance(raw, dict):
        return _work("no result event in the transcript")
    is_error = bool(raw.get("is_error"))
    subtype = raw.get("subtype")
    text_parts = [
        raw.get("result"),
        raw.get("error"),
        subtype if isinstance(subtype, str) else None,
    ]
    result_text = "\n".join(p for p in text_parts if isinstance(p, str))
    status = raw.get("api_error_status")
    return classify_failure(
        api_error_status=status if isinstance(status, int) else None,
        is_error=is_error,
        result_text=result_text or None,
        terminal_reason=None,
    )


def classify_log(
    log_path: str | Path,
    *,
    failure_reason: str | None = None,
    tail_bytes: int = 65536,
    provider_name: str | None = None,
    provider: "Provider | None" = None,
) -> FailureClassification:
    """Classify a worker's terminal state from its log.

    Checks the usage-limit kill message (via
    :func:`coord.worker_events.detect_usage_limit_kill_in_log`, which is
    bounded to the transcript's literal last line — this is the literal
    ``claude`` CLI subscription-limit message, so it stays a direct
    ``coord.worker_events`` call regardless of provider; see the #1710
    inventory) and the terminal state of the log, parsed via the
    assignment's resolved :class:`~coord.providers.base.Provider`
    (``provider.parse_log()`` — #1710) rather than assuming every log is
    claude's stream-json shape. A missing/unreadable log with no
    ``failure_reason`` classifies :data:`WORK` — an unreadable log is not
    evidence of an outage.

    *provider_name* (typically ``Assignment.provider_name`` /
    ``IssueState.work_provider``) resolves via
    :func:`coord.providers.get_provider`; ``None`` defaults to
    :class:`~coord.providers.claude.ClaudeProvider`, matching pre-#1710
    behaviour for every existing caller that doesn't pass it. *provider* is
    an escape hatch for tests to pass an already-constructed provider
    directly, bypassing name resolution.

    #1710 NOTE — a documented, narrow behaviour difference from the
    pre-#1710 implementation: that version additionally scanned the raw
    ``result`` event's ``error`` and bare ``subtype`` fields (via
    :func:`classify_result_event`) for an environmental token/phrase when
    ``result`` itself carried none.
    :class:`~coord.providers.base.WorkerSummary` (the ``parse_log()`` seam's
    return shape) only carries ``result_text`` (``raw.get("result")``), not
    ``error``/``subtype`` — so a claude result event whose *only*
    environmental signal lives in ``error`` or a bare ``subtype`` phrase
    (no matching ``api_error_status``, no ``result`` text) would classify
    differently here than before. No test in ``tests/test_failure_class.py``
    exercises that narrow combination, and ``classify_log`` itself has no
    production caller today — but this is exactly the "say so and stop for a
    decision" case #1710 asks for if a fix proves ``WorkerSummary``'s shape
    claude-specific. Flagged rather than silently accepted.
    """
    if is_usage_limit_reason(failure_reason):
        return _usage_limit(_reset_at_from_reason(failure_reason), "usage_limit_reason")

    kill = detect_usage_limit_kill_in_log(log_path)
    if kill is not None:
        return _usage_limit(kill.reset_at_raw, "usage limit kill message")

    if provider is None:
        from coord.providers import get_provider  # noqa: PLC0415
        provider = get_provider(provider_name)
    summary = provider.parse_log(log_path, tail_bytes=tail_bytes)

    classification = classify_failure(
        api_error_status=summary.api_error_status,
        is_error=summary.is_error,
        result_text=summary.result_text,
        terminal_reason=None,
    )
    if classification.is_environmental:
        return classification

    return classify_failure(failure_reason=failure_reason)


# ── resume from the captured reset time (#1590 part 3) ──────────────────────


@dataclass(frozen=True)
class ResumePlan:
    """When an environmentally-parked node may be relaunched.

    ``resume_at`` is always set (timezone-aware): if the reset string could not
    be parsed we fall back to ``failed_at + DEFAULT_USAGE_LIMIT_PARK_SECS``
    rather than leaving the node parked forever — a park with no exit is the
    bug this is fixing, one level down.
    """

    resume_at: datetime
    parsed_from: str | None
    reason: str

    @property
    def from_reset_time(self) -> bool:
        """True when ``resume_at`` came from the worker's own reset string."""
        return self.parsed_from is not None

    def seconds_remaining(self, *, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (self.resume_at - now).total_seconds())

    def due(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.resume_at


_TZ_RE = re.compile(r"\(([A-Za-z][A-Za-z_+\-]*(?:/[A-Za-z_+\-0-9]+)+|UTC|GMT)\)")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\b")
_TIME_12_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.IGNORECASE)
_TIME_24_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _resolve_zone(raw: str) -> tuple[object, str]:
    """``(tzinfo, remainder)`` — pull a ``(America/Chicago)`` suffix off *raw*.

    Falls back to the host's local timezone when absent or unknown: the CLI
    prints the reset in *some* wall-clock, and guessing UTC would silently
    shift the resume by hours.
    """
    local = datetime.now().astimezone().tzinfo or timezone.utc
    m = _TZ_RE.search(raw)
    if not m:
        return local, raw
    name = m.group(1)
    remainder = (raw[: m.start()] + " " + raw[m.end():]).strip()
    if name.upper() in ("UTC", "GMT"):
        return timezone.utc, remainder
    try:
        return ZoneInfo(name), remainder
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return local, remainder


def parse_reset_at(raw: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse a ``reset_at_raw`` string into an absolute, aware ``datetime``.

    Handles the shapes the CLI actually prints — ``"8:30pm (America/Chicago)"``,
    ``"Jul 27, 1:30am (America/Chicago)"``, ``"12pm"``, ``"20:30 (UTC)"``.

    With no date component the reset is taken as the next occurrence of that
    wall-clock time in its own timezone (so a "resets 8:30pm" seen at 9pm
    means tomorrow, not twelve hours ago). With a month/day but no year, a
    date that would land in the past rolls to next year.

    Never raises: an unrecognisable string returns ``None``.
    """
    if not raw or not isinstance(raw, str):
        return None
    now = now or datetime.now(timezone.utc)
    tz, rest = _resolve_zone(raw)
    local_now = now.astimezone(tz)

    hour: int | None = None
    minute = 0
    m12 = _TIME_12_RE.search(rest)
    if m12:
        hour = int(m12.group(1))
        minute = int(m12.group(2) or 0)
        if hour == 12:
            hour = 0
        if m12.group(3).lower() == "p":
            hour += 12
        # Consume the time so the date scan can't re-read "1:30" as "Jan 30".
        rest_wo_time = rest[: m12.start()] + " " + rest[m12.end():]
    else:
        m24 = _TIME_24_RE.search(rest)
        if not m24:
            return None
        hour = int(m24.group(1))
        minute = int(m24.group(2))
        rest_wo_time = rest[: m24.start()] + " " + rest[m24.end():]

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    month: int | None = None
    day: int | None = None
    mdate = _DATE_RE.search(rest_wo_time)
    if mdate:
        month = _MONTHS.get(mdate.group(1).lower())
        if month is not None:
            day = int(mdate.group(2))
            if not (1 <= day <= 31):
                month = None

    try:
        if month is not None and day is not None:
            candidate = local_now.replace(
                month=month, day=day, hour=hour, minute=minute,
                second=0, microsecond=0,
            )
            if candidate < local_now - timedelta(days=1):
                candidate = candidate.replace(year=candidate.year + 1)
        else:
            candidate = local_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= local_now:
                candidate += timedelta(days=1)
    except ValueError:
        # e.g. Feb 30 — a garbled string, not something to guess at.
        return None
    return candidate


def plan_usage_limit_resume(
    *,
    failure_reason: str | None = None,
    reset_at_raw: str | None = None,
    failed_at: datetime | None = None,
    now: datetime | None = None,
    fallback_secs: float = DEFAULT_USAGE_LIMIT_PARK_SECS,
) -> ResumePlan:
    """Turn a usage-limit kill into an absolute resume instant.

    ``reset_at_raw`` wins if given; otherwise it is recovered from a
    ``failure_reason`` stamped by
    :func:`coord.worker_events.format_usage_limit_reason`. When neither parses,
    the node parks for *fallback_secs* from ``failed_at`` (default: now).

    ``failed_at`` is load-bearing for a bare wall-clock reset like
    ``"8:30pm"``: the reset is the next 8:30pm **after the kill**, not after
    *now*. Anchoring on *now* would push an already-elapsed reset a full day
    into the future every time the plan is recomputed, so callers that know
    when the row failed must pass it. Without it we anchor on *now*, which errs
    toward parking too long rather than relaunching into a live limit.
    """
    now = now or datetime.now(timezone.utc)
    raw = reset_at_raw or _reset_at_from_reason(failure_reason)
    parsed = parse_reset_at(raw, now=failed_at or now)
    if parsed is not None:
        return ResumePlan(
            resume_at=parsed,
            parsed_from=raw,
            reason=f"usage limit — resuming at the captured reset time ({raw})",
        )
    base = failed_at or now
    detail = f" (unparseable reset time {raw!r})" if raw else " (no reset time captured)"
    return ResumePlan(
        resume_at=base + timedelta(seconds=fallback_secs),
        parsed_from=None,
        reason=(
            f"usage limit — no usable reset time{detail}; re-checking in "
            f"{int(fallback_secs // 60)}m"
        ),
    )


# ── liveness probe + relaunch gate (#1590 part 4) ───────────────────────────


def environmental_backoff_secs(
    attempt: int,
    *,
    base: float = DEFAULT_BACKOFF_BASE_SECS,
    ceiling: float = DEFAULT_BACKOFF_CEILING_SECS,
) -> float:
    """Exponential backoff for environmental retries, capped at *ceiling*.

    *attempt* is 1-based (the first retry is attempt 1 and waits *base*).
    Unlike ``concurrency.backoff_base``'s three-strikes-at-60s ladder, this is
    designed to be held indefinitely: the cap is in the tens of minutes and
    each wake-up costs one ~700ms, zero-token probe.
    """
    if attempt < 1:
        return 0.0
    # Cap the exponent before the shift so a large attempt count can't build a
    # multi-thousand-bit int on the way to being clamped.
    exponent = min(attempt - 1, 32)
    return min(ceiling, base * float(2 ** exponent))


@dataclass(frozen=True)
class LivenessResult:
    """Outcome of the pre-relaunch liveness probe.

    ``probed`` distinguishes "we asked and the service answered" from "we
    could not ask" (no ``claude`` binary, API-key/Bedrock auth where ``/usage``
    means nothing, unrecognised output). A probe we cannot trust must never
    hold a relaunch, so an unprobeable environment reports
    ``alive=True, probed=False`` — fail-open, exactly like
    :func:`coord.usage_limits.evaluate_usage_gate`.
    """

    alive: bool
    detail: str
    probed: bool = True

    def to_dict(self) -> dict:
        return {"alive": self.alive, "detail": self.detail, "probed": self.probed}


def _looks_like_outage(text: str | None) -> str | None:
    """The environmental signal in a probe's error/raw text, if any."""
    if not text:
        return None
    api = _environmental_api(text)
    if api is not None:
        status, signal = api
        return f"api {status}" if status is not None else signal
    return _environmental_network(text)


def probe_environment_liveness(
    *,
    timeout: float = 15.0,
    probe: Callable[[], object] | None = None,
) -> LivenessResult:
    """Is the Claude service answering right now?

    Reuses the ``claude -p /usage`` probe the sequencer already runs
    (:func:`coord.usage_limits.probe_plan_limits`): ~700ms, ``$0``, zero turns.
    A parseable set of plan bars is proof of life. Anything else is triaged:

    * the failure text carries an outage signal (5xx, overloaded, a named
      transport error, or a hard timeout) → ``alive=False``;
    * the failure is "this environment can't answer that question" (missing
      binary, non-OAuth auth, unrecognised prose) → ``alive=True,
      probed=False``, so the relaunch is not held hostage to a probe that will
      never work here.

    *probe* is an injection seam for tests; it must return a
    :class:`coord.usage_limits.PlanLimits`-shaped object.
    """
    if probe is None:
        from coord.usage_limits import probe_plan_limits  # noqa: PLC0415

        def probe() -> object:
            return probe_plan_limits(timeout=timeout)

    try:
        limits = probe()
    except Exception as exc:  # noqa: BLE001 — a probe must never raise upward
        return LivenessResult(
            alive=True, detail=f"probe raised {type(exc).__name__}: {exc}", probed=False
        )

    if getattr(limits, "ok", False):
        return LivenessResult(alive=True, detail="claude -p /usage answered with plan bars")

    error = getattr(limits, "error", None) or ""
    raw = getattr(limits, "raw", None) or ""
    signal = _looks_like_outage(error) or _looks_like_outage(raw)
    if signal:
        return LivenessResult(alive=False, detail=f"claude -p /usage reports {signal}")
    if "TimeoutExpired" in error:
        return LivenessResult(alive=False, detail=f"claude -p /usage timed out: {error}")
    return LivenessResult(
        alive=True,
        detail=f"probe inconclusive, not holding the relaunch: {error or 'no detail'}",
        probed=False,
    )


@dataclass(frozen=True)
class RelaunchGate:
    """Whether to relaunch now, and if not, how long to wait and why."""

    allow: bool
    wait_secs: float
    reason: str
    classification: FailureClassification | None = None
    liveness: LivenessResult | None = None

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "wait_secs": self.wait_secs,
            "reason": self.reason,
            "classification": (
                self.classification.to_dict() if self.classification else None
            ),
            "liveness": self.liveness.to_dict() if self.liveness else None,
        }


def gate_relaunch(
    classification: FailureClassification,
    *,
    attempt: int = 1,
    failed_at: datetime | None = None,
    now: datetime | None = None,
    probe: Callable[[], object] | None = None,
    backoff_base: float = DEFAULT_BACKOFF_BASE_SECS,
    backoff_ceiling: float = DEFAULT_BACKOFF_CEILING_SECS,
) -> RelaunchGate:
    """Decide whether an environmentally-failed node may be relaunched now.

    * A :data:`WORK` failure is never gated here — it goes down the normal
      bounded-retry / BLOCKED path, and this function says so without spending
      a probe.
    * A usage-limit kill waits for :func:`plan_usage_limit_resume`'s instant
      first, then still has to pass the liveness probe. Pass ``failed_at`` —
      it anchors the bare wall-clock reset string (see that function).
    * An API/network failure waits out
      :func:`environmental_backoff_secs` from ``failed_at``, then has to pass
      the probe.

    Runs at most one probe, and only when the clock already allows a relaunch —
    a node parked until 8:30pm should not be probing every tick.
    """
    now = now or datetime.now(timezone.utc)

    if not classification.is_environmental:
        return RelaunchGate(
            allow=True,
            wait_secs=0.0,
            reason=(
                "work failure — not gated on the environment; "
                f"{classification.reason}"
            ),
            classification=classification,
        )

    if classification.is_usage_limit:
        plan = plan_usage_limit_resume(
            reset_at_raw=classification.reset_at_raw,
            failed_at=failed_at,
            now=now,
        )
        if not plan.due(now=now):
            remaining = plan.seconds_remaining(now=now)
            return RelaunchGate(
                allow=False,
                wait_secs=remaining,
                reason=(
                    f"{classification.reason}; parked for another "
                    f"{int(remaining // 60)}m — {plan.reason}"
                ),
                classification=classification,
            )
    else:
        wait = environmental_backoff_secs(
            attempt, base=backoff_base, ceiling=backoff_ceiling
        )
        elapsed = (now - failed_at).total_seconds() if failed_at else wait
        if elapsed < wait:
            remaining = wait - elapsed
            return RelaunchGate(
                allow=False,
                wait_secs=remaining,
                reason=(
                    f"{classification.reason}; backing off "
                    f"{int(wait)}s from attempt {attempt} "
                    f"({int(remaining)}s remaining)"
                ),
                classification=classification,
            )

    liveness = probe_environment_liveness(probe=probe)
    if liveness.alive:
        return RelaunchGate(
            allow=True,
            wait_secs=0.0,
            reason=(
                f"{classification.reason}; liveness probe passed "
                f"({liveness.detail}) — safe to relaunch"
            ),
            classification=classification,
            liveness=liveness,
        )

    wait = environmental_backoff_secs(
        attempt, base=backoff_base, ceiling=backoff_ceiling
    )
    return RelaunchGate(
        allow=False,
        wait_secs=wait,
        reason=(
            f"{classification.reason}; the service is still down "
            f"({liveness.detail}) — waiting {int(wait)}s before probing again"
        ),
        classification=classification,
        liveness=liveness,
    )
