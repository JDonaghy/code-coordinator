"""GitHub operations via gh CLI."""

from __future__ import annotations

import base64
import json
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from coord import github_throttle
from coord.forge_availability import record_gh_call


# ── Typed gh errors ─────────────────────────────────────────────────────────


class GhError(RuntimeError):
    """Base class for typed ``gh`` CLI errors.

    Subclass of :class:`RuntimeError` so existing ``except RuntimeError``
    call sites keep working without modification; catch a subclass specifically
    to distinguish it from a generic ``gh`` failure.
    """


class GhNotFound(GhError):
    """Raised when a GitHub resource (e.g. a label) does not exist in the repo.

    Callers (e.g. ``serve_app`` HTTP handlers) that need to distinguish
    "the resource doesn't exist" (a 4xx-class client error) from transient
    backend failures (auth, network, rate-limit — which are 5xx) should
    catch this specifically and return an appropriate HTTP status code.
    """


class GhTransientError(GhError):
    """Raised by :func:`get_branch_sha` (opt-in via ``raise_on_transient``)
    when a branch-head lookup failed for a transient infra reason — auth,
    network, or (#2704's incident) a secondary rate limit — as opposed to
    the branch genuinely not existing.

    #2704: ``get_branch_sha``'s unconditional ``except Exception: return
    None`` made a 403 rate limit indistinguishable from a 404 "branch
    deleted" — both callers ever saw was ``None``. The two call for opposite
    handling: a 404 is permanent (stop asking), a rate limit is transient
    and retryable (ask again once it clears). ``raise_on_transient=False``
    (the default) keeps every existing caller's exact fold-both-to-``None``
    contract; a caller that needs to react differently opts in and catches
    this specifically, mirroring how :class:`GhNotFound` lets a caller
    distinguish "gone" from "unreachable" for other resources.
    """


class GhRateLimitError(GhTransientError):
    """Raised by :func:`_gh` when a ``gh`` failure is specifically a GitHub
    rate limit — primary (core quota exhausted) or secondary (abuse-
    detection, #2809's incident) — rather than some other transient infra
    failure (auth, network).

    A :class:`GhTransientError` subclass, so every existing
    ``except GhTransientError`` (and ``except RuntimeError``) call site
    catches this without modification. Callers that need to react to a rate
    limit *specifically* — log the real cause instead of a generic "GitHub
    unreachable, unauthenticated, or rate-limited" string, or honour
    ``retry_after_s`` — catch this subclass and read its attributes instead
    of re-parsing the message text.

    ``status_code``/``request_id``/``retry_after_s`` are best-effort: parsed
    from ``gh``'s stderr text (always available) and, for the ``-i``-
    augmented calls in this module (:func:`get_branch_sha`,
    :func:`get_default_branch_head`), from the real HTTP response headers
    GitHub sent (more precise — the stderr text alone never carries
    ``Retry-After``). Any of the three may be ``None`` when it could not be
    recovered from either source. ``from_cache=True`` marks an instance
    raised WITHOUT a fresh network call, by :func:`_gh` short-circuiting an
    already-known active backoff (see :mod:`coord.github_throttle`) — its
    fields describe the ORIGINAL hit that started that backoff, not a new
    one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after_s: float | None = None,
        secondary: bool = False,
        from_cache: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after_s = retry_after_s
        self.secondary = secondary
        self.from_cache = from_cache


# ── #2977: coord-assign-side signal for a throttle-SKIPPED gh call ─────────
#
# `_gh`'s pre-call guard above raises `GhRateLimitError(from_cache=True)`
# WITHOUT ever making a network call, whenever a shared backoff
# (:mod:`coord.github_throttle`) is already known active — see that raise's
# own comment. Before this, that distinction was lost the moment it crossed
# the `coord assign` subprocess boundary: the caller (`coord/drive.py`)
# just saw a bare non-zero exit and folded it into the same generic "drive
# died" bucket as a real infrastructure failure, so `coord/drive_queue.py`'s
# tick spent one of the entry's two launch attempts on a call that never
# even reached GitHub (the 2026-08-30 `coord-portal#161` incident this
# closes — see the issue for the full trace).
#
# `EX_TEMPFAIL` is the conventional (sysexits.h) code for "temporary
# failure, retry later" — used by :mod:`coord.commands.dispatch`'s `assign`
# command to give this ONE failure shape a distinct exit status. That is
# necessary but not sufficient for `coord/drive_queue.py`'s pure tick logic
# to act on: the exit code alone carries no expiry, and by the time a
# `drive_exited` audit row is read back the file that raised the original
# `GhRateLimitError` is long gone, so there is nothing left to re-consult.
# `format_throttle_skip_reason` instead embeds everything the tick needs —
# the marker, and an absolute `until=<epoch>` wall-clock timestamp — directly
# in the human-readable reason text that already survives the subprocess
# boundary unmodified (`coord.drive.Driver._spawn` captures the child's
# stdout+stderr verbatim into `_last_run_output`, which flows straight into
# the `drive_exited` audit summary `coord/commands/drive_queue.py`'s
# `_fetch_exit_reasons` reads back as `own_reason` — no protocol change
# needed on that path at all). `is_throttle_skip_reason`/
# `parse_throttle_skip_until` are `coord/drive_queue.py`'s read side: pure
# text parsing, no I/O, so `_reconcile_running`'s park (and its wall-clock
# resume, no live re-check needed) stay exactly as pure as every other
# branch in that module.
THROTTLE_SKIP_MARKER = "[gh-throttle-skipped #2977]"

#: The conventional sysexits.h "temporary failure, please try again" code —
#: distinct from every other exit status `coord assign` uses (see
#: `coord.drive.EXIT_DISPATCH_REFUSED`/`EXIT_DEAD_END` for the other two
#: exit codes a downstream reader must be able to tell apart from "died").
EX_TEMPFAIL = 75

_THROTTLE_SKIP_UNTIL_RE = re.compile(r"until=([0-9]+(?:\.[0-9]+)?)")


def format_throttle_skip_reason(exc: GhRateLimitError, *, now: float | None = None) -> str:
    """Canonical, parseable text for a ``coord assign`` refusal caused by a
    throttle-SKIPPED ``gh`` call (``exc.from_cache`` — see
    :class:`GhRateLimitError`'s docstring).  Wraps ``str(exc)`` (already
    carries the reason/status/request-id `_gh` recovered) with
    :data:`THROTTLE_SKIP_MARKER` and an absolute ``until=`` epoch timestamp
    that :func:`parse_throttle_skip_until` recovers later, with no clock of
    its own and no access to :mod:`coord.github_throttle` needed.

    ``exc.retry_after_s`` is "seconds remaining AT THE MOMENT ``_gh`` raised"
    — a value that goes stale the instant it's read back, which is exactly
    why an absolute timestamp is embedded here (computed once, at the one
    point in this whole path that has both a fresh clock and the exception)
    rather than a duration re-derived downstream. Falls back to
    :data:`coord.github_throttle.DEFAULT_BACKOFF_S` when ``retry_after_s``
    is ``None`` (the pre-call guard's own fallback wording already reads
    "GitHub's guidance" in that case, so this mirrors it rather than
    inventing a shorter window with no evidence behind it).
    """
    now = now if now is not None else time.time()
    retry_after = (
        exc.retry_after_s if exc.retry_after_s is not None else github_throttle.DEFAULT_BACKOFF_S
    )
    until = now + max(0.0, retry_after)
    return (
        f"{THROTTLE_SKIP_MARKER} {exc} — no gh call was made for this launch "
        f"attempt, so it should not spend one (#2977); until={until:.3f}"
    )


def is_throttle_skip_reason(text: str | None) -> bool:
    """``True`` when *text* is (or contains) a :func:`format_throttle_skip_reason`
    result — the same marker-based convention as
    ``coord.models.is_policy_refusal_reason``/``coord.gate_a.
    is_gate_a_refusal_reason``, so ``coord/drive_queue.py`` can recognise this
    park from plain reason text with no I/O.
    """
    return bool(text) and THROTTLE_SKIP_MARKER in text


def parse_throttle_skip_until(text: str | None) -> float | None:
    """The absolute epoch ``until=`` timestamp embedded by
    :func:`format_throttle_skip_reason`, or ``None`` when *text* doesn't
    carry the marker at all, or carries it without a parseable timestamp
    (defensive — should not happen for text this module itself wrote, but a
    hand-edited row or a future format change must degrade to "unknown",
    never a wrong resume time).
    """
    if not is_throttle_skip_reason(text):
        return None
    m = _THROTTLE_SKIP_UNTIL_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


class GhTooOldForJsonChecks(GhError):
    """Raised when the installed ``gh`` doesn't support ``gh pr checks --json``
    *at all* (#1564 Addendum 2), as opposed to supporting ``--json`` but
    rejecting one of the requested field names (a plain :class:`RuntimeError`
    from :func:`get_pr_checks`, e.g. the original ``conclusion`` bug).

    The fleet that surfaced this issue runs the merge gate across hosts with
    wildly different ``gh`` versions — and because ``coord merge`` re-invokes
    itself on the daemon host (``COORD_MERGE_ON_DAEMON``, see ``serve_app.py``),
    it is the *daemon's* ``gh`` that decides every production merge, not the
    thin client's. A daemon stuck on a too-old ``gh`` must fail with a message
    that names the problem and the fix, not the same undiagnosable
    "could not read CI status" text used for auth/network flakes — see
    :func:`coord.ci_github.GitHubCi._fetch`, which catches this subclass
    ahead of the generic ``RuntimeError`` branch for exactly that reason.
    """


# Keywords indicating a transient/infra failure that should never trigger
# label auto-creation.  Checked against the full lowercase error string.
_TRANSIENT_ERROR_KEYWORDS: tuple[str, ...] = (
    "http 401",
    "http 403",
    "http 429",
    "authentication required",
    "bad credentials",
    "credentials not found",
    "gh auth login",
    "api rate limit",
    "rate limit exceeded",
    "secondary rate limit",
    "timed out",
    "connection refused",
    "connection reset",
    "connection error",
    "could not resolve host",
    "no such host",
    "dial tcp",
)


def _is_transient_error(exc: Exception) -> bool:
    """True when the ``gh`` error suggests an auth, rate-limit, or network failure.

    These failures should never trigger label auto-creation or other
    retry-with-side-effects logic — the root cause is infra, not a missing
    resource.
    """
    msg = str(exc).lower()
    return any(kw in msg for kw in _TRANSIENT_ERROR_KEYWORDS)


def _is_label_not_found(exc: Exception) -> bool:
    """True when ``gh`` reported that a label does not exist in the repository.

    Returns ``False`` for transient errors (:func:`_is_transient_error`) so
    callers never attempt auto-creation on auth/network/rate-limit failures.
    """
    if _is_transient_error(exc):
        return False
    msg = str(exc).lower()
    # "could not resolve to a label" — the GraphQL error gh surfaces
    # "label 'foo' not found" — the REST error in some gh versions
    return "could not resolve to a label" in msg or (
        "label" in msg and "not found" in msg
    )


def _classify_gh_exit(stderr: str) -> str:
    """Classify a non-zero ``gh`` exit's stderr for forge-availability
    recording: ``"transient"`` (auth/network/rate-limit -- an availability
    signal, per :func:`_is_transient_error`) or ``"app_error"`` (``gh`` ran
    fine and reported an ordinary application-level failure, e.g. "label not
    found" -- business as usual, not a forge problem).

    Factored out of :func:`_gh` so the handful of call sites in this module
    that shell out to ``gh`` directly instead of through :func:`_gh` (#1896
    review: each documents why it bypasses ``_gh`` -- idempotent-on-a-
    specific-stderr-message semantics ``_gh``'s raise-only contract can't
    express) can record the same classification :func:`_gh` does, rather
    than silently producing no forge-availability observation at all.
    """
    return "transient" if _is_transient_error(RuntimeError(stderr)) else "app_error"


# ── #2809: rate-limit detection + structured detail extraction ─────────────
#
# GitHub's secondary (abuse-detection) limiter doesn't show up in `gh api
# rate_limit` at all -- the ONLY signal it ever gives is a 403 on the actual
# call. Before this, every such 403 was folded into a generic "transient"
# bucket indistinguishable from an auth failure or a dead network, and its
# `Retry-After`/request-ID were discarded outright (issue evidence:
# `coord/github_ops.py:1697`, `coord/merge_queue.py:3489-3498`). The two
# helpers below recover as much of that detail as each call site can offer:
# `_extract_rate_limit_detail` always works (parses `gh`'s stderr text, which
# every call gets); `_parse_gh_include` additionally recovers the exact
# `Retry-After` header when the caller passed `-i` to `gh api` (real HTTP
# headers on stdout -- see `get_branch_sha`/`get_default_branch_head`,
# verified empirically: `-i` puts headers+body on stdout even on a non-2xx
# response, `gh`'s own short diagnostic still goes to stderr separately).

_HTTP_STATUS_IN_MESSAGE_RE = re.compile(r"\(HTTP (\d{3})\)")
_REQUEST_ID_RE = re.compile(r"request ID\s+([A-Za-z0-9:_-]+)", re.IGNORECASE)
_STATUS_LINE_RE = re.compile(r"^HTTP/\S+\s+(\d{3})")


class GhResponseMeta(NamedTuple):
    """Structured detail recovered from one `gh api -i` response."""

    status: int | None
    request_id: str | None
    retry_after_s: float | None


def _classify_rate_limit(stderr: str) -> tuple[bool, bool]:
    """``(is_rate_limit, is_secondary)`` for one `gh` stderr string.

    Deliberately narrower than :func:`_is_transient_error` — a bare "HTTP
    403" is also how a plain permissions failure (token lacks a scope) looks,
    and that must NOT engage the shared backoff in :mod:`coord.github_throttle`
    (it would pause every other `gh` call on the host for a problem no wait
    will ever fix). Only an explicit "rate limit" mention in the message
    counts.
    """
    low = stderr.lower()
    if "secondary rate limit" in low:
        return True, True
    if "rate limit" in low:
        return True, False
    return False, False


# #2858 proposal 4: a plain "rate limit" 403 with no "secondary" wording gets
# classified `primary_rate_limit` by `_classify_rate_limit` above purely from
# `gh`'s stderr text — but GitHub's abuse-detection (secondary) limiter fires
# on request RATE/concurrency, not the primary quota, and its own error text
# does not reliably say "secondary" (see :mod:`coord.github_throttle`'s
# docstring: "does not show up in `gh api rate_limit` at all"). The
# 2026-08-27 incident this closes: every hit was recorded `primary_rate_
# limit` while `gh api rate_limit` read `core 4986/5000` the whole time —
# sending an operator to check a quota that was never the problem.
_PRIMARY_HEALTHY_REMAINING_FLOOR = 50
_RATE_LIMIT_CHECK_TIMEOUT_S = 5.0


def _primary_quota_healthy() -> bool | None:
    """Best-effort ``True``/``False``/``None`` (unknown) reading of whether
    the primary REST quota is currently healthy.

    Shells out directly rather than through :func:`_gh` — deliberately: this
    runs FROM INSIDE `_gh`'s own rate-limit failure handling, so routing it
    back through `_gh` would recurse into the same seam that just failed
    (and would itself be subject to the very backoff this call exists to
    disambiguate). `gh api rate_limit` is GitHub's own documented exemption
    from counting against the quota it reports, so one extra call here does
    not make the situation this is diagnosing any worse.

    Returns ``None`` — "don't know, don't reclassify" — on ANY failure
    (missing binary, timeout, non-zero exit, unparseable JSON): silently
    mislabeling a hit as ``secondary_rate_limit`` on a guess would be worse
    than leaving the pre-#2858 ``primary_rate_limit`` default alone, since
    only a *positive* health reading is stronger evidence than "gh's own
    stderr didn't say 'secondary'".
    """
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit"],
            capture_output=True, text=True, timeout=_RATE_LIMIT_CHECK_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    core = ((payload or {}).get("resources") or {}).get("core") or {}
    remaining = core.get("remaining")
    if not isinstance(remaining, (int, float)):
        return None
    return remaining >= _PRIMARY_HEALTHY_REMAINING_FLOOR


def _extract_rate_limit_detail(stderr: str) -> GhResponseMeta:
    """Best-effort status/request-id extraction from `gh`'s plain stderr
    text (no `-i`, no headers) -- covers every `_gh` call site, not just the
    two that pass `-i`. `retry_after_s` is always ``None`` here: GitHub does
    not put it in the message body, only in the (unavailable-without-``-i``)
    `Retry-After` header.
    """
    status = None
    m = _HTTP_STATUS_IN_MESSAGE_RE.search(stderr)
    if m:
        status = int(m.group(1))
    request_id = None
    m = _REQUEST_ID_RE.search(stderr)
    if m:
        request_id = m.group(1)
    return GhResponseMeta(status=status, request_id=request_id, retry_after_s=None)


def _parse_gh_include(raw: str) -> tuple[GhResponseMeta, str]:
    """Split `gh api -i ...` output into ``(meta, body)``.

    ``-i`` prefixes the response body with the HTTP status line and response
    headers (a blank line separates the two, same as a raw HTTP response
    rendered as text) -- this is how the real ``Retry-After`` and
    ``X-GitHub-Request-Id`` headers get recovered at all, since neither
    appears in the JSON error body or in `gh`'s own stderr diagnostic.

    Falls back to ``(empty meta, raw)`` when *raw* doesn't look like an
    ``-i`` response (no header block found) — this is what makes it safe to
    call unconditionally on both the ``-i``-augmented success path (strip
    the headers `get_branch_sha` doesn't want) and on test fixtures that
    still hand back a bare JSON string with no headers at all.
    """
    normalized = raw.replace("\r\n", "\n")
    if "\n\n" not in normalized:
        return GhResponseMeta(None, None, None), raw
    head, _, body = normalized.partition("\n\n")
    lines = head.splitlines()
    if not lines or not lines[0].startswith("HTTP/"):
        return GhResponseMeta(None, None, None), raw
    status = None
    m = _STATUS_LINE_RE.match(lines[0])
    if m:
        status = int(m.group(1))
    request_id = None
    retry_after: float | None = None
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "x-github-request-id":
            request_id = value
        elif key == "retry-after":
            try:
                retry_after = float(value)
            except ValueError:
                pass
    return GhResponseMeta(status, request_id, retry_after), body


def _resolve_caller(explicit: str) -> str:
    """#2988: the caller tag threaded into :func:`coord.forge_availability.
    record_gh_call`, so GitHub call volume is attributable to a code path
    instead of just an endpoint class (``argv[0]``).

    An *explicit* tag always wins -- that's the ~85 call sites in this
    module that name themselves (``caller="github_ops.get_issue"``, etc.):
    a hand-picked tag survives refactors, is greppable, and costs nothing at
    runtime, which a stack walk on every single ``gh`` call would not.

    When a call site hasn't been given one (``explicit == ""`` — every
    caller *outside* this module that still calls :func:`_gh`/
    :func:`_gh_json`/:func:`_gh_input_json` directly, e.g. ``coord.claim``,
    ``coord.smoke``, ``coord.refine_chat``), this falls back to the
    immediate calling module's dotted name via a stack walk. Coarser than a
    hand-picked tag (can't distinguish two call sites in the same module),
    but every row still carries a real, non-empty, greppable value instead
    of the pre-#2988 argv[0]-only key -- that's the acceptance bar, not
    maximal precision. ``sys._getframe(2)``: frame 0 is this function,
    frame 1 is the ``_gh``/``_gh_json``/``_gh_input_json`` call that invoked
    it, frame 2 is whoever called *that* -- correct for all three funnels
    because each resolves its own ``caller`` in its own body, before doing
    any further calling (see e.g. ``_gh_json`` resolving before it calls
    ``_gh``, so ``_gh``'s own resolution is always a no-op in that path).
    """
    if explicit:
        return explicit
    try:
        frame = sys._getframe(2)
        name = frame.f_globals.get("__name__", "")
        return name or "unknown"
    except Exception:  # noqa: BLE001 -- best-effort; never break a gh call over this
        return "unknown"


def _gh(*args: str, caller: str = "", force_through_backoff: bool = False) -> str:
    """Run ``gh`` with *args* and return its stdout, or raise :class:`GhError`.

    *caller* (#2988): a short tag identifying the code path making this
    call (e.g. ``"github_ops.get_issue"``), threaded straight into
    :func:`coord.forge_availability.record_gh_call` so GitHub call volume is
    attributable. Resolved via :func:`_resolve_caller` when not given
    explicitly -- see that function's docstring.

    #1483: this is the single seam every ``_gh``-backed helper in this module
    funnels through, so it is also the single place that must absorb the
    ways ``gh`` can fail to even run — not just a non-zero exit.
    ``subprocess.run`` raises ``FileNotFoundError`` when the ``gh`` binary
    isn't on PATH (the elitebook incident that prompted #1483: a worker's
    systemd PATH didn't include the linuxbrew-installed ``gh``) and
    ``subprocess.TimeoutExpired`` when it hangs past the 30s budget; both are
    caught here and re-raised as :class:`GhError` (a ``RuntimeError``
    subclass) so every existing ``except RuntimeError`` call site — and any
    future one — gets the same fail-safe behavior the pre-#1483 direct
    ``shutil.which("gh")`` + ``subprocess.run(..., check=False)`` call sites
    had, for free, without each caller having to remember to guard for it.

    #2809: also the single seam that consults and feeds
    :mod:`coord.github_throttle`'s shared backoff. A rate-limit failure
    below records the hit there so every OTHER `gh` caller on this host
    learns about it on their very next call; a call that starts while an
    earlier hit's backoff is still active either rides it out (a short,
    jittered sleep) or — deep inside a longer window — skips the network
    call entirely and raises immediately, reusing the ORIGINAL hit's detail.
    That skip is the actual damping: fewer requests reach a limiter that
    only decays when request volume drops.

    #2858: ``force_through_backoff=True`` is the starvation-floor escape
    hatch for a caller on a SLOW, fixed cadence (``coord.serve_app.
    _sync_issues_tick``'s 300s tick) that a shared latch re-armed by faster
    pollers can otherwise starve indefinitely — every sample this caller
    takes lands mid-window, so it never once falls through to the short
    sleep-then-call path below, and it is exactly this class of caller a
    2026-08-27 incident showed CAN keep failing for 39+ minutes straight
    even though GitHub's real limiter had already cleared (a direct `gh`
    call succeeded in under a second the whole time). Setting it skips ONLY
    the pre-emptive "still deep inside the window, don't even try" branch
    immediately below — the short jittered pre-call sleep just after it
    still runs unchanged, and a genuinely-still-active real limit still
    raises (and still re-records) normally once the actual network call is
    made. This never removes damping for the callers the latch exists to
    protect (this flag is not theirs to set); it only stops a rare,
    low-frequency caller from being permanently outbid by them.
    """
    caller = _resolve_caller(caller)
    backoff_sleep_s, active_backoff = github_throttle.consult()
    if active_backoff is not None:
        remaining = active_backoff.until - time.time()
        if remaining > github_throttle.MAX_PRECALL_SLEEP_S and not force_through_backoff:
            # Still well inside a known backoff window -- don't add another
            # request to a limiter that only recovers when the rate drops.
            # This is not a fresh observation, so it is not re-recorded.
            record_gh_call(
                args, outcome="transient", duration_s=0.0, caller=caller,
                detail=(
                    f"skipped: coordinated backoff active ({active_backoff.reason}, "
                    f"{remaining:.0f}s remaining)"
                ),
            )
            raise GhRateLimitError(
                f"gh {' '.join(args)} skipped: GitHub {active_backoff.reason} "
                f"backoff active for {remaining:.0f}s more "
                f"(status={active_backoff.status}, request_id={active_backoff.request_id})",
                status_code=active_backoff.status,
                request_id=active_backoff.request_id,
                retry_after_s=remaining,
                secondary=active_backoff.reason == "secondary_rate_limit",
                from_cache=True,
            )
        if backoff_sleep_s > 0:
            time.sleep(backoff_sleep_s)
    # #1896 Phase 0: time + classify every `gh` invocation through this one
    # seam so `coord diagnose --forge-availability` has real data on how
    # often the forge is actually unreachable, not just anecdote from one
    # bad day. Recording is best-effort (coord.forge_availability.
    # record_gh_call never raises) and adds no network call of its own —
    # only a local timer + a local DB write.
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        record_gh_call(args, outcome="unreachable", duration_s=time.monotonic() - _t0,
                        detail="gh not found", caller=caller)
        raise GhError(f"gh {' '.join(args)} failed: gh not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        record_gh_call(args, outcome="unreachable", duration_s=time.monotonic() - _t0,
                        detail="timed out", caller=caller)
        raise GhError(f"gh {' '.join(args)} failed: timed out: {exc}") from exc
    except OSError as exc:
        record_gh_call(args, outcome="unreachable", duration_s=time.monotonic() - _t0,
                        detail=str(exc), caller=caller)
        raise GhError(f"gh {' '.join(args)} failed: {exc}") from exc
    duration = time.monotonic() - _t0
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # #1896: distinguish a transient forge/auth/network failure (an
        # availability signal) from an ordinary application-level error like
        # "label not found" (not one) — see `_classify_gh_exit`.
        record_gh_call(args, outcome=_classify_gh_exit(stderr), duration_s=duration,
                        detail=stderr, caller=caller)
        is_rate_limit, is_secondary = _classify_rate_limit(stderr)
        if is_rate_limit:
            if not is_secondary:
                # #2858: `gh`'s own text didn't say "secondary" -- confirm
                # against the live primary quota before believing it rather
                # than assuming the text is always right. `None` (couldn't
                # tell) leaves `is_secondary` exactly as `_classify_rate_
                # limit` set it -- see `_primary_quota_healthy`'s docstring.
                if _primary_quota_healthy():
                    is_secondary = True
            text_meta = _extract_rate_limit_detail(stderr)
            # `-i` callers (get_branch_sha/get_default_branch_head) get
            # headers on stdout even on a non-2xx response -- prefer that
            # over the text-only extraction above whenever it parsed one.
            header_meta = (
                _parse_gh_include(result.stdout)[0]
                if result.stdout.startswith("HTTP/") else None
            )
            status = (header_meta.status if header_meta else None) or text_meta.status
            request_id = (
                (header_meta.request_id if header_meta else None) or text_meta.request_id
            )
            retry_after = header_meta.retry_after_s if header_meta else None
            github_throttle.record(
                reason="secondary_rate_limit" if is_secondary else "primary_rate_limit",
                status=status, request_id=request_id, retry_after_s=retry_after,
            )
            raise GhRateLimitError(
                f"gh {' '.join(args)} failed: {stderr}",
                status_code=status, request_id=request_id, retry_after_s=retry_after,
                secondary=is_secondary,
            )
        raise RuntimeError(f"gh {' '.join(args)} failed: {stderr}")
    record_gh_call(args, outcome="ok", duration_s=duration, caller=caller)
    return result.stdout.strip()


def _json_loads_or(raw: str | None, default: Any = None) -> Any:
    """Decode *raw* as JSON, or return *default* on empty/malformed input.

    #1353: ``_gh`` above treats a ``gh`` invocation that exits 0 with empty
    stdout as a success — indistinguishable, from ``_gh``'s point of view,
    from a real empty payload. Roughly half of this module's call sites used
    to hand ``_gh``'s output straight to a bare ``json.loads``, so that empty
    string decoded to exactly ``json.JSONDecodeError: Expecting value: line 1
    column 1 (char 0)`` — a one-line, unattributable crash that took down an
    entire ``coord merge`` drain in the incident that prompted this (see
    issue #1353). This is the one guarded decode every such call site now
    routes through, so "gh returned nothing useful" degrades to a documented
    *default* value at every site uniformly, rather than at only the two
    sites (``get_pr_commit_messages``, ``pr_is_merged``) that happened to
    hand-roll their own guard beforehand.

    Does **not** change ``_gh``'s own failure contract: a non-zero ``gh``
    exit still raises ``RuntimeError``/``GhError`` same as always — this only
    covers the exit-0-but-stdout-is-garbage case that used to reach a bare
    ``json.loads``.
    """
    if raw is None or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _gh_json(
    *args: str, default: Any = None, caller: str = "", force_through_backoff: bool = False,
) -> Any:
    """Run ``gh`` with *args* and JSON-decode its stdout, failing open.

    Composes :func:`_gh` (still raises on a non-zero ``gh`` exit / missing
    binary / timeout) with :func:`_json_loads_or` (fails open to *default* on
    empty/malformed stdout from an otherwise-successful invocation). See
    :func:`_json_loads_or` for why this exists.

    *caller* (#2988): resolved via :func:`_resolve_caller` — same as
    :func:`_gh` — *before* calling :func:`_gh`, so the module name inferred
    (when no explicit tag was given) names whoever called ``_gh_json``, not
    ``_gh_json`` itself. Always passed to :func:`_gh` explicitly from here on
    (never omitted), unlike *force_through_backoff* below — seeing this
    module's own ~85 call sites hand-tag themselves already changed most
    ``_gh_json``-mocking tests' expected call args regardless, so there was
    no longer a "keep every OTHER call site's exact signature" case to
    preserve for this parameter the way there was pre-#2988.

    *force_through_backoff* passes straight through to :func:`_gh` — see its
    docstring (#2858). Deliberately omitted from the call entirely (rather
    than forwarded as an explicit ``False``) when unset: a bare
    ``_gh(*args, caller=caller)`` keeps every OTHER ``_gh_json``-based call
    site's call args free of a stray ``force_through_backoff=False``, which
    matters because several tests mock ``_gh`` directly and assert on its
    exact call args — forwarding an always-present keyword would have
    changed every one of those, not just the callers that actually use this.
    """
    caller = _resolve_caller(caller)
    if force_through_backoff:
        raw = _gh(*args, caller=caller, force_through_backoff=True)
    else:
        raw = _gh(*args, caller=caller)
    return _json_loads_or(raw, default)


def get_open_issues(repo: str, *, force_through_backoff: bool = False) -> list[dict]:
    """*force_through_backoff* (#2858): set by ``coord.serve_app.
    _sync_issues_tick`` once ``coord.issues_sync_status.is_starved(repo)``
    says this repo has gone too long without a successful sync — see
    :func:`coord.github_ops._gh`'s docstring for what it actually changes.
    """
    # #658: raised from 100 → 500 so repos with many open issues don't silently
    # skip old issue numbers during coord sync.  GitHub paginates the REST list
    # endpoint at 100 items internally, so this costs ~5 API calls for a large
    # repo — acceptable for a background sync.
    return _gh_json(
        "issue", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,labels,milestone,body,assignees",
        "--limit", "500",
        default=[],
        force_through_backoff=force_through_backoff, caller="github_ops.get_open_issues")


def get_closed_epics(repo: str, *, label: str = "epic") -> list[dict]:
    """Return closed issues in *repo* carrying *label* (default ``"epic"``).

    Used by ``coord plans`` (#974) so a milestone's tracking epic is still
    found once it has been closed while the milestone itself stays open
    (e.g. all work-order nodes finished and someone tidied up the epic
    before remembering to close the milestone) — see
    :func:`coord.plans.find_tracking_issue`. A small, label-filtered,
    closed-only lookup rather than a full ``--state all`` issue fetch, since
    only closed *epics* are of interest here.
    """
    return _gh_json(
        "issue", "list", "--repo", repo, "--state", "closed", "--label", label,
        "--json", "number,title,labels,milestone,body,assignees",
        "--limit", "500",
        default=[], caller="github_ops.get_closed_epics")


def get_issue(repo: str, issue_number: int) -> dict:
    """Fetch a single issue by number.

    Returns ``{number, title, body, state, milestone, labels, ...}``.
    ``milestone`` is ``None`` when the issue has none, else ``{"number":
    ..., "title": ...}`` — used by ``coord milestone order`` (#768) to
    resolve a tracking issue's milestone and validate node membership
    without a second call. ``labels`` is a list of ``{"name": ..., ...}``
    dicts — #1138's ``enforce_oracle_readiness`` reads issue labels (e.g.
    ``oracle:exempt``) off this same call, so it must be requested here
    too, not just on the list endpoints (``get_open_issues``,
    ``get_closed_epics``).
    """
    return _gh_json(
        "issue", "view", str(issue_number), "--repo", repo,
        "--json", "number,title,body,state,milestone,labels",
        default={}, caller="github_ops.get_issue")


# ── Sub-issues (#1195) ───────────────────────────────────────────────────────
#
# The REST sub-issues API is live on GitHub today but used nowhere in this
# repo before #1195 — every epic->child relation so far is the `## Work
# order` / `## Sub-issues` markdown checklist `coord.milestone_order` parses.
# These wrap the raw endpoints; `coord.parentage_github.GitHubParentage` is
# the adapter that turns them into the backend-agnostic `coord.parentage`
# seam shape (`Child`/`ParentRef`).
#
# Gotcha verified while filing #1195: the write endpoints (POST/DELETE) take
# the child's internal database `id`, NOT its issue `number` — resolve via
# `get_issue`'s `--jq .id` before writing (see `_resolve_issue_id`).


def get_sub_issues(repo: str, issue_number: int) -> list[dict]:
    """The live sub-issues of *issue_number* (``GET .../sub_issues``).

    Returns ``[]`` for an issue with no sub-issues (confirmed live: this is
    the API's normal response, not a 404/410 — see #1195's filing notes).
    Each item is a full issue object; callers only need ``number``/``state``.
    """
    return _gh_json("api", f"repos/{repo}/issues/{issue_number}/sub_issues", default=[],
        caller="github_ops.get_sub_issues")


def get_issue_parent(repo: str, issue_number: int) -> dict | None:
    """The parent of *issue_number*, or ``None`` when it has none.

    Reads the ``parent`` field GitHub already includes on ``GET
    /issues/{n}`` (confirmed live while filing #1195 — no preview header
    needed). ``None`` covers both "field absent" and the documented
    ``parent: null`` shape.
    """
    raw = _gh("api", f"repos/{repo}/issues/{issue_number}", "--jq", ".parent",
        caller="github_ops.get_issue_parent")
    stripped = raw.strip()
    if not stripped or stripped == "null":
        return None
    return _json_loads_or(stripped, default=None)


def _resolve_issue_id(repo: str, issue_number: int) -> int:
    """Issue `number` -> internal database `id` (#1195's write-path gotcha:
    the sub-issues POST/DELETE endpoints want the latter, not the former)."""
    raw = _gh("api", f"repos/{repo}/issues/{issue_number}", "--jq", ".id",
        caller="github_ops._resolve_issue_id")
    return int(raw.strip())


def add_sub_issue(repo: str, parent_number: int, child_number: int) -> None:
    """Make *child_number* a sub-issue of *parent_number* (``POST
    .../sub_issues``). Resolves *child_number* to its database id first —
    the endpoint wants ``sub_issue_id`` (a database id), not the issue
    number, and 422s if the body doesn't shape up."""
    child_id = _resolve_issue_id(repo, child_number)
    _gh(
        "api", f"repos/{repo}/issues/{parent_number}/sub_issues",
        "-X", "POST",
        "-F", f"sub_issue_id={child_id}", caller="github_ops.add_sub_issue")


def remove_sub_issue(repo: str, parent_number: int, child_number: int) -> None:
    """Detach *child_number* from *parent_number* (``DELETE
    .../sub_issue`` — singular, unlike the GET/POST plural; a real GitHub
    API asymmetry, not a typo here)."""
    child_id = _resolve_issue_id(repo, child_number)
    _gh(
        "api", f"repos/{repo}/issues/{parent_number}/sub_issue",
        "-X", "DELETE",
        "-F", f"sub_issue_id={child_id}", caller="github_ops.remove_sub_issue")


def edit_issue(
    repo: str,
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
) -> None:
    """Edit an issue's title and/or body. The GitHub backend of the
    issue-tracker seam (`state.edit_issue_content`) — GitLab / bare-DB adapters
    slot in alongside this later. The body is piped via stdin (`--body-file -`)
    to avoid arg-length and shell-quoting issues on long markdown bodies.

    Shells out directly rather than through :func:`_gh` because of the
    ``input=`` (stdin) parameter :func:`_gh` doesn't support — but still
    records the same #1896 forge-availability observation :func:`_gh` would
    have, so this call site isn't a silent gap in that measurement.
    """
    if title is None and body is None:
        return
    args = ["issue", "edit", str(issue_number), "--repo", repo]
    if title is not None:
        args += ["--title", title]
    if body is not None:
        args += ["--body-file", "-"]
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *args],
            input=body if body is not None else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_gh_call(tuple(args), outcome="unreachable",
                        duration_s=time.monotonic() - _t0, detail=str(exc),
                        caller="github_ops.edit_issue")
        raise
    duration = time.monotonic() - _t0
    if result.returncode != 0:
        stderr = result.stderr.strip()
        record_gh_call(tuple(args), outcome=_classify_gh_exit(stderr),
                        duration_s=duration, detail=stderr, caller="github_ops.edit_issue")
        raise RuntimeError(
            f"gh issue edit #{issue_number} failed: {stderr}"
        )
    record_gh_call(tuple(args), outcome="ok", duration_s=duration, caller="github_ops.edit_issue")


def create_milestone(
    repo: str,
    title: str,
    *,
    description: str | None = None,
    due_on: str | None = None,
) -> dict:
    """Create a GitHub milestone via ``gh api POST .../milestones`` (#645 seam).

    The GitHub backend of the milestone-tracker seam
    (``coord.state.write_milestone``) — GitLab / bare-DB adapters slot in
    alongside this later, same as ``edit_issue``. ``due_on`` is an ISO 8601
    timestamp (e.g. ``"2026-08-01T00:00:00Z"``) per the GitHub API; this
    layer does not validate the format, it just forwards it. Returns the
    created milestone's JSON (``number``, ``title``, ``description``,
    ``due_on``, ``html_url``, ...).
    """
    args = ["api", f"repos/{repo}/milestones", "-f", f"title={title}"]
    if description is not None:
        args += ["-f", f"description={description}"]
    if due_on is not None:
        args += ["-f", f"due_on={due_on}"]
    return _json_loads_or(_gh(*args, caller="github_ops.create_milestone"), default={})


def edit_milestone(
    repo: str,
    number: int,
    *,
    title: str | None = None,
    description: str | None = None,
    due_on: str | None = None,
) -> dict:
    """Edit a GitHub milestone's title/description/due date via
    ``gh api -X PATCH .../milestones/{number}`` (#645 seam, mirrors
    ``edit_issue``). A no-op (all three fields ``None``) returns ``{}``
    without shelling out. Returns the updated milestone's JSON."""
    if title is None and description is None and due_on is None:
        return {}
    args = ["api", "-X", "PATCH", f"repos/{repo}/milestones/{number}"]
    if title is not None:
        args += ["-f", f"title={title}"]
    if description is not None:
        args += ["-f", f"description={description}"]
    if due_on is not None:
        args += ["-f", f"due_on={due_on}"]
    return _json_loads_or(_gh(*args, caller="github_ops.edit_milestone"), default={})


class IssueHasOpenChildrenError(RuntimeError):
    """Raised by :func:`close_issue` when *issue_number* has open children and
    ``force`` was not passed (#1196).

    The close-invariant chokepoint: every deterministic close path in this
    codebase (``merge_queue``, ``state._close_issue_local``,
    ``commands/issues``) funnels through :func:`close_issue`, so guarding
    here is the one place that stops an epic reading as "done" while its
    sub-issues are still open — see claude-coordinator#1041, the incident
    that prompted this. A subclass of ``RuntimeError`` so existing
    ``except RuntimeError`` / ``except Exception`` call sites keep working
    without modification; catch this specifically to distinguish "refused,
    has open children" from any other close failure.
    """


def get_issues_live_state(repo: str, numbers: list[int]) -> dict[int, str]:
    """Batch-fetch the *live* open/closed state of each issue in *numbers*.

    One GraphQL request (one aliased ``issue(number: N)`` field per number)
    rather than N ``gh issue view`` round-trips — see #1354: a close-guard
    that fans out one lookup per child turns closing an epic into N API
    calls. Returns ``{number: "open" | "closed"}``; a number that GitHub
    doesn't resolve (deleted, wrong repo) is simply absent from the result.

    Returns ``{}`` on any failure — a bad *repo* string, a ``gh`` error, or
    an unparseable response — so callers can treat "no live data" uniformly
    and fall back to their offline signal (see :func:`get_open_children`),
    matching the deliberate fail-open contract on the parent lookup.
    """
    if not numbers:
        return {}
    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        return {}
    unique = sorted(set(numbers))
    fields = "\n".join(
        f"  n{n}: issue(number: {n}) {{ number state }}" for n in unique
    )
    query = (
        f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        f"{fields}\n"
        f"}} }}"
    )
    try:
        data = _gh_json("api", "graphql", "-f", f"query={query}", default={},
            caller="github_ops.get_issues_live_state")
    except RuntimeError:
        return {}
    data_field = data.get("data") if isinstance(data, dict) else None
    repo_data = data_field.get("repository") if isinstance(data_field, dict) else None
    if not isinstance(repo_data, dict):
        return {}
    result: dict[int, str] = {}
    for value in repo_data.values():
        if not isinstance(value, dict):
            continue
        num, state = value.get("number"), value.get("state")
        if num is None or state is None:
            continue
        result[int(num)] = str(state).lower()
    return result


def get_open_children(repo: str, issue_number: int) -> list[dict]:
    """Open children of *issue_number*, per the #1195 parentage seam (#1196).

    Uses :class:`coord.parentage.MarkdownParentage` over the issue's own
    body (fetched via :func:`get_issue`) to discover *which* issues are
    children — the ``## Sub-issues`` checklist convention (#1008) is the
    primary parentage source; with ``fallback_to_work_order=True`` this also
    falls back to a ``## Work order`` block (#1221) so epics seeded before
    #1008 — which have only a Work order block, not a Sub-issues checklist —
    still register their children instead of silently reading as childless.

    Each discovered child's reported state then comes from a **live**
    lookup (:func:`get_issues_live_state`, one batched GraphQL call for all
    children at once) rather than the checklist's own ``- [x]``/``- [ ]``
    box (#1354: the box is a proxy that drifts — a closed child's box is
    often never ticked, and a ticked box can just as easily sit over a
    child that was later reopened). The checkbox is used only as the
    per-child fallback when the live lookup doesn't cover that number
    (batch call failed entirely, or GitHub didn't resolve that number) —
    preserving the pre-#1354 offline behavior for exactly the cases where a
    live answer isn't available, rather than treating a lookup failure as
    grounds to refuse or to silently allow the close.

    The live GitHub sub-issues REST API (:class:`coord.parentage_github.
    GitHubParentage`) is wired but not yet backfilled onto existing epics
    (EP-2, unbuilt), so using it *instead of* the markdown checklist here to
    discover children would silently miss every real epic and defeat the
    guard — only the per-child *state* is live, not the parent->child edges
    themselves.

    Returns ``[{"number": int, "state": "open"}, ...]``. **Fails open**
    (returns ``[]``) both when the issue lookup itself errors (a transient
    ``gh`` failure must not permanently wedge every close in the system —
    :func:`close_issue` is the enforcement point, not this lookup) and when
    the body's ``## Sub-issues`` or ``## Work order`` block fails to parse
    (a malformed checklist on *this* issue must not block closing some
    *other*, well-formed one — the same per-issue fail-isolation #1195
    already applies to the ``/board`` children display).
    """
    try:
        issue = get_issue(repo, issue_number)
    except RuntimeError:
        return []
    from coord.parentage import MarkdownParentage  # noqa: PLC0415

    body = issue.get("body") or ""
    try:
        children = MarkdownParentage().children(
            repo, issue_number, body=body, fallback_to_work_order=True,
        )
    except Exception:  # noqa: BLE001 — malformed checklist: fail open, don't wedge close
        return []
    if not children:
        return []
    live_states = get_issues_live_state(repo, [c.number for c in children])
    return [
        {"number": c.number, "state": live_states.get(c.number, c.state)}
        for c in children
        if live_states.get(c.number, c.state) == "open"
    ]


def has_open_children(repo: str, issue_number: int) -> bool:
    """True when *issue_number* has at least one open child (#1196)."""
    return bool(get_open_children(repo, issue_number))


def is_epic_issue(repo: str, issue_number: int) -> bool:
    """True when *issue_number* carries the tracking/epic label (#1318).

    Same label :data:`coord.milestone_order.TRACKING_ISSUE_LABEL` that
    ``dispatch.enforce_epic_dispatch_guard`` (#1314) and
    ``plan_followup.pr()`` (#1077/#1314) already check at dispatch/PR-create
    time. This is the merge-time counterpart used by the pre-merge
    epic-closing-keyword guard: a closing keyword anywhere in a PR body or
    commit message that targets an epic must never be allowed to auto-close
    it on merge.

    Fail-open: any ``gh`` error returns ``False`` — a transient read failure
    must not itself block a merge; the caller still has its other gates.
    """
    from coord.milestone_order import TRACKING_ISSUE_LABEL  # noqa: PLC0415

    try:
        issue_data = get_issue(repo, issue_number)
    except RuntimeError:
        return False
    labels = {lbl.get("name", "") for lbl in (issue_data.get("labels") or [])}
    return TRACKING_ISSUE_LABEL in labels


def close_issue(
    repo: str, issue_number: int, *, comment: str | None = None, force: bool = False,
) -> None:
    """Close a GitHub issue, optionally posting *comment* first.

    The deterministic counterpart to a ``Closes #N`` keyword in a PR body:
    ``coord merge`` calls this after a successful merge so an issue is never
    stranded open when a worker-created PR forgot the keyword (and
    conventional-commit ``fix(#N):`` subjects are *not* GitHub closing
    keywords).  Idempotent — closing an already-closed issue is a no-op.
    Raises RuntimeError on any other ``gh`` failure.  Part of the
    issue-tracker seam (GitHub backend); GitLab / bare-DB adapters slot in
    alongside this later (#806).

    #1196: the close-invariant chokepoint. Refuses (raises
    :class:`IssueHasOpenChildrenError`) when *issue_number* has open
    children unless *force* is ``True`` — an epic must not read as "done"
    while its sub-issues are still open/unstarted. Every deterministic
    close path in the codebase funnels through here, so this single guard
    covers all of them.

    Shells out directly rather than through :func:`_gh` because idempotency
    on "already closed" needs the exit code + stderr text *without*
    :func:`_gh`'s raise-on-nonzero contract in the way — but still records
    the same #1896 forge-availability observation :func:`_gh` would have
    (see :func:`_classify_gh_exit`), so this call site isn't a silent gap in
    that measurement.
    """
    if not force:
        open_children = get_open_children(repo, issue_number)
        if open_children:
            numbers = ", ".join(f"#{c['number']}" for c in open_children)
            raise IssueHasOpenChildrenError(
                f"refusing to close {repo}#{issue_number}: open children "
                f"{numbers} — pass force=True (CLI: --force) to override"
            )
    if comment:
        post_issue_comment(repo, issue_number, comment)
    args = ["issue", "close", str(issue_number), "--repo", repo]
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_gh_call(tuple(args), outcome="unreachable",
                        duration_s=time.monotonic() - _t0, detail=str(exc),
                        caller="github_ops.close_issue")
        raise
    duration = time.monotonic() - _t0
    if result.returncode != 0:
        stderr = result.stderr.strip()
        record_gh_call(tuple(args), outcome=_classify_gh_exit(stderr),
                        duration_s=duration, detail=stderr, caller="github_ops.close_issue")
        if "already closed" not in stderr.lower():
            raise RuntimeError(
                f"gh issue close #{issue_number} failed: {stderr}"
            )
    else:
        record_gh_call(tuple(args), outcome="ok", duration_s=duration,
                        caller="github_ops.close_issue")


def reopen_issue(
    repo: str, issue_number: int, *, comment: str | None = None,
) -> None:
    """Reopen a GitHub issue, optionally posting *comment* first.

    Idempotent — reopening an already-open issue is a no-op. Raises
    RuntimeError on any other ``gh`` failure. Part of the issue-tracker seam
    (GitHub backend); GitLab / bare-DB adapters slot in alongside this later
    (#806).

    Mirror of :func:`close_issue` for the complement operation (issue #1078).
    Shells out directly rather than through :func:`_gh` for the same
    idempotency reason as :func:`close_issue` — see that docstring — but
    still records the same #1896 forge-availability observation.
    """
    if comment:
        post_issue_comment(repo, issue_number, comment)
    args = ["issue", "reopen", str(issue_number), "--repo", repo]
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_gh_call(tuple(args), outcome="unreachable",
                        duration_s=time.monotonic() - _t0, detail=str(exc),
                        caller="github_ops.reopen_issue")
        raise
    duration = time.monotonic() - _t0
    if result.returncode != 0:
        stderr = result.stderr.strip()
        record_gh_call(tuple(args), outcome=_classify_gh_exit(stderr),
                        duration_s=duration, detail=stderr, caller="github_ops.reopen_issue")
        if "already open" not in stderr.lower():
            raise RuntimeError(
                f"gh issue reopen #{issue_number} failed: {stderr}"
            )
    else:
        record_gh_call(tuple(args), outcome="ok", duration_s=duration,
                        caller="github_ops.reopen_issue")


def check_pr_mergeable(repo: str, number: int) -> bool | None:
    """Return GitHub's current mergeability verdict for PR *number* (#1477).

    Used by the merge queue's stale-``CONFLICT`` reconciliation
    (:func:`coord.merge_queue.reconcile_conflict_entries`) to re-test whether
    a parked entry's branch has since become clean — a conflict-fix worker
    landed, or a human pushed a fix by hand — rather than trusting the
    ``gh pr merge`` failure message cached from whenever the queue last
    attempted it.

    Returns ``True`` when GitHub reports ``MERGEABLE``, ``False`` when it
    reports ``CONFLICTING``, and ``None`` for anything else — including
    ``UNKNOWN`` (GitHub computes mergeability asynchronously; a very recent
    push can read back unresolved for a few seconds) and any ``gh``
    error/timeout. Callers must treat ``None`` the same as ``False`` — an
    inconclusive read is never a green light to unpark an entry.
    """
    try:
        value = _gh_json(
            "pr", "view", str(number), "--repo", repo, "--json", "mergeable",
            default={}, caller="github_ops.check_pr_mergeable").get("mergeable")
    except Exception:  # noqa: BLE001 — fail-safe: unknown mergeability blocks nothing
        return None
    if value == "MERGEABLE":
        return True
    if value == "CONFLICTING":
        return False
    return None


def branch_has_merge_commit(repo: str, number: int) -> bool | None:
    """True when any commit on PR *number* has more than one parent (#1467).

    GitHub refuses to rebase-merge (``gh pr merge --rebase``) any branch
    containing a merge commit — a *linearity* requirement distinct from a
    content conflict. :func:`check_pr_mergeable`'s ``mergeable`` field can't
    detect this: GitHub reports a branch with a merge commit as
    ``MERGEABLE`` right up until the rebase-merge attempt itself fails with
    "This branch can't be rebased". This probe answers the question
    ``check_pr_mergeable`` can't, so :mod:`coord.merge_queue` can fall back
    from ``--rebase`` to ``--squash`` before ever hitting that refusal.

    Reads ``repos/{repo}/pulls/{number}/commits`` — each commit's
    ``parents`` array has length > 1 only for a merge commit — rather than a
    local ``git rev-list --merges``, because ``coord merge`` runs on the
    daemon host, which has no guaranteed checkout of an arbitrary configured
    repo. Pages up to 100 commits, comfortably above any real worker branch.

    Returns ``True``/``False`` when determined, and ``None`` on any ``gh``
    failure or malformed response — an inconclusive read. Mirrors
    :func:`check_pr_mergeable`'s fail-closed contract: callers must treat
    ``None`` as "don't know" and never let it drive a behaviour change (here:
    never silently switch merge method, or unpark a queue entry, on an
    inconclusive read).
    """
    try:
        raw = _gh("api", f"repos/{repo}/pulls/{number}/commits?per_page=100",
            caller="github_ops.branch_has_merge_commit")
        commits = json.loads(raw)
    except Exception:  # noqa: BLE001 — fail-safe: unknown parents blocks nothing
        return None
    if not isinstance(commits, list):
        return None
    try:
        return any(len(c.get("parents") or []) > 1 for c in commits)
    except (AttributeError, TypeError):
        return None


def get_pr_body(repo: str, number: int) -> str:
    """Return PR *number*'s current body text (empty string if unset)."""
    return _gh_json(
        "pr", "view", str(number), "--repo", repo, "--json", "body", default={},
            caller="github_ops.get_pr_body").get("body") or ""


def edit_pr_body(repo: str, number: int, body: str) -> None:
    """Overwrite PR *number*'s body text via ``gh pr edit --body``."""
    _gh("pr", "edit", str(number), "--repo", repo, "--body", body,
        caller="github_ops.edit_pr_body")


def get_pr_commit_messages(repo: str, number: int) -> list[str]:
    """Return the full commit message (headline + body) of every commit on
    PR *number*, in commit order (#1318).

    GitHub's closing-keyword scanner reads commit messages verbatim once
    they land on the base branch — for ``--rebase``/``--merge`` methods
    that's every original commit, unchanged, so a keyword buried in
    commit-message prose (e.g. explaining a bug fixed elsewhere, in a
    quote) can auto-close an issue on merge exactly like a PR-body keyword
    (#1196) does, and no PR-body edit can neutralize it. Best-effort:
    returns ``[]`` on any ``gh`` failure or malformed response, same
    fail-open posture as :func:`get_pr_body`.
    """
    try:
        raw = _gh("pr", "view", str(number), "--repo", repo, "--json", "commits",
            caller="github_ops.get_pr_commit_messages")
    except RuntimeError:
        return []
    data = _json_loads_or(raw, default={})
    commits = (data.get("commits") if isinstance(data, dict) else None) or []
    messages: list[str] = []
    for c in commits:
        headline = (c.get("messageHeadline") or "").strip()
        body = (c.get("messageBody") or "").strip()
        messages.append(f"{headline}\n\n{body}" if body else headline)
    return messages


def issue_is_closed(repo: str, issue_number: int) -> bool:
    """True when issue ``issue_number`` is closed on GitHub.

    Best-effort and **fail-open**: any ``gh`` error returns ``False`` so a
    transient GitHub/CLI failure never silently blocks a legitimate dispatch.
    """
    try:
        return get_issue(repo, issue_number).get("state", "").upper() == "CLOSED"
    except (RuntimeError, json.JSONDecodeError):
        return False


def pr_is_merged(repo: str, branch: str) -> bool:
    """True when ``branch``'s *current* tip is a commit that actually merged on ``repo``.

    Uses ``gh pr list --head <branch> --state all`` rather than ``pr view`` so
    the result survives **branch deletion after merge** and the quadraui case
    where a PR merged into ``develop`` leaves its linked issue OPEN (so
    :func:`issue_is_closed` would miss it).  Best-effort and **fail-open**:
    returns ``False`` when there is no PR, the PR is still open, or ``gh``
    errors — never blocks a legitimate dispatch on a transient failure.

    #1150: branch reuse across merge cycles is a designed pattern
    (``--fix-of``/``--rework-of`` continue on the same branch; ``--force`` can
    re-target a branch name with prior history) — so "a PR with this head ref
    name merged *at some point*" is not proof that the branch's *current*
    commits are merged. To distinguish those cases, once a merged PR is found
    we resolve the branch's current tip via :func:`get_branch_sha` (the same
    GitHub-API SHA lookup #821 uses for stale-review detection) and require it
    to match the merged PR's ``headRefOid`` — the exact commit that landed.

    When the tip can't be resolved via ``get_branch_sha`` (it fails closed to
    ``None`` on *any* error, transient or not — see its docstring), we do
    **not** blindly trust the historical merge, because that would reintroduce
    this same issue's bug class under a transient-failure trigger: a rate
    limit or network blip at the wrong moment would read as "already merged"
    and callers (``reconcile``'s merge sweep, ``prune_stale_queue_entries``)
    would permanently mark live, unmerged work as done or delete its queue
    entry. Trusting history is only actually safe in the one case where it's
    *structurally* impossible for new commits to exist: the branch was
    positively confirmed deleted (a 404, via :func:`branch_exists_on_remote`,
    which distinguishes "GitHub said not found" from any other failure).
    Every other unresolved case — auth hiccup, timeout, rate limit — fails
    open toward ``False`` ("not yet merged"), matching this function's and
    ``prune_stale_queue_entries``'s documented fail-open convention.
    """
    if not branch:
        return False
    try:
        raw = _gh(
            "pr", "list", "--repo", repo, "--head", branch,
            "--state", "all", "--json", "number,state,mergedAt,headRefOid",
            "--limit", "10", caller="github_ops.pr_is_merged")
    except RuntimeError:
        return False
    prs = _json_loads_or(raw, default=[])
    if not isinstance(prs, list):
        return False
    merged = [
        p for p in prs
        if p.get("mergedAt") or p.get("state", "").upper() == "MERGED"
    ]
    if not merged:
        return False

    current_sha = get_branch_sha(repo, branch)
    if current_sha is not None:
        return any(p.get("headRefOid") == current_sha for p in merged)
    # SHA lookup failed. Only trust the historical merge if we can positively
    # confirm the branch is gone (a 404 means no further commits could have
    # been pushed to it). Any other failure (transient network/auth/rate
    # limit) fails open toward False — see docstring.
    if not branch_exists_on_remote(repo, branch):
        return True  # confirmed deleted — no new commits possible; trust history
    return False


def work_is_terminal(
    repo_github: str,
    issue_number: int | None,
    branch: str | None,
    *,
    cache: dict | None = None,
    trust_issue_closed: bool = True,
) -> bool:
    """True when work is already done on GitHub: **issue closed OR PR merged**.

    The single chokepoint guard (#522) consulted before any fix/review
    dispatch, so already-merged/closed work can never re-enter the loop (the
    root cause of the 2026-06-09 launch flood: #349 ×4, #194).

    Best-effort and **fail-open**: any error resolves to ``False`` so a
    transient GitHub/CLI failure never blocks a legitimate dispatch.

    *cache* — optional ``dict`` shared across a single ``notify`` run, keyed by
    ``(repo_github, issue_number, branch, trust_issue_closed)``, so a burst of
    transitions for the same merged issue costs **one** ``gh`` round-trip, not
    one per call.

    *trust_issue_closed* — set ``False`` when *issue_number* is not this row's
    own deliverable — e.g. a ``test-author``/``mock-author`` row, whose
    ``issue_number`` is always the milestone's *tracking* issue, never the
    per-slice issue it's actually writing (:data:`coord.models.
    SEALED_PATH_AUTHOR_TYPES`; the real one lives in ``for_issue_number``).
    A tracking issue is closed for most of a milestone's life while slices
    are still being authored against it, so trusting ``issue_is_closed`` here
    reports *every* such row "terminal" the moment the epic closes —
    regardless of whether THIS row's own branch ever landed — and a
    conservative caller like :func:`coord.reconcile.reconcile_board_merges`
    then permanently flips it to ``status='merged'`` with nothing on GitHub
    to show for it (#2639). With this ``False``, only ``pr_is_merged``
    (branch/commit-scoped since #1150) decides — exactly right for a row
    whose own landed-ness can only be answered by its own branch. Defaults to
    ``True`` so every other caller (chiefly ``type='work'``, where
    ``issue_number`` *is* the row's own deliverable — :data:`coord.models.
    CLOSES_ISSUE_TYPES`) keeps today's behaviour, including the #522 flood
    guard: manually closing a ``type='work'`` issue must still retire it here.
    """
    if not repo_github:
        return False

    key = (repo_github, issue_number, branch, trust_issue_closed)
    if cache is not None and key in cache:
        return cache[key]

    terminal = False
    try:
        if (
            trust_issue_closed
            and issue_number
            and issue_is_closed(repo_github, issue_number)
        ):
            terminal = True
        elif branch and pr_is_merged(repo_github, branch):
            terminal = True
    except Exception:  # noqa: BLE001 — fail-open: never block a dispatch
        terminal = False

    if cache is not None:
        cache[key] = terminal
    return terminal


# ── #873: durable issue_comments mirror — capture-at-write ──────────────────

_COMMENT_ID_RE = re.compile(r"issuecomment-(\d+)")

# Memoized per-process: the authenticated gh identity, used as a best-effort
# `author` on capture-at-write rows. One extra `gh api user` call the first
# time a comment is posted in this process; the backfill sync
# (state.sync_issue_comments) overwrites it with the real per-comment author
# regardless, so a stale/failed lookup here is harmless.
_login_cache: dict[str, str | None] = {}


def parse_comment_id(url: str) -> int | None:
    """Extract the numeric REST comment id from a GitHub comment URL
    (``...#issuecomment-<digits>``) — the format both ``gh issue comment``'s
    stdout and ``gh issue view --json comments``'s ``url`` field use. Returns
    ``None`` when *url* doesn't match (e.g. blank, or gh's output format
    changes)."""
    m = _COMMENT_ID_RE.search(url or "")
    return int(m.group(1)) if m else None


def _current_gh_login() -> str | None:
    if "login" not in _login_cache:
        try:
            _login_cache["login"] = _gh("api", "user", "--jq", ".login",
                caller="github_ops._current_gh_login") or None
        except Exception:  # noqa: BLE001 — best-effort; capture still proceeds without it
            _login_cache["login"] = None
    return _login_cache["login"]


def get_issue_comments(repo: str, issue_number: int) -> list[dict]:
    """All comments on *issue_number*, oldest first (gh's default order).

    Each dict carries at least ``id`` (a GraphQL node id — NOT the numeric
    REST id; use :func:`parse_comment_id` on ``url`` for that), ``url``,
    ``author`` (``{"login": ...}``), ``body``, ``createdAt``. Used by
    ``state.sync_issue_comments`` (#873) to backfill the ``issue_comments``
    mirror with human + out-of-band comments coord never wrote itself.
    """
    return _gh_json(
        "issue", "view", str(issue_number), "--repo", repo, "--json", "comments",
        default={}, caller="github_ops.get_issue_comments").get("comments", [])


def post_issue_comment(repo: str, issue_number: int, body: str):
    url = _gh("issue", "comment", str(issue_number), "--repo", repo, "--body", body,
        caller="github_ops.post_issue_comment")
    _capture_comment_write(repo, issue_number, body, url)


def _capture_comment_write(repo: str, issue_number: int, body: str, url: str) -> None:
    """Best-effort mirror of a just-posted comment into the durable
    ``issue_comments`` table (#873).

    Capture-at-write: the coord-authored prose message bus (completion
    summaries, review bodies, failure reports) becomes durable and
    machine-independent the instant it posts, rather than depending on a
    later reconciliation/recovery pass. Never raises — a mirror failure must
    never undo (or even surface as an error against) a GitHub comment that
    already landed.
    """
    try:
        from coord import state  # noqa: PLC0415 — avoid a github_ops<->state import cycle

        state.record_issue_comment_capture(
            repo_name=repo,
            issue_number=issue_number,
            body=body,
            gh_comment_id=parse_comment_id(url),
            author=_current_gh_login(),
        )
    except Exception:  # noqa: BLE001 — fail-open, see docstring
        pass


def add_issue_labels(repo: str, issue_number: int, labels: list[str]) -> None:
    """Add labels to an issue. Idempotent — `gh issue edit --add-label`
    silently no-ops when the label is already present.  Raises RuntimeError
    on `gh` failure; callers should wrap in try/except when labeling is
    best-effort (e.g. post-dispatch auto-tagging)."""
    if not labels:
        return
    args = ["issue", "edit", str(issue_number), "--repo", repo]
    for lbl in labels:
        args.extend(["--add-label", lbl])
    _gh(*args, caller="github_ops.add_issue_labels")


def create_label(
    repo: str,
    label: str,
    *,
    color: str | None = None,
    description: str | None = None,
    force: bool = True,
) -> None:
    """Create *label* in *repo* via ``gh label create``.

    ``force=True`` (the default) makes this idempotent — ``gh`` overwrites
    the color/description if the label already exists instead of erroring.
    Raises ``RuntimeError`` (a plain non-zero ``gh`` exit) or its subclass
    :class:`GhError` (``gh`` missing from PATH or timed out — see ``_gh``) on
    failure; callers that treat label pre-creation as best-effort (e.g. a
    concurrent-create race) should catch ``RuntimeError`` to cover both. Used
    by ``coord set-test-mode`` (#1483) to ensure the ``test-mode:*`` labels
    exist before ``change_issue_labels`` tries to add one.
    """
    args = ["label", "create", label, "--repo", repo]
    if color:
        args.extend(["--color", color])
    if description:
        args.extend(["--description", description])
    if force:
        args.append("--force")
    _gh(*args, caller="github_ops.create_label")


def remove_issue_label(repo: str, issue_number: int, label: str) -> None:
    """Remove a label from an issue via ``gh issue edit --remove-label``.

    Idempotent — ``gh`` silently no-ops if the label is not present.
    Raises RuntimeError on ``gh`` failure.
    """
    _gh("issue", "edit", str(issue_number), "--repo", repo, "--remove-label", label,
        caller="github_ops.remove_issue_label")


def change_issue_labels(
    repo: str,
    issue_number: int,
    *,
    add: set[str],
    remove: set[str],
) -> tuple[list[str], bool]:
    """Atomically add and/or remove arbitrary labels on an issue (#802).

    Fetches the current label set first, computes the minimal delta, and
    runs a single ``gh issue edit`` call only when something actually
    changes — tolerates already-present ``add`` labels and already-absent
    ``remove`` labels (idempotent, matches ``_apply_label_change``'s
    pre-#802 behavior).

    Returns ``(new_labels, changed)`` where ``new_labels`` is the final
    label list (sorted) and ``changed`` is ``True`` when any labels were
    added or removed. Raises ``RuntimeError`` on ``gh`` failure.
    """
    view_data = _gh_json(
        "issue", "view", str(issue_number), "--repo", repo, "--json", "labels",
        default={}, caller="github_ops.change_issue_labels")
    current: set[str] = {
        lbl.get("name", "")
        for lbl in view_data.get("labels", [])
    }

    to_add = add - current
    to_remove = remove & current
    changed = bool(to_add or to_remove)

    if changed:
        args = ["issue", "edit", str(issue_number), "--repo", repo]
        for lbl in sorted(to_add):
            args.extend(["--add-label", lbl])
        for lbl in sorted(to_remove):
            args.extend(["--remove-label", lbl])
        try:
            _gh(*args, caller="github_ops.change_issue_labels")
        except RuntimeError as exc:
            if to_add and _is_label_not_found(exc):
                # A label in ``to_add`` doesn't exist in the repo yet.
                # Auto-create each label and retry the edit once.  Only
                # triggered on the add path (``to_add`` is non-empty) and
                # only for label-not-found errors — auth, network, and
                # rate-limit failures are re-raised immediately without
                # touching GitHub.  ``gh label create`` errors are swallowed
                # so an "already exists" race on a concurrent create is
                # handled gracefully.
                for lbl in sorted(to_add):
                    try:
                        _gh("label", "create", lbl, "--repo", repo,
                            caller="github_ops.change_issue_labels")
                    except RuntimeError:
                        pass  # idempotent: label may already exist
                try:
                    _gh(*args, caller="github_ops.change_issue_labels")
                except RuntimeError as retry_exc:
                    if _is_label_not_found(retry_exc):
                        raise GhNotFound(str(retry_exc)) from retry_exc
                    raise
            else:
                raise

    new_labels = sorted((current - to_remove) | to_add)
    return new_labels, changed


_TEST_MODE_LABELS = ("test-mode:smoke", "test-mode:auto")


def set_test_mode_label(
    repo_github: str,
    repo_name: str,
    issue_number: int,
    mode: str,
) -> None:
    """Persist the per-issue test-mode policy as a GitHub label.

    Removes any existing ``test-mode:*`` label then adds ``test-mode:{mode}``.
    Also updates the local issues cache so the TUI pipeline reflects the change
    without waiting for the next ``coord sync``.

    ``repo_github`` — ``owner/name`` slug for the ``gh`` CLI.
    ``repo_name``   — coordinator-local repo name for the DB cache.
    ``mode``        — ``"smoke"`` or ``"auto"``.
    """
    from coord import state as _state  # noqa: PLC0415

    if mode not in ("smoke", "auto"):
        raise ValueError(f"mode must be 'smoke' or 'auto', got {mode!r}")

    # Step 1: remove any stale test-mode:* labels.
    for old_label in _TEST_MODE_LABELS:
        try:
            remove_issue_label(repo_github, issue_number, old_label)
        except RuntimeError:
            pass  # already absent — not an error

    # Step 2: add the new label (idempotent).
    new_label = f"test-mode:{mode}"
    add_issue_labels(repo_github, issue_number, [new_label])

    # Step 3: refresh the local cache so the TUI sees the update.
    try:
        issue_data = get_issue(repo_github, issue_number)
        current_labels = [lbl.get("name", "") for lbl in issue_data.get("labels", [])]
        _state.update_issue_labels(repo_name, issue_number, current_labels)
    except Exception:
        pass  # cache update is best-effort


def get_repo_file_with_sha(repo: str, path: str, branch: str = "develop") -> tuple[str, str]:
    """Like :func:`get_repo_file` but also returns the blob's current
    ``sha`` — the Contents API's optimistic-concurrency token
    :func:`update_repo_file` needs to PUT an edit to this exact revision.

    #2164: added for the post-merge ``expected_red`` clearing sweep
    (``coord.acceptance.clear_expected_red_via_pr``), which reads-then-edits
    a manifest purely through the GitHub API (no local checkout — the merge
    queue is a ``gh``-only wire layer, see ``coord.merge_queue.process``'s
    docstring). :func:`get_repo_file` is now a thin wrapper over this.
    """
    import base64
    raw = _gh("api", f"repos/{repo}/contents/{path}?ref={branch}",
        caller="github_ops.get_repo_file_with_sha")
    data = _json_loads_or(raw, default=None)
    # #1353: an empty/malformed-but-exit-0 response used to bare-json.loads()
    # into an unattributable JSONDecodeError (or, post-decode, a KeyError on
    # "content"). Both callers of this function (_default_gate_a_file_exists,
    # _default_fetch_repo_file) already catch RuntimeError to mean "file
    # doesn't exist" — raise that instead, so a `gh` hiccup degrades to the
    # same handled path as a real 404 rather than an uncaught crash.
    if not isinstance(data, dict) or "content" not in data or "sha" not in data:
        raise RuntimeError(
            f"gh api repos/{repo}/contents/{path}?ref={branch}: "
            "empty or malformed response"
        )
    return base64.b64decode(data["content"]).decode(), data["sha"]


def get_repo_file(repo: str, path: str, branch: str = "develop") -> str:
    return get_repo_file_with_sha(repo, path, branch)[0]


def update_repo_file(
    repo: str, path: str, branch: str, content: str, message: str, *, sha: str,
) -> str:
    """Commit *content* to *path* on *branch* via the Contents API (a single
    commit, directly on that branch — no local checkout). Returns the new
    commit sha.

    #2164: the write half of the post-merge ``expected_red`` clearing
    sweep. *branch* is expected to be a throwaway branch created off the
    default branch's current tip (:func:`create_remote_branch`) — the
    resulting commit is then opened as a PR (:func:`create_pr`) and merged
    the normal way (:func:`merge_pr`), so a protected default branch (this
    repo's own CLAUDE.md: "main requires passing status checks... a plain
    `git push origin main` is rejected") accepts it exactly like any other
    change, instead of a raw push such a repo would reject outright.
    *sha* is the blob sha from :func:`get_repo_file_with_sha` — the API
    refuses the write (409) if the file moved since it was read.
    """
    import base64
    raw = _gh(
        "api", "-X", "PUT", f"repos/{repo}/contents/{path}",
        "-f", f"message={message}",
        "-f", f"content={base64.b64encode(content.encode()).decode()}",
        "-f", f"branch={branch}",
        "-f", f"sha={sha}", caller="github_ops.update_repo_file")
    data = _json_loads_or(raw, default={})
    return ((data or {}).get("commit") or {}).get("sha", "")


def list_repo_dir(repo: str, path: str, branch: str = "develop") -> list[str]:
    """Filenames (not full paths) directly under *path* on *branch*.

    Same ``contents`` endpoint :func:`get_repo_file` uses, which returns a
    JSON array (rather than a single file object) when *path* is a
    directory. Raises like :func:`get_repo_file` (``RuntimeError`` via
    ``_gh``) when *path* doesn't exist — callers that want a soft "not
    found" should catch that, mirroring ``_default_gate_a_file_exists``.
    """
    raw = _gh("api", f"repos/{repo}/contents/{path}?ref={branch}",
        caller="github_ops.list_repo_dir")
    data = _json_loads_or(raw, default=None)
    if not isinstance(data, list):
        return []
    return [entry["name"] for entry in data if entry.get("type") == "file"]


def list_repo_subdirs(repo: str, path: str, branch: str = "develop") -> list[str]:
    """Directory names (not full paths) directly under *path* on *branch* —
    the ``type == "dir"`` sibling of :func:`list_repo_dir`.

    #2164: used to enumerate ``tests/acceptance/ms-*/`` via the API alone
    (no local checkout) when hunting for the ``ms-NN`` manifest that maps a
    given issue — see ``coord.acceptance.find_ms_manifest_for_issue_via_api``.
    """
    raw = _gh("api", f"repos/{repo}/contents/{path}?ref={branch}",
        caller="github_ops.list_repo_subdirs")
    data = _json_loads_or(raw, default=None)
    if not isinstance(data, list):
        return []
    return [entry["name"] for entry in data if entry.get("type") == "dir"]


def check_branch_exists(repo: str, branch: str) -> bool:
    try:
        _gh("api", f"repos/{repo}/branches/{branch}", caller="github_ops.check_branch_exists")
        return True
    except RuntimeError:
        return False


def get_repo_default_branch(repo: str) -> str:
    """The repo's REAL default branch, from GitHub (#2220).

    ``coord repo add`` reads this rather than trusting a ``--default-branch``
    flag, and ``coord repo doctor`` compares it against the configured value:
    a ``default_branch`` that disagrees with the repo's actual default routes
    every worker PR to the wrong base, and nothing else in the fleet notices.

    Raises (``RuntimeError``/:class:`GhError`) on any read failure rather than
    guessing ``"main"`` — a caller that cannot tell "the default is main" from
    "I could not ask" must not treat the second as the first.
    """
    data = _gh_json("api", f"repos/{repo}", default=None,
        caller="github_ops.get_repo_default_branch")
    if not isinstance(data, dict) or not data.get("default_branch"):
        raise RuntimeError(f"gh api repos/{repo}: no default_branch in response")
    return str(data["default_branch"])


def list_repo_labels(repo: str) -> list[str]:
    """Every label name defined in *repo* (#2220).

    Backs the ``coord`` / ``tier:*`` label checks in ``coord repo doctor``:
    without the ``coord`` label an issue is live on GitHub but invisible to
    the Pipeline, which is indistinguishable from "nobody has filed anything
    yet". Raises on read failure — see :func:`get_repo_default_branch`.
    """
    data = _gh_json(
        "api", "--paginate", "--slurp", f"repos/{repo}/labels", default=None,
            caller="github_ops.list_repo_labels")
    # `--paginate --slurp` yields a list of per-page arrays; a single
    # unpaginated read yields a flat array. Accept both rather than depending
    # on how many labels the repo happens to have.
    if not isinstance(data, list):
        raise RuntimeError(f"gh api repos/{repo}/labels: malformed response")
    out: list[str] = []
    for entry in data:
        if isinstance(entry, list):
            out.extend(str(e.get("name")) for e in entry if isinstance(e, dict) and e.get("name"))
        elif isinstance(entry, dict) and entry.get("name"):
            out.append(str(entry["name"]))
    return out


def list_repo_workflows(repo: str) -> list[dict]:
    """The GitHub Actions workflow definitions GitHub knows about for *repo*.

    Sibling of :func:`get_repo_workflow_count` (which only needs the count for
    ``expects_checks``); this returns the entries themselves so ``coord repo
    doctor`` can go on and read each one's ``on:`` triggers. Each dict carries
    at least ``name`` and ``path``.

    Raises on read failure rather than returning ``[]`` — same reasoning as
    :func:`get_repo_workflow_count`: "no workflows" and "couldn't check" have
    opposite consequences for the merge gate.
    """
    data = _gh_json("api", f"repos/{repo}/actions/workflows", default=None,
        caller="github_ops.list_repo_workflows")
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
        raise RuntimeError(
            f"gh api repos/{repo}/actions/workflows: malformed response"
        )
    return [w for w in data["workflows"] if isinstance(w, dict)]


def repo_file_exists(repo: str, path: str, branch: str) -> bool:
    """True when *path* exists on *branch* of *repo* (#2220).

    Thin, explicitly-branch-taking wrapper over the Contents API —
    :func:`get_repo_file` defaults to ``develop``, which is wrong for the
    majority of repos this is asked about. Returns ``False`` only for a real
    "not found"; every other failure propagates, so a caller never reads an
    auth error as a missing ``CLAUDE.md``.
    """
    try:
        _gh("api", f"repos/{repo}/contents/{path}?ref={branch}",
            caller="github_ops.repo_file_exists")
        return True
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "404" in msg:
            return False
        raise


def list_remote_branch_names(repo: str) -> set[str]:
    """Return the set of branch names that currently exist on `repo` (owner/name).

    One paginated ``gh api`` call.  Used by ``coord merge`` to skip re-enqueuing
    done-work whose branch was already merged-and-deleted (the dominant
    merge-queue clog source).  Returns an empty set on error so callers can
    fail OPEN (treat "couldn't determine" as "don't skip").
    """
    try:
        raw = _gh(
            "api", "--paginate",
            f"repos/{repo}/git/refs/heads",
            "--jq", ".[].ref", caller="github_ops.list_remote_branch_names")
    except RuntimeError:
        return set()
    prefix = "refs/heads/"
    return {
        line.strip()[len(prefix):]
        for line in raw.splitlines()
        if line.strip().startswith(prefix)
    }


def branch_exists_on_remote(
    repo: str, branch: str, *, cache: dict | None = None
) -> bool:
    """Return True if `branch` currently exists on `repo` (owner/name) at GitHub.

    Uses a targeted ``gh api`` call rather than listing all branches.  Fails
    OPEN (returns True) on any infrastructure problem — an unresponsive ``gh``,
    a network glitch, or an authentication issue must never prevent a legitimate
    dispatch.  Only returns False when we receive a clear "not found" signal
    from GitHub (HTTP 4xx in the error output).

    Called by ``dispatch_review`` and ``_dispatch_fix`` (#586) to avoid
    routing a follow-on assignment to a machine that can't fetch the branch.

    *cache* (#2989) is an optional caller-owned ``dict`` used to memoise the
    lookup for the duration of ONE sweep, mirroring
    :func:`work_is_terminal`'s ``cache=``.  A single ``reconcile_board_merges``
    pass measured 1,304 ref lookups for only 851 distinct refs (1.53x
    redundancy) because sibling assignment rows — a work row and its
    conflict-fix, or a re-dispatch — legitimately share one branch.  The
    cache is deliberately **per-pass and caller-scoped**, never module-level:
    branch existence is exactly the kind of fact that must be re-read on the
    next pass.
    """
    key = (repo, branch)
    if cache is not None and key in cache:
        return cache[key]
    result = _branch_exists_on_remote_uncached(repo, branch)
    if cache is not None:
        cache[key] = result
    return result


def _branch_exists_on_remote_uncached(repo: str, branch: str) -> bool:
    try:
        _gh("api", f"repos/{repo}/git/refs/heads/{branch}",
            caller="github_ops.branch_exists_on_remote")
        return True
    except RuntimeError as exc:
        err = str(exc).lower()
        # Only return False when GitHub explicitly told us the ref doesn't
        # exist (HTTP 4xx response).  Any other failure (gh not installed,
        # not authenticated, network timeout) is treated as "unknown" and we
        # fail OPEN so the guard doesn't block legitimate dispatch.
        if "http 4" in err or "could not resolve" in err or "not found" in err:
            return False
        return True


def delete_remote_branch(repo: str, branch: str) -> bool:
    """Delete a remote branch. Returns True on success, False on failure."""
    try:
        _gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}",
            caller="github_ops.delete_remote_branch")
        return True
    except RuntimeError:
        return False


def create_remote_branch(repo: str, branch: str, sha: str) -> bool:
    """Create a remote branch (a ``refs/heads/{branch}`` ref) pointing at
    *sha*. Returns True on success, False on failure.

    #934: used by ``coord.branch_model.ensure_feature_branch_exists`` to
    create ``feature/ms-NN`` off ``develop`` on demand, idempotently (the
    caller checks ``branch_exists_on_remote`` first).
    """
    try:
        _gh(
            "api", "-X", "POST", f"repos/{repo}/git/refs",
            "-f", f"ref=refs/heads/{branch}",
            "-f", f"sha={sha}", caller="github_ops.create_remote_branch")
        return True
    except RuntimeError:
        return False


# ── Repo creation, the forge seam (IL-1, #2747) ─────────────────────────────
#
# `coord repo add` requires the repo to already exist on GitHub — the only
# way to make one was a raw `gh repo create`, exactly the habit the
# backend-agnostic forge seam exists to stop, and one workers can't do at
# all (`gh` is deny-listed for them). These three functions are that seam's
# GitHub backend: `create_repo` (mirrors `create_issue`/`create_milestone` —
# the single place this module shells out to `gh repo create`, so a GitLab
# backend later replaces this one function instead of every call site) plus
# `repo_exists` (the pre-flight `coord repo create` uses to refuse reusing an
# existing repo) and `create_commit_with_files` (seeds CLAUDE.md, the CI
# workflow, and the ported `.githooks/*` in one commit via the Git Data API —
# not the Contents API `update_repo_file` uses, because that API always
# writes files at mode 100644 and silently strips the executable bit the
# `.githooks/post-*` shims need to run at all as git hooks).


def repo_exists(repo: str) -> bool:
    """True when *repo* (``owner/name``) already exists on GitHub.

    `coord repo create`'s pre-flight: creation is for a NEW repo, and an
    existing one should go through `coord repo add` instead — reusing a name
    would silently seed CLAUDE.md/CI/.githooks on top of someone else's repo.
    Raises on anything that isn't a clean 404 (auth, network, rate-limit) —
    a false "doesn't exist" here is what would let `create_repo` attempt a
    create against a repo that's merely unreadable right now, which then
    fails with `gh`'s much less clear "name already exists" error instead of
    surfacing the real (auth/network) problem immediately.
    """
    try:
        _gh("api", f"repos/{repo}", caller="github_ops.repo_exists")
        return True
    except RuntimeError as exc:
        msg = str(exc).lower()
        # Same broad match :func:`repo_file_exists` uses — real `gh` 404 text
        # varies by version/endpoint ("HTTP 404: Not Found", "gh: Not Found
        # (HTTP 404)", ...), so match on either signal rather than one exact
        # phrasing.
        if "not found" in msg or "404" in msg:
            return False
        raise


def create_repo(
    repo: str, *, private: bool = False, description: str | None = None,
) -> dict:
    """Create a new GitHub repository via ``gh repo create`` (#2747).

    Passes *repo* as a full ``owner/name`` slug and neither ``--clone`` nor
    ``--source`` — ``gh`` then creates the remote repo directly with no local
    checkout involved, which matters here: this runs from whatever directory
    the caller happens to be in (a worker's worktree, an operator's shell),
    and must never touch it.

    Always creates with ``--add-readme`` so the repo has a real default-branch
    commit to seed onto immediately after: an empty repo has zero refs, and
    the Git Data API :func:`create_commit_with_files` uses needs a parent
    commit + base tree to build the seed commit on top of — there is nothing
    to branch from otherwise.

    Returns ``{"name", "full_name", "url", "default_branch"}`` read back from
    the API (not parsed from ``gh``'s prose stdout). Raises
    ``RuntimeError``/:class:`GhError` on failure, including the name already
    being taken — callers that want idempotency should check
    :func:`repo_exists` first.

    Note there is an inherent TOCTOU gap between a caller's
    :func:`repo_exists` pre-flight and this call — accepted, not overlooked:
    nothing else touches GitHub in between (so the window is small) and
    ``gh repo create``'s own "name already exists" error is a reasonable
    fallback if the race is actually hit.
    """
    args = ["repo", "create", repo, "--private" if private else "--public", "--add-readme"]
    if description:
        args += ["--description", description]
    _gh(*args, caller="github_ops.create_repo")
    data = _gh_json("api", f"repos/{repo}", default=None, caller="github_ops.create_repo")
    if not isinstance(data, dict) or not data.get("default_branch"):
        raise RuntimeError(f"repos/{repo}: created but could not read it back")
    return {
        "name": data.get("name"),
        "full_name": data.get("full_name"),
        "url": data.get("html_url"),
        "default_branch": data.get("default_branch"),
    }


def _gh_input_json(*args: str, body: str, caller: str = "") -> Any:
    """Like :func:`_gh_json` but pipes *body* via stdin (``gh api --input -``)
    for endpoints whose payload — a nested tree/commit object, here — can't be
    expressed with :func:`_gh`'s flat ``-f key=value`` args. Records the same
    #1896 forge-availability observation as :func:`_gh`, for the same reason
    :func:`edit_issue`/:func:`close_issue` do their own stdin plumbing instead
    of routing through it -- including the #2988 caller tag, resolved the same
    way :func:`_gh` resolves its own (see :func:`_resolve_caller`).
    """
    caller = _resolve_caller(caller)
    full_args = [*args, "--input", "-"]
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *full_args], input=body, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_gh_call(tuple(full_args), outcome="unreachable",
                        duration_s=time.monotonic() - _t0, detail=str(exc), caller=caller)
        raise GhError(f"gh {' '.join(full_args)} failed: {exc}") from exc
    duration = time.monotonic() - _t0
    if result.returncode != 0:
        stderr = result.stderr.strip()
        record_gh_call(tuple(full_args), outcome=_classify_gh_exit(stderr),
                        duration_s=duration, detail=stderr, caller=caller)
        raise RuntimeError(f"gh {' '.join(full_args)} failed: {stderr}")
    record_gh_call(tuple(full_args), outcome="ok", duration_s=duration, caller=caller)
    return _json_loads_or(result.stdout, default={})


def create_commit_with_files(
    repo: str,
    branch: str,
    files: list[tuple[str, str, bool]],
    message: str,
) -> str:
    """Commit multiple new files to *branch* in a single commit via the Git
    Data API (blob -> tree -> commit -> ref update) — the write half of
    seeding a freshly created repo (#2747).

    *files* is ``[(path, content, executable), ...]``. Each file becomes a
    blob and a tree entry at mode ``100755`` (executable) or ``100644``
    (not) — the ``.githooks/post-*`` shims need the former to run as git
    hooks at all, which is exactly what :func:`update_repo_file`'s
    Contents-API PUT can't express (it always writes 100644). One commit for
    the whole seed rather than one Contents-API commit per file.

    Reads *branch*'s current tip as the sole parent, so this must run against
    a branch that already has at least one commit (:func:`create_repo`'s
    ``--add-readme`` guarantees that for a repo it just created). Returns the
    new commit sha. Raises ``RuntimeError`` on any step's failure — a partial
    seed (some blobs created, no commit) is possible on a mid-way failure,
    but never a *corrupt* one: nothing is written to *branch* until the final
    ref update, which is the one step that can't partially apply.
    """
    ref = _gh_json("api", f"repos/{repo}/git/refs/heads/{branch}", default={},
        caller="github_ops.create_commit_with_files")
    parent_sha = ((ref or {}).get("object") or {}).get("sha")
    if not parent_sha:
        raise RuntimeError(f"repos/{repo}: could not resolve branch {branch!r} head")
    parent_commit = _gh_json("api", f"repos/{repo}/git/commits/{parent_sha}", default={},
        caller="github_ops.create_commit_with_files")
    base_tree = (parent_commit or {}).get("tree", {}).get("sha")
    if not base_tree:
        raise RuntimeError(f"repos/{repo}: could not resolve base tree for {parent_sha}")

    tree_entries = []
    for path, content, executable in files:
        blob = _gh_json(
            "api", f"repos/{repo}/git/blobs",
            "-f", f"content={base64.b64encode(content.encode()).decode()}",
            "-f", "encoding=base64",
            default={}, caller="github_ops.create_commit_with_files")
        blob_sha = (blob or {}).get("sha")
        if not blob_sha:
            raise RuntimeError(f"repos/{repo}: blob create failed for {path!r}")
        tree_entries.append({
            "path": path,
            "mode": "100755" if executable else "100644",
            "type": "blob",
            "sha": blob_sha,
        })

    new_tree = _gh_input_json(
        "api", f"repos/{repo}/git/trees",
        body=json.dumps({"base_tree": base_tree, "tree": tree_entries}),
        caller="github_ops.create_commit_with_files",
    )
    tree_sha = (new_tree or {}).get("sha")
    if not tree_sha:
        raise RuntimeError(f"repos/{repo}: tree create failed")

    new_commit = _gh_input_json(
        "api", f"repos/{repo}/git/commits",
        body=json.dumps({"message": message, "tree": tree_sha, "parents": [parent_sha]}),
        caller="github_ops.create_commit_with_files",
    )
    commit_sha = (new_commit or {}).get("sha")
    if not commit_sha:
        raise RuntimeError(f"repos/{repo}: commit create failed")

    _gh(
        "api", "-X", "PATCH", f"repos/{repo}/git/refs/heads/{branch}",
        "-f", f"sha={commit_sha}", caller="github_ops.create_commit_with_files")
    return commit_sha


def get_default_branch_head(repo: str, branch: str) -> str:
    """Return the full commit SHA at the tip of `branch` on `repo` (owner/name)."""
    # #2809: `-i` so a 403 on this call carries the real `Retry-After` /
    # `X-GitHub-Request-Id` headers into `_gh`'s `GhRateLimitError` — see
    # `_parse_gh_include`. Harmless on success: `_parse_gh_include` below
    # strips the header block back off before this parses the JSON body.
    raw = _gh("api", "-i", f"repos/{repo}/branches/{branch}",
        caller="github_ops.get_default_branch_head")
    _meta, body = _parse_gh_include(raw)
    data = _json_loads_or(body, default=None)
    # #1353: every caller of this already catches RuntimeError to mean "HEAD
    # lookup failed" — raise that instead of letting an empty/malformed
    # exit-0 response crash with an unattributable JSONDecodeError/KeyError.
    if not isinstance(data, dict) or "commit" not in data:
        raise RuntimeError(
            f"gh api repos/{repo}/branches/{branch}: empty or malformed response"
        )
    return data["commit"]["sha"]


def get_branch_sha(repo: str, branch: str, *, raise_on_transient: bool = False) -> str | None:
    """Return the current HEAD SHA for *branch* on *repo*, or ``None`` on failure.

    Best-effort wrapper around the GitHub branches API.  Returns ``None`` when
    GitHub is unavailable, ``gh`` is not authenticated, or the branch does not
    exist — callers treat ``None`` as "SHA tracking unavailable" and skip the
    commit-bound staleness check introduced in #821.

    #2704: *raise_on_transient* (default ``False``, so every caller that
    doesn't ask for it keeps the exact behaviour above) raises
    :class:`GhTransientError` instead of returning ``None`` when the failure
    looks like transient infra — auth, network, or a rate limit, per
    :func:`_is_transient_error` — rather than a confirmed-absent branch. Opt
    in only where the caller can act differently on "GitHub couldn't answer"
    versus "the branch is gone" — today that's
    :func:`coord.merge_queue.evaluate_smoke_verdict` and
    :func:`coord.merge_queue.live_gate_entry`, both via
    :func:`coord.merge_queue._gh_get_branch_sha`, which detects support for
    this kwarg before passing it (``GhOps`` is duck-typed; most stand-ins,
    e.g. :class:`coord.gate_snapshot.GateSnapshot`, don't implement it and
    are unaffected).

    #2809: this is THE call named in that issue's incident (its ``-i``
    addition is what lets a rate limit here carry the real ``Retry-After``
    header — see :func:`get_default_branch_head`). When *exc* raised by
    ``_gh`` is already a :class:`GhTransientError` (including the
    :class:`GhRateLimitError` subclass with its status/request-id/retry-after
    detail), it is re-raised AS-IS rather than rewrapped — rewrapping into a
    fresh ``GhTransientError(str(exc))`` would stringify away exactly the
    structured detail #2809 asks to preserve.
    """
    try:
        # #2809: `-i` recovers the real HTTP headers (Retry-After,
        # X-GitHub-Request-Id) on a 403 — see `get_default_branch_head`.
        raw = _gh("api", "-i", f"repos/{repo}/branches/{branch}",
            caller="github_ops.get_branch_sha")
        _meta, body = _parse_gh_include(raw)
        data = _json_loads_or(body, default={})
        return data["commit"]["sha"]
    except Exception as exc:  # noqa: BLE001 — fail-safe: unknown SHA is not blocking
        # #2809 review: an exception that is ALREADY a GhTransientError (e.g. the
        # from-cache "coordinated backoff active" GhRateLimitError _gh raises
        # while a shared backoff window is open) must always be recognized as
        # transient, even though its message uses `active_backoff.reason`
        # verbatim ("secondary_rate_limit", underscore) and "status=403"
        # (not "HTTP 403") — neither of which matches _is_transient_error's
        # substring keywords. Checking isinstance first (independent of the
        # keyword scan) is what makes the docstring's "re-raised AS-IS"
        # promise true for that dominant-during-an-incident code path,
        # instead of it silently collapsing into `return None`.
        if raise_on_transient and (isinstance(exc, GhTransientError) or _is_transient_error(exc)):
            raise (exc if isinstance(exc, GhTransientError) else GhTransientError(str(exc))) from exc
        return None


def get_branch_commit_timestamp(repo: str, branch: str) -> float | None:
    """Return the unix timestamp of *branch*'s current HEAD commit on *repo*,
    or ``None`` on failure (#1851).

    The base-side half of :func:`coord.ci_store.checks_are_stale`'s
    comparison: a green CI check's ``started_at`` predating this timestamp
    means the check ran before the base's newest commit landed — GitHub only
    re-runs ``pull_request`` checks on head ``synchronize``, never on base
    movement, so that check never saw it.

    Same endpoint as :func:`get_branch_sha` (``GET
    repos/{repo}/branches/{branch}``) — reads the commit's ``committer.date``
    (when it landed on the branch) rather than ``author.date`` (when it was
    originally authored, which for a rebased/cherry-picked commit can predate
    the merge by a wide margin and would understate how fresh the base
    actually is). Best-effort like :func:`get_branch_sha`: returns ``None``
    when GitHub is unavailable, ``gh`` is not authenticated, the branch
    doesn't exist, or the response is missing the expected fields — callers
    must treat ``None`` as "unknown" and fail closed (stale), never as "the
    base never moved".
    """
    try:
        raw = _gh("api", f"repos/{repo}/branches/{branch}",
            caller="github_ops.get_branch_commit_timestamp")
        data = _json_loads_or(raw, default={})
        date = data["commit"]["commit"]["committer"]["date"]
        return datetime.fromisoformat(str(date).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001 — fail-safe: unknown timestamp is stale, not blocking here
        return None


# ── PR operations (used by the merge queue) ──────────────────────────────

def find_pr_for_branch(repo: str, branch: str) -> dict | None:
    """Return the first open PR whose head ref matches `branch`, or None."""
    items = _gh_json(
        "pr", "list", "--repo", repo, "--state", "open",
        "--head", branch,
        "--json", "number,title,url,headRefName,baseRefName,additions,deletions,mergeable",
        "--limit", "1",
        default=[], caller="github_ops.find_pr_for_branch")
    return items[0] if items else None


def get_pr_state_for_branch(repo: str, branch: str) -> str | None:
    """Return the current lifecycle state (``OPEN``/``MERGED``/``CLOSED``) of
    the PR whose head is *branch*, or ``None`` when no such PR exists (or
    ``gh`` fails).

    Unlike :func:`find_pr_for_branch` (``pr list --state open`` — only ever
    finds *open* PRs), this resolves the branch directly via ``gh pr view``,
    which answers regardless of state. Used by
    :meth:`coord.drive.GitMergeVerifier.verify_merged` (#1483) to confirm a
    MERGED PR whose branch may since have been deleted from the remote.
    """
    try:
        state = _gh("pr", "view", branch, "--repo", repo, "--json", "state", "-q", ".state",
            caller="github_ops.get_pr_state_for_branch")
    except RuntimeError:
        return None
    return state or None


def get_pr_head_ref(repo: str, number: int) -> str | None:
    """Return PR *number*'s head branch name, or ``None`` on any ``gh``
    failure (including "no such PR").

    Used by ``coord test``'s branch-reconciliation fallback (#349, #1483) to
    recover the PR's actual head ref when the DB's recorded branch name has
    gone stale.
    """
    try:
        head_ref = _gh(
            "pr", "view", str(number), "--repo", repo,
            "--json", "headRefName", "--jq", ".headRefName", caller="github_ops.get_pr_head_ref")
    except RuntimeError:
        return None
    return head_ref or None


# #3092: the ONE string that ties a provisioned preview lane to the lookup
# that reads it back. `cloudflare/pages-action` names the GitHub Deployment's
# environment "<projectName> (Preview)"; `get_pr_deployment_url` below matches
# on this marker. Both the generator (`preview_environment_name`, used by
# `coord repo add --with-preview` to assert at PROVISION time that the
# workflow it just wrote will actually be readable) and the matcher
# (`is_preview_environment`) live here so they cannot drift into a mismatch —
# and a mismatch is silent, because `get_pr_deployment_url` returns None
# rather than raising.
PREVIEW_ENVIRONMENT_MARKER = "(Preview)"


def preview_environment_name(project_name: str) -> str:
    """The GitHub Deployment environment name ``cloudflare/pages-action``
    derives from a Pages ``projectName`` (#3092).

    Pure and vendor-specific by design: it is the action's own convention
    (``"natal-chart (Preview)"`` for ``projectName: natal-chart``), which is
    exactly why it can be *asserted* at provision time rather than probed
    with a throwaway PR.
    """
    return f"{project_name} {PREVIEW_ENVIRONMENT_MARKER}"


def is_preview_environment(environment: object) -> bool:
    """True when a GitHub Deployment's ``environment`` names a per-PR
    preview (#3092).

    The single predicate :func:`get_pr_deployment_url` selects deployments
    with. Takes ``object`` rather than ``str`` because it is applied straight
    to a value parsed out of GitHub's JSON, which is not guaranteed to be a
    string.
    """
    return isinstance(environment, str) and PREVIEW_ENVIRONMENT_MARKER in environment


def set_repo_secret(repo: str, name: str, value: str) -> None:
    """Set GitHub Actions secret *name* on *repo* to *value* (#3092).

    Deliberately NOT routed through :func:`_gh`: that helper passes every
    argument as argv, and ``gh secret set NAME --body <value>`` would put the
    Cloudflare API token in this host's process table for any local user to
    read. ``gh`` reads the value from stdin when ``--body`` is omitted, so
    that is what this does — the value never appears in argv, never in an
    exception message, and never in :func:`record_gh_call`'s telemetry (which
    only ever sees the argv this builds).

    Idempotent: ``gh secret set`` overwrites an existing secret of the same
    name. Raises :class:`GhError` on any failure, consistent with every other
    write in this module.
    """
    args = ["secret", "set", name, "--repo", repo]
    caller = "github_ops.set_repo_secret"
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *args],
            input=value, capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        record_gh_call(args, outcome="unreachable", duration_s=time.monotonic() - _t0,
                       detail="gh not found", caller=caller)
        raise GhError(f"gh {' '.join(args)} failed: gh not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        record_gh_call(args, outcome="unreachable", duration_s=time.monotonic() - _t0,
                       detail="timed out", caller=caller)
        raise GhError(f"gh {' '.join(args)} failed: timed out: {exc}") from exc
    except OSError as exc:
        record_gh_call(args, outcome="unreachable", duration_s=time.monotonic() - _t0,
                       detail=str(exc), caller=caller)
        raise GhError(f"gh {' '.join(args)} failed: {exc}") from exc
    duration = time.monotonic() - _t0
    if result.returncode != 0:
        stderr = result.stderr.strip()
        record_gh_call(args, outcome=_classify_gh_exit(stderr), duration_s=duration,
                       detail=stderr, caller=caller)
        raise GhError(f"gh {' '.join(args)} failed: {stderr}")
    record_gh_call(args, outcome="ok", duration_s=duration, caller=caller)


def get_pr_deployment_url(repo: str, branch: str) -> str | None:
    """Return the live preview-deployment URL for *branch*'s GitHub
    Deployment, or ``None`` when one can't be confirmed (#2948).

    Cloudflare Pages (via ``cloudflare/pages-action``, and any similarly-wired
    per-PR-preview host) does NOT publish a derivable preview URL — it is a
    per-deployment content hash, not a branch-alias subdomain, and there is no
    branch-alias fallback either (measured live against natal-chart, see
    ``docs/CUSTOMER_FACING_APPS.md`` §1 and #2948). The only reliable source
    is the GitHub Deployment the action creates per push, read straight from
    the forge instead of guessed:

        gh api repos/{repo}/deployments?ref={branch}
        gh api repos/{repo}/deployments/{id}/statuses

    Matches on the deployment's **environment name containing "(Preview)"**
    (Cloudflare Pages' own convention, e.g. ``"natal-chart (Preview)"``, via
    the shared :func:`is_preview_environment` predicate — #3092 asserts the
    provisioned ``projectName`` against that same predicate), NOT
    on recency/list order — production deploys are hash URLs too and
    interleave with previews in the same ``ref`` list, so picking ``[0]``
    can silently hand back a production URL. Deployments are walked
    newest-first (GitHub's default order); the first environment-matching
    deployment whose latest status carries an ``environment_url`` wins.

    Returns ``None`` — never raises — on any ``gh`` failure, a malformed
    response, a ref with no deployments, or a matched deployment whose
    statuses carry no URL yet. Every one of those is "can't confirm a real
    preview URL right now", which callers must treat as unresolved rather
    than silently falling back to a constructed guess (the #2948 bug this
    function replaces).
    """
    try:
        deployments = _gh_json(
            "api", f"repos/{repo}/deployments?ref={branch}",
            default=None, caller="github_ops.get_pr_deployment_url",
        )
    except Exception:  # noqa: BLE001 — any gh failure: no URL to report
        return None
    if not isinstance(deployments, list):
        return None
    for deployment in deployments:
        if not isinstance(deployment, dict):
            continue
        if not is_preview_environment(deployment.get("environment")):
            continue
        deployment_id = deployment.get("id")
        if deployment_id is None:
            continue
        try:
            statuses = _gh_json(
                "api", f"repos/{repo}/deployments/{deployment_id}/statuses",
                default=None, caller="github_ops.get_pr_deployment_url",
            )
        except Exception:  # noqa: BLE001 — keep looking at other candidates
            continue
        if not isinstance(statuses, list):
            continue
        for status in statuses:
            if isinstance(status, dict):
                url = status.get("environment_url")
                if isinstance(url, str) and url:
                    return url
    return None


# #1564: `gh pr checks --json` does NOT have a `conclusion` field — it never
# has. Requesting it makes `gh` exit 1 with empty stdout, which used to make
# every single merge look like an unreadable CI status (fail-closed, so it
# blocked every merge rather than passing every one, but neither is correct).
# `bucket` is gh's own pass/fail/pending/skipping/cancel rollup of `state`
# (which is a per-check verdict like SUCCESS/FAILURE, not a lifecycle phase)
# and is exactly what the merge gate wants. Pinned in a regression test
# (tests/test_github_ops.py::TestPrChecksJsonFieldsAreValid) that shells out
# to `gh pr checks --help` and asserts every field here is one `gh` advertises,
# so the next `gh` schema change fails a test instead of silently breaking
# the gate again.
PR_CHECKS_JSON_FIELDS: tuple[str, ...] = (
    "name", "state", "bucket", "link", "startedAt", "completedAt",
)

# #1564 Addendum 2: the fleet that surfaced this issue runs `gh` versions that
# disagree about whether `gh pr checks` even *has* a `--json` flag —
# dellserver's 2.45.0 (Ubuntu's apt package) does not: `gh pr checks --json
# name,state,bucket` fails with `unknown flag: --json`, exit 1, empty stdout.
# 2.86.0 (elitebook) and 2.92.0 (precision) both support `--json` fine. There
# is no gh version floor documented anywhere else in this codebase, so this
# constant is the single source of truth for it — surfaced in the actionable
# error message below (:func:`_gh_too_old_message`) and in
# ``docs/AGENT_OPERATIONS.md``'s daemon-host prerequisites.
#
# 2.86.0 is simply the *oldest version this fleet has directly observed
# working* — nobody has bisected the actual gh release that first shipped
# `pr checks --json` support, so treat this as a confirmed-good floor, not a
# precisely-researched one. Lower it if a narrower floor is ever confirmed.
GH_PR_CHECKS_JSON_MIN_VERSION = "2.86.0"

# The exact, stable cobra/pflag message `gh` emits for a flag it doesn't
# recognise at all — confirmed verbatim against dellserver's gh 2.45.0.
# Distinct from (and must be checked before assuming) the "field not valid"
# failure a newer gh gives for a bad field *name* (e.g. the original
# `conclusion` bug), which instead exits 1 with a "Unknown JSON field" body.
_GH_UNKNOWN_JSON_FLAG_MARKER = "unknown flag: --json"


def _gh_version() -> str | None:
    """Best-effort parse of ``gh --version``'s version string (e.g. "2.45.0").

    Returns ``None`` if ``gh`` is missing, times out, or prints something
    this can't parse — callers must treat that as "unknown", never fail on it.
    """
    try:
        result = subprocess.run(
            ["gh", "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"gh version (\S+)", result.stdout or "")
    return match.group(1) if match else None


def _gh_too_old_message(stderr: str) -> str:
    """Build the actionable error text for :class:`GhTooOldForJsonChecks`.

    Names the host and the installed (if determinable) and required gh
    versions explicitly — this is the whole point of #1564 Addendum 2: an
    operator reading a merge refusal should never have to guess whether the
    gate found a red check, hit a network blip, or is simply running on a
    `gh` too old to ask the question at all.
    """
    host = socket.gethostname()
    version = _gh_version() or "unknown"
    return (
        f"gh on host {host!r} (version {version}) does not support "
        f"`gh pr checks --json` at all ({stderr!r}) — gh >= "
        f"{GH_PR_CHECKS_JSON_MIN_VERSION} is required on whichever host runs "
        f"the CI merge gate. Since `coord merge` re-invokes itself on the "
        f"daemon (COORD_MERGE_ON_DAEMON), that means the *daemon* host's gh, "
        f"not the client's. See docs/AGENT_OPERATIONS.md's daemon-host "
        f"prerequisites."
    )


def get_repo_workflow_count(repo: str) -> int:
    """Number of GitHub Actions workflows GitHub recognises for *repo* (#1904).

    Backs :meth:`coord.ci_github.GitHubCi.expects_checks` — the signal that
    distinguishes "this repo has no CI configured" (an empty ``gh pr checks``
    result is correct) from "CI exists but never triggered for this PR" (an
    empty result is a red flag: a throttled webhook, a wedged run, a
    ``paths:``-filtered-out workflow). Queries the workflow *definitions*
    GitHub knows about for the repo as a whole — not any particular branch
    or PR's check runs — so the answer doesn't depend on whether this PR's
    push ever actually triggered a run, which is exactly the case this
    exists to catch.

    Raises (``RuntimeError``/:class:`GhError`) on any read failure — auth,
    rate-limit, malformed response — rather than defaulting to 0. A caller
    that can't tell "no workflows" from "couldn't check" must not silently
    treat the latter as the former; see #1525 for the identical reasoning
    applied to check-run reads themselves.
    """
    data = _gh_json("api", f"repos/{repo}/actions/workflows", default=None,
        caller="github_ops.get_repo_workflow_count")
    if not isinstance(data, dict) or not isinstance(data.get("total_count"), int):
        raise RuntimeError(
            f"gh api repos/{repo}/actions/workflows: malformed response"
        )
    return data["total_count"]


def get_required_status_check_contexts(repo: str) -> list[str] | None:
    """Required status-check context names for *repo*'s default branch (#2388).

    Backs the merge gate's "don't wait on an advisory check" filter — a repo
    can report many more `gh pr checks` entries than GitHub's own branch
    protection actually requires (this repo: 9 reported, 5 required), and a
    slow/hung advisory job (an unconditional Playwright/Chromium install
    step, say) has no business blocking `coord merge` when GitHub itself
    already considers the PR mergeable.

    Returns ``None`` — never an empty list treated as "nothing required" —
    when the default branch has no protection configured, or any read
    step fails (auth, rate-limit, malformed response, `gh` missing). #1525's
    bias applies here too: "couldn't determine what's required" must read as
    "don't filter, wait on everything reported", not as a free pass to stop
    waiting on checks that might in fact be required. Only an actually
    non-empty ``required_status_checks.contexts`` list narrows the gate.
    """
    try:
        repo_data = _gh_json("api", f"repos/{repo}", default=None,
            caller="github_ops.get_required_status_check_contexts")
        if not isinstance(repo_data, dict):
            return None
        default_branch = repo_data.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            return None
        protection = _gh_json(
            "api", f"repos/{repo}/branches/{default_branch}/protection",
            default=None, caller="github_ops.get_required_status_check_contexts")
    except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError):
        return None
    if not isinstance(protection, dict):
        return None
    contexts = protection.get("required_status_checks", {})
    contexts = contexts.get("contexts") if isinstance(contexts, dict) else None
    if not isinstance(contexts, list) or not contexts:
        return None
    return [str(c) for c in contexts]


def get_pr_checks(repo: str, number: int) -> list[dict]:
    """Return ``gh pr checks``' raw check-run list for PR *number*.

    ``gh pr checks`` exits non-zero when any check has failed, but its JSON
    stdout is still valid in that case — only raise when stdout is genuinely
    empty (a real lookup failure: bad PR number, auth, rate-limit, an old gh
    that doesn't support ``--json`` at all, ...). The single ``gh`` sink for
    :class:`coord.ci_github.GitHubCi`, the CI backend behind the merge gate
    (#1483).

    Raises :class:`GhTooOldForJsonChecks` — instead of the generic
    ``RuntimeError`` below — when the installed ``gh`` doesn't recognise
    ``--json`` on ``pr checks`` at all (#1564 Addendum 2), so callers can
    surface a distinct, actionable "upgrade gh" message rather than lumping
    it in with ordinary read failures.
    """
    result = subprocess.run(
        [
            "gh", "pr", "checks", str(number),
            "--repo", repo,
            "--json", ",".join(PR_CHECKS_JSON_FIELDS),
        ],
        capture_output=True, text=True, timeout=30,
    )
    stdout = (result.stdout or "").strip()
    if result.returncode != 0 and not stdout:
        stderr = result.stderr.strip()
        if _GH_UNKNOWN_JSON_FLAG_MARKER in stderr:
            raise GhTooOldForJsonChecks(_gh_too_old_message(stderr))
        raise RuntimeError(f"gh pr checks failed: {stderr}")
    # #1525: unlike the fail-open sites elsewhere in this module, a malformed
    # (non-empty) response here must NOT be swallowed to a quiet ``[]`` —
    # ``ci_github.GitHubCi._fetch`` deliberately catches the ``ValueError``
    # (``json.JSONDecodeError`` is a subclass) this raises and turns it into
    # a synthetic *failing* check, so the merge gate blocks and says why
    # instead of reading "no checks" as "clear to merge" (the exact silent
    # fail-open that let PR #1521 merge past a real CI failure). Only the
    # genuinely-empty-stdout case (``stdout or "[]"``, unchanged from before)
    # is a deliberate default here — that's ``gh``'s normal "zero checks
    # configured" response, not a decode failure.
    return json.loads(stdout or "[]")


def get_run_jobs(repo: str, run_id: str) -> list[dict]:
    """Return ``gh api .../actions/runs/{run_id}/jobs``' raw ``jobs`` list (#1892).

    The single ``gh`` sink for :meth:`coord.ci_github.GitHubCi.
    list_jobs_for_run` — the one call that carries per-step detail
    (``runner_name``, each step's ``name``/``conclusion``) a plain
    ``gh pr checks`` read never has. Deliberately NOT the ``--json``
    field-selection style :func:`get_pr_checks` uses: ``gh api`` returns the
    endpoint's full JSON shape and this needs several nested fields
    (``steps[].name``, ``steps[].conclusion``, ``runner_name``) that aren't
    worth hand-picking.

    Raises ``RuntimeError``/``ValueError`` on any read failure — auth,
    rate-limit, malformed response, or the run id simply not existing
    (rerun raced a retention window). Callers on the classification path
    (:mod:`coord.ci_store`'s false-negative bias) must catch and treat a
    raised error the same as "no job data" — never as evidence either way.
    """
    data = _gh_json("api", f"repos/{repo}/actions/runs/{run_id}/jobs", default=None,
        caller="github_ops.get_run_jobs")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise RuntimeError(
            f"gh api repos/{repo}/actions/runs/{run_id}/jobs: malformed response"
        )
    return data["jobs"]


def get_job_log(repo: str, job_id: str) -> str:
    """Return the plain-text log for Actions job *job_id* on *repo* (#3114).

    The single ``gh`` sink for :func:`coord.ci_github.build_ci_failure_detail`
    — the call that backs a ci-fix briefing's log excerpt. ``gh api
    repos/{repo}/actions/jobs/{id}/logs`` redirects to a blob-storage URL
    carrying the job's raw text log, not JSON; ``gh api`` follows the
    redirect and prints the body verbatim, so this goes through :func:`_gh`
    directly rather than :func:`_gh_json` — there is nothing to JSON-decode
    here, unlike every other read in this module.

    Raises the same as :func:`_gh` on any failure — missing/timed-out/
    rate-limited ``gh``, or a non-zero exit (e.g. the run's log retention
    window has expired). The sole caller,
    :func:`coord.ci_github.build_ci_failure_detail`, treats any raise as "no
    log available" and degrades to the summary-only briefing — this is
    never called from the polling path, only once at CI-fix dispatch time.

    No server-side tail: this downloads the job's ENTIRE log before
    :func:`coord.ci_github._bound_log_excerpt` truncates it client-side. A
    GitHub Actions job log can run to many MB, and this call shares
    :func:`_gh`'s single subprocess timeout with every other ``gh``
    invocation — so a very verbose job's log can time out this fetch
    outright (fails soft to no detail, same as any other raise here, but
    means the noisiest/most-verbose failures are the ones likeliest to get
    no excerpt at all).
    """
    return _gh(
        "api", f"repos/{repo}/actions/jobs/{job_id}/logs",
        caller="github_ops.get_job_log",
    )


def rerun_workflow_run(repo: str, run_id: str) -> bool:
    """Re-run Actions workflow run *run_id* on *repo* via ``gh run rerun``.

    The single ``gh`` sink (#1483) for :meth:`coord.ci_github.GitHubCi.
    rerun_for_pr` (#1851) — the same seam :func:`get_pr_checks` is for
    reading checks. Returns ``True`` only on a clean (exit 0) rerun; any
    subprocess failure (missing ``gh``, timeout, non-zero exit — e.g. the run
    is already in progress, or the id is stale/invalid) returns ``False``
    rather than raising, matching this module's other best-effort mutators
    (:func:`merge_pr`, :func:`edit_pr_body`).

    Shells out directly rather than through :func:`_gh` because it needs
    "any failure -> False" rather than a raise — but still records the same
    #1896 forge-availability observation :func:`_gh` would have.
    """
    args = ["run", "rerun", str(run_id), "--repo", repo]
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_gh_call(tuple(args), outcome="unreachable",
                        duration_s=time.monotonic() - _t0, detail=str(exc),
                        caller="github_ops.rerun_workflow_run")
        return False
    duration = time.monotonic() - _t0
    if result.returncode != 0:
        stderr = result.stderr.strip()
        record_gh_call(tuple(args), outcome=_classify_gh_exit(stderr),
                        duration_s=duration, detail=stderr,
                        caller="github_ops.rerun_workflow_run")
        return False
    record_gh_call(tuple(args), outcome="ok", duration_s=duration,
                    caller="github_ops.rerun_workflow_run")
    return True


def rerun_workflow_run_failed(repo: str, run_id: str) -> bool:
    """Re-run only the FAILING job(s) of Actions workflow run *run_id* on
    *repo* via ``gh run rerun <id> --failed`` (#2252).

    The single ``gh`` sink for :meth:`coord.ci_github.GitHubCi.
    rerun_failed_for_pr` — the narrower sibling of
    :func:`rerun_workflow_run` (#1851/#1892), which restarts the WHOLE run
    including jobs that already reported green. ``--failed`` is exactly
    #2252's own scoping ask: cheaper, and it keeps the first pass's green
    evidence intact instead of discarding it for no reason.

    Returns ``True`` only on a clean (exit 0) rerun; any subprocess failure
    (missing ``gh``, timeout, non-zero exit — e.g. the run is already in
    progress, or the id is stale/invalid) returns ``False`` rather than
    raising, matching :func:`rerun_workflow_run`'s own best-effort contract.

    Shells out directly rather than through :func:`_gh` for the same reason
    as :func:`rerun_workflow_run` — see that docstring — but still records
    the same #1896 forge-availability observation.
    """
    args = ["run", "rerun", str(run_id), "--repo", repo, "--failed"]
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_gh_call(tuple(args), outcome="unreachable",
                        duration_s=time.monotonic() - _t0, detail=str(exc),
                        caller="github_ops.rerun_workflow_run_failed")
        return False
    duration = time.monotonic() - _t0
    if result.returncode != 0:
        stderr = result.stderr.strip()
        record_gh_call(tuple(args), outcome=_classify_gh_exit(stderr),
                        duration_s=duration, detail=stderr,
                        caller="github_ops.rerun_workflow_run_failed")
        return False
    record_gh_call(tuple(args), outcome="ok", duration_s=duration,
                    caller="github_ops.rerun_workflow_run_failed")
    return True


def diff_file_paths(diff_text: str) -> list[str]:
    """Return every file path touched by *diff_text*, deduped, order-preserving.

    Scans unified-diff file-header lines (``diff --git a/X b/Y``, ``---
    a/X``, ``+++ b/X``) — cheap, dependency-free (#944 sealing v1) ahead of a
    real diff parser. Lives here (rather than :mod:`coord.review`, its
    original home) because :func:`truncate_diff_text` needs it too (#2819)
    and ``coord.review`` imports :mod:`coord.github_ops`, not the reverse —
    :mod:`coord.review` re-uses this copy for its own sealed-path /
    missing-test-coverage checks instead of keeping a second one.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for line in diff_text.splitlines():
        candidates: list[str] = []
        if line.startswith("diff --git "):
            for part in line.split()[1:]:
                if part.startswith("a/") or part.startswith("b/"):
                    candidates.append(part[2:])
        elif line.startswith("--- a/"):
            candidates.append(line[len("--- a/"):])
        elif line.startswith("+++ b/"):
            candidates.append(line[len("+++ b/"):])
        for c in candidates:
            if c not in seen:
                seen.add(c)
                paths.append(c)
    return paths


_DIFF_FILE_BOUNDARY_RE = re.compile(r"^diff --git ", re.MULTILINE)
_RENAME_FROM_RE = re.compile(r"^rename from (.+)$", re.MULTILINE)
_RENAME_TO_RE = re.compile(r"^rename to (.+)$", re.MULTILINE)


def diff_pure_renames(diff_text: str) -> list[tuple[str, str]]:
    """Every ``(old_path, new_path)`` pair *diff_text* renames with NO
    content change — git's own ``similarity index 100%`` marker, not a
    caller's claim (#2896 review).

    A sealed-oracle relocation (``git mv`` of a byte-identical file) is
    textually indistinguishable from "delete old + add new" to a reader
    that only looks at :func:`diff_file_paths`'s flat a/b-side list — both
    the old and new paths show up as "touched" with no signal that they're
    the same content moving, not two independent edits. This recovers the
    narrower fact ``coord.review``'s sealed-tamper carve-out needs: a rename
    block only counts here when git *itself* reports the two sides as
    identical (``rename from``/``rename to`` lines plus ``similarity index
    100%`` — anything less than 100% means content changed too, and must
    keep tripping the tamper check same as any other edit).

    A ``diff --git a/X b/X`` block for an in-place edit (no move at all)
    has no ``rename from``/``rename to`` lines regardless of how similar
    old and new content are, so it's correctly excluded even when the path
    happens to be unchanged either side.
    """
    renames: list[tuple[str, str]] = []
    for block in _DIFF_FILE_BOUNDARY_RE.split(diff_text)[1:]:
        if "similarity index 100%" not in block:
            continue
        from_match = _RENAME_FROM_RE.search(block)
        to_match = _RENAME_TO_RE.search(block)
        if from_match and to_match:
            renames.append((from_match.group(1).strip(), to_match.group(1).strip()))
    return renames


def truncate_diff_text(diff: str, max_chars: int = 60000) -> str:
    """Truncate *diff* to at most *max_chars*, cutting on a ``diff --git``
    file boundary rather than mid-hunk, and naming the files that got cut.

    Factored out of :func:`pr_diff` (#1475) so callers that need the full,
    untruncated diff for content hashing (``compute_patch_id``) can still
    apply the same display truncation to a *separate* copy shown to a human
    reviewer or embedded in a briefing, without a second ``gh`` fetch.

    #2819: the original version was a blind character slice — handed the
    reviewer a diff that stopped mid-hunk, with no indication anything was
    even cut, on any PR whose diff crossed *max_chars* (61% of a review
    briefing at the flat 60k default). That's a silent coverage hole in a
    gate that's supposed to be adversarial. This version instead:

    1. Cuts at the last ``diff --git`` boundary that fits within
       *max_chars* — every file kept in the output is complete, never a
       partial hunk.
    2. Appends the paths of every file that got dropped, so the reviewer
       knows exactly what it did not see and can inspect those files
       directly (e.g. ``git show``) instead of silently missing them.

    Falls back to the old raw character slice — no boundary preference — when
    no file boundary fits within *max_chars* at all (a single file's own
    diff already exceeds the cap, or *diff* isn't a unified diff to begin
    with). Cutting nothing at that point would just emit an empty string,
    which is worse than the old behavior. #2819 follow-up: that fallback
    slice lands *inside* the first file's own diff (the loop below only
    leaves ``cut_at`` unset when the second file's boundary already exceeds
    *max_chars*, i.e. before any second file exists in the output) — so
    that file's header rides into ``head`` looking complete while its body
    is silently cut mid-hunk. Left unflagged, a reviewer who checks the
    omitted-files list and finds their file *not* on it would reasonably
    (and wrongly) conclude it was shown in full. That file is now always
    called out as truncated, separately from the fully-omitted list.
    """
    if len(diff) <= max_chars:
        return diff

    boundaries = [m.start() for m in _DIFF_FILE_BOUNDARY_RE.finditer(diff)]
    cut_at: int | None = None
    for b in boundaries:
        if b == 0:
            continue  # the first file's own boundary — cutting here keeps nothing
        if b > max_chars:
            break
        cut_at = b

    if cut_at is None:
        head = diff[:max_chars]
        # The cut point falls inside the first file's own diff (see the
        # docstring above) — flag that file as incomplete rather than
        # letting it silently pass as "not omitted, so fully shown".
        all_paths = diff_file_paths(diff)
        incomplete = all_paths[0] if all_paths else None
    else:
        head = diff[:cut_at].rstrip("\n")
        incomplete = None

    dropped = [p for p in diff_file_paths(diff) if p not in diff_file_paths(head)]

    note = f"\n... [diff truncated at {max_chars} chars"
    tallies = []
    if incomplete:
        tallies.append(f"1 file cut off mid-diff ({incomplete})")
    if dropped:
        tallies.append(f"{len(dropped)} file(s) omitted")
    if tallies:
        note += "; " + "; ".join(tallies)
    note += "] ..."
    if incomplete:
        note += (
            f"\nFile truncated mid-diff — the shown portion of `{incomplete}` "
            "is INCOMPLETE, not fully reviewed above; inspect the rest "
            f"directly (e.g. `git show <head-sha> -- {incomplete}`) before "
            "approving."
        )
    if dropped:
        note += (
            "\nFiles omitted by truncation — NOT reviewed above; inspect "
            "these directly (e.g. `git show <head-sha> -- <path>`) before "
            "approving:\n" + "\n".join(f"  - {p}" for p in dropped)
        )
    return head + note


def pr_diff(repo_github: str, pr_number: int, *, max_chars: int | None = 60000) -> str | None:
    """Return the merge-base (three-dot) diff for PR ``pr_number``, or None.

    ``gh pr diff`` is three-dot / merge-base by GitHub semantics, so the output
    is exactly the branch's own changes (#612) — code merged to the base after
    the branch was cut never appears as spurious deletions. Truncated to
    *max_chars* with a trailing note so a huge diff can't blow the briefing
    size — pass ``max_chars=None`` to get the full, untruncated diff (#1475:
    needed for content-hashing via ``compute_patch_id``, which must not hash
    a mutated/truncated string). Best-effort: returns None on any ``gh`` error
    so the caller falls back to the in-briefing three-dot diff instructions.
    """
    try:
        diff = _gh("pr", "diff", str(pr_number), "--repo", repo_github,
            caller="github_ops.pr_diff")
    except RuntimeError:
        return None
    if max_chars is None:
        return diff
    return truncate_diff_text(diff, max_chars)


def compute_patch_id(diff_text: str | None) -> str | None:
    """Return the ``git patch-id --stable`` hash of *diff_text*, or ``None``.

    #1475: a content-addressed fingerprint of a diff — insensitive to commit
    SHA / line numbers, but sensitive to the surrounding context lines. A
    pure rebase with no conflict replays the identical diff against a new
    base and produces the same patch-id even though the commit SHA changed;
    a conflict resolution or genuine content change produces a different one
    (that distinction is #1476's job, not this function's).

    ``git patch-id`` operates purely on the piped diff text — no working
    directory or repo checkout required, so this is safe to call from any
    process. Returns ``None`` for empty/missing input or on any subprocess
    failure; callers must fail closed (treat a missing patch-id as "cannot
    confirm identical content", never as "identical").
    """
    if not diff_text or not diff_text.strip():
        return None
    try:
        result = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=diff_text, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not line:
        return None
    return line.split()[0]


def get_compare_diff(repo: str, base: str, head: str) -> str | None:
    """Return the raw three-dot (``base...head``) unified diff text, or None.

    *head* may be a branch name or a commit SHA — GitHub's compare API treats
    them identically, so this also works for a historical SHA that is no
    longer any branch's tip (e.g. the HEAD a review approved before a
    conflict-fix rebase moved the branch on, #1476). Factored out of
    :func:`get_branch_patch_id` so a scoped re-review can fetch the diff for
    an old SHA as well as the current branch tip, without duplicating the
    ``gh api compare`` call shape. Returns ``None`` on any ``gh`` failure —
    callers must fail closed (missing diff ⇒ cannot confirm anything about
    its content).
    """
    try:
        return _gh(
            "api", f"repos/{repo}/compare/{base}...{head}",
            "-H", "Accept: application/vnd.github.v3.diff", caller="github_ops.get_compare_diff")
    except RuntimeError:
        return None


def get_compare_files(repo: str, base: str, head: str) -> list[str] | None:
    """Return the list of file paths changed in the three-dot ``base...head``
    compare, or ``None`` on any ``gh`` failure.

    #1720: the dispatch-time file-overlap fence needs *which files*, not the
    diff content — asking the compare API for ``.files[].filename`` directly
    is cheaper and simpler than fetching :func:`get_compare_diff`'s full
    unified-diff text and parsing ``diff --git a/... b/...`` headers out of
    it. Uses the GitHub API (not a local checkout) so it works from any host
    with `gh` on PATH, matching this module's existing checkout-independent
    diff helpers (:func:`pr_diff`, :func:`get_compare_diff`) rather than the
    coordinator assuming it has a local clone of every dispatched repo.

    ``--jq`` on a leaf-string selector (``.filename``, unlike the object
    selector in :func:`get_repo_milestones`) emits *raw* text, one path per
    line, not JSON-quoted — so this reads lines directly rather than
    JSON-decoding them. Returns ``None`` (not ``[]``) on failure so callers
    can distinguish "no files changed" from "couldn't ask" and fail open
    accordingly.
    """
    try:
        raw = _gh(
            "api", f"repos/{repo}/compare/{base}...{head}",
            "--jq", ".files[].filename", caller="github_ops.get_compare_files")
    except RuntimeError:
        return None
    return [line for line in (ln.strip() for ln in raw.splitlines()) if line]


def _gh_ref_confirmed_missing(exc: Exception) -> bool:
    """True when *exc* (raised by ``_gh`` for a ``gh api`` ref/compare call)
    is GitHub positively saying "no such ref" — a 404 — as opposed to an
    auth, rate-limit, network, or other transient failure that merely looks
    like one from the caller's point of view.

    #2324: deliberately narrower than the "http 4" match
    :func:`branch_exists_on_remote` uses (which folds 401/403/429 in with
    404, and fails open elsewhere for that function's own reasons). This is
    the one call site where conflating "confirmed gone" with "auth/rate-limit
    blip" would make `coord retry`'s zero-commit gate treat a locked-out
    ``gh`` as proof a branch was deleted and race ahead re-dispatching real,
    unconfirmed work — so it checks :func:`_is_transient_error`'s keywords
    first and only then looks for an explicit "404"/"not found" signal.
    """
    if _is_transient_error(exc):
        return False
    msg = str(exc).lower()
    return "404" in msg or "not found" in msg


def _head_branch_confirmed_deleted(repo: str, base: str, branch: str) -> bool:
    """True when *branch* 404s on *repo* while *base* still resolves.

    #2324: :func:`branch_commits_ahead`'s three-dot compare call 404s
    identically whether it's the head branch that's gone, the base branch,
    or something else entirely obscures both — the compare error text alone
    can't tell them apart. This asks each ref directly (the same
    ``git/refs/heads`` lookup :func:`branch_exists_on_remote` uses) so a
    branch that was genuinely deleted — the normal cleanup after a
    zero-commit worker exit — reads as "confirmed gone" instead of being
    lumped in with an unrelated failure and reported as unknown.

    Only returns True on the one shape that's actually conclusive: the head
    ref 404s (via :func:`_gh_ref_confirmed_missing`) *and* the base ref
    still resolves. Any other combination — head resolves fine, head lookup
    itself fails inconclusively, or base can't be confirmed either — returns
    False so the caller keeps its existing fail-closed ``None``.
    """
    try:
        _gh("api", f"repos/{repo}/git/refs/heads/{branch}",
            caller="github_ops._head_branch_confirmed_deleted")
        return False  # head resolves fine; compare failed for some other reason
    except RuntimeError as exc:
        if not _gh_ref_confirmed_missing(exc):
            return False  # inconclusive — do not guess
    try:
        _gh("api", f"repos/{repo}/git/refs/heads/{base}",
            caller="github_ops._head_branch_confirmed_deleted")
    except RuntimeError:
        return False  # base itself unconfirmable — don't trust the read
    return True  # head confirmed gone, base confirmed present


def branch_commits_ahead(repo: str, base: str, branch: str) -> int | None:
    """Commits *branch* is ahead of *base* on the remote, or ``None``.

    #1534: the coordinator usually has no local checkout of a worker's branch,
    so the zero-commit question has to be asked of GitHub.  Uses the same
    three-dot compare API as :func:`get_branch_diff_size` and reads
    ``ahead_by`` — no PR required.

    Returns ``None`` (never 0) on any ``gh`` failure, an unparseable payload,
    or a missing ``ahead_by`` field.  Callers MUST treat ``None`` as "unknown,
    assume non-zero": this exists to *refuse* work on a provably empty branch,
    and a network blip must not silently become a refusal — that would strand
    real reviews.  This is the opposite polarity from
    :func:`branch_is_fully_merged`, which returns a plain ``False`` on error
    because *its* fail-safe direction is "keep the PR open".

    #2324 exception: when the compare call fails, a *confirmed* 404 on the
    head branch while the base branch still resolves
    (:func:`_head_branch_confirmed_deleted`) returns ``0``, not ``None`` — a
    head ref that GitHub positively says doesn't exist is the strongest
    possible evidence nothing was ever pushed to it (a branch carrying
    commits would still be there). Every other failure shape — the compare
    call fails for a reason that isn't a confirmed head-branch 404, or the
    follow-up ref checks themselves are inconclusive — keeps the existing
    fail-closed ``None``.
    """
    if not branch or not base:
        return None
    if branch == base:
        return 0
    try:
        raw = _gh("api", f"repos/{repo}/compare/{base}...{branch}",
            caller="github_ops.branch_commits_ahead")
        cmp = json.loads(raw)
    except Exception:  # noqa: BLE001 — unknown, not zero
        if _head_branch_confirmed_deleted(repo, base, branch):
            return 0
        return None
    if not isinstance(cmp, dict):
        return None
    ahead = cmp.get("ahead_by")
    if not isinstance(ahead, int) or isinstance(ahead, bool):
        return None
    return ahead


def branch_commits_ahead_for_assignment(assignment: Any, config: Any) -> int | None:
    """:func:`branch_commits_ahead` for a board *assignment*, or ``None``.

    #1606: `coord retry`'s advisory zero-commit gate
    (``coord/commands/dispatch.py``'s ``retry()``) and `coord diagnose
    --stage work`'s ADVISORY-row recovery (``coord/diagnose.py``'s
    ``_work_advisory_commits_ahead``) both ask GitHub this exact question —
    they used to do it via two independently-written inline copies of
    "branch empty -> 0, repo missing -> None, else ask GitHub", which even
    diverged in how they looked up the repo config (``cfg.repo(name)`` vs.
    a hand-rolled scan over ``config.repos`` — equivalent, but two copies is
    how they drift apart later). This is the one copy both now call.

    *assignment* needs only ``.branch`` and ``.repo_name``; *config* needs
    only ``.repo(name)`` returning an object with ``.github`` /
    ``.default_branch`` (both ``coord.models.Assignment`` and
    ``coord.config.Config`` satisfy this — left untyped here to avoid
    github_ops.py importing either module).

    An assignment with no branch (or a blank one) is treated as 0 commits
    ahead without ever calling GitHub — there is nothing to compare. A repo
    that ``config`` doesn't know about returns ``None`` ("cannot confirm"),
    never a bare 0, matching :func:`branch_commits_ahead`'s own fail-closed
    polarity: an unconfirmable commit count must never be silently read as
    "empty branch, safe to touch" — *except* the one case
    :func:`branch_commits_ahead` itself carves out (#2324): a recorded
    branch whose head ref GitHub positively 404s, with the base branch still
    resolving, comes back as 0 rather than ``None`` — a deleted branch
    proves nothing was pushed at least as conclusively as a blank one does.
    """
    branch = (getattr(assignment, "branch", None) or "").strip()
    if not branch:
        return 0
    repo_cfg = config.repo(assignment.repo_name)
    if repo_cfg is None:
        return None
    base = repo_cfg.default_branch or "main"
    return branch_commits_ahead(repo_cfg.github, base, branch)


def get_branch_patch_id(repo: str, base: str, branch: str) -> str | None:
    """Return the content-addressed patch-id for *branch*'s diff against *base*.

    #1475: uses the GitHub three-dot compare API (no PR required, mirroring
    :func:`get_branch_diff_size`) to fetch the raw unified diff, then hashes
    it with :func:`compute_patch_id`. Returns ``None`` on any failure — the
    merge-queue gate treats a missing patch-id as "cannot confirm identical
    content" and falls back to the pre-#1475 SHA-only staleness check
    (fail closed).
    """
    return compute_patch_id(get_compare_diff(repo, base, branch))


def create_pr(
    repo: str,
    *,
    base: str,
    head: str,
    title: str,
    body: str,
) -> dict:
    """Open a PR. Returns {number, url}. If one already exists for `head`, returns it."""
    existing = find_pr_for_branch(repo, head)
    if existing is not None:
        return {"number": existing["number"], "url": existing["url"], "existed": True}
    url = _gh(
        "pr", "create", "--repo", repo,
        "--base", base, "--head", head,
        "--title", title, "--body", body, caller="github_ops.create_pr")
    # gh pr create returns the URL on the last line of stdout.
    pr_url = url.strip().splitlines()[-1] if url.strip() else ""
    number = int(pr_url.rsplit("/", 1)[-1]) if pr_url else 0
    return {"number": number, "url": pr_url, "existed": False}


def get_pr_size(repo: str, number: int) -> int:
    """Return additions+deletions for sequencing. 0 on lookup failure."""
    try:
        raw = _gh(
            "pr", "view", str(number), "--repo", repo,
            "--json", "additions,deletions", caller="github_ops.get_pr_size")
    except RuntimeError:
        return 0
    data = _json_loads_or(raw, default={})
    return int(data.get("additions", 0)) + int(data.get("deletions", 0))


def get_branch_diff_size(repo: str, base: str, branch: str) -> int:
    """Return total diff size (additions+deletions) for *branch* relative to *base*.

    Uses the GitHub three-dot compare API — no PR required.  Sums
    ``additions + deletions`` across all changed files.  Returns ``0`` on any
    failure so callers can treat size as unknown-but-not-blocking.

    Prefer this over :func:`get_pr_size` at enqueue time so size is populated
    before a PR is opened and the ordering shown to the user matches the
    ordering used at merge time (#776 size unification).
    """
    try:
        raw = _gh("api", f"repos/{repo}/compare/{base}...{branch}",
            caller="github_ops.get_branch_diff_size")
        data = json.loads(raw)
        return sum(
            int(f.get("additions", 0)) + int(f.get("deletions", 0))
            for f in data.get("files", [])
        )
    except Exception:  # noqa: BLE001 — fail-open: unknown size is not blocking
        return 0


def merge_pr(
    repo: str, number: int, method: str = "rebase", *, delete_branch: bool = False
) -> tuple[bool, str]:
    """Merge a PR. Returns (success, message).

    Conflict / not-rebaseable cases come back as (False, <gh stderr>). Caller
    decides whether to retry or surface to the user — we never resolve conflicts
    here.

    ``delete_branch`` (#2790) defaults to ``False`` — the merge queue's own
    callers never pass it, preserving their pre-existing ``--delete-branch=
    false`` behavior unchanged; ``coord pr merge`` is the one caller that
    opts in via its own ``--delete-branch`` flag.
    """
    flag = {"rebase": "--rebase", "squash": "--squash", "merge": "--merge"}.get(method, "--rebase")
    delete_flag = f"--delete-branch={'true' if delete_branch else 'false'}"
    try:
        out = _gh("pr", "merge", str(number), "--repo", repo, flag, delete_flag,
            caller="github_ops.merge_pr")
    except RuntimeError as e:
        return False, str(e)
    return True, out


def list_open_prs(repo: str) -> list[dict]:
    return _gh_json(
        "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,headRefName",
        default=[], caller="github_ops.list_open_prs")


def get_recent_develop_commits(repo: str, count: int = 10) -> list[dict]:
    commits = _gh_json(
        "api", f"repos/{repo}/commits?sha=develop&per_page={count}",
        default=[], caller="github_ops.get_recent_develop_commits")
    return [
        {"sha": c["sha"][:7], "message": c["commit"]["message"].split("\n")[0]}
        for c in commits
    ]


def create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
    milestone: str | None = None,
) -> dict:
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    if labels:
        for label in labels:
            args.extend(["--label", label])
    if milestone:
        args.extend(["--milestone", milestone])
    raw = _gh(*args, caller="github_ops.create_issue")
    url = raw.strip()
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    return {"number": number, "url": url}


def update_issue_body(repo: str, issue_number: int, body: str) -> None:
    _gh(
        "api", "-X", "PATCH",
        f"repos/{repo}/issues/{issue_number}",
        "-f", f"body={body}", caller="github_ops.update_issue_body")


def get_repo_milestones(repo: str, *, state: str = "open") -> list[dict]:
    """Return milestones for *repo* (open ones by default).

    Each item has at least ``number`` and ``title`` keys, matching the
    shape returned by the GitHub milestones REST endpoint. Used to resolve a
    milestone title → number (``coord milestone assign``) without a separate
    call.
    """
    raw = _gh(
        "api", "--paginate",
        f"repos/{repo}/milestones?state={state}",
        "--jq", ".[] | {number: .number, title: .title}", caller="github_ops.get_repo_milestones")
    # --jq emits one JSON object per line when applied to an array.  #1353:
    # a single malformed line used to bare-json.loads() into an unattributable
    # crash that discarded every other (well-formed) milestone line too — skip
    # just the bad line instead.
    results = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = _json_loads_or(line, default=None)
        if parsed is not None:
            results.append(parsed)
    return results


#: The ``--jq`` projection :func:`get_repo_milestones_with_counts` sends.
#:
#: A named module constant rather than an inline literal so the #967
#: regression guard (an invalid filter — ``.[].{...}`` with no pipe — that
#: made the whole call fail end to end, and which no mocked-``subprocess``
#: test could catch) can run THIS EXACT string through a real jq engine.
#: :func:`get_repo_milestones`'s own filter is pinned the same way, by
#: source-regex, in ``tests/test_cli_milestone_assign.py``; this one is
#: split across several source lines, so it needs a name to be reachable.
MILESTONE_COUNTS_JQ = (
    ".[] | {number: .number, title: .title, state: .state, "
    "open_issues: .open_issues, closed_issues: .closed_issues, "
    "description: .description}"
)


def get_repo_milestones_with_counts(repo: str, *, state: str = "open") -> list[dict]:
    """Milestones for *repo*, carrying GitHub's own open/closed issue counts.

    Same REST listing (and the same one paginated ``gh api`` call) as
    :func:`get_repo_milestones`, projecting the four extra fields GitHub
    already computes for every milestone: ``state``, ``open_issues``,
    ``closed_issues`` and ``description``. Backs the dashboard's
    ``GET /api/milestones`` roster (#3072), where "how many of this
    milestone's issues are closed" is a headline column — and where deriving
    it locally would mean either a second ``--state all`` issue fetch per
    milestone or a count that quietly disagrees with what GitHub's own
    milestone page shows.

    A deliberate *sibling* of :func:`get_repo_milestones` rather than a
    widening of it: that function is the identity-only lookup every
    title→number resolution path uses (``coord milestone assign``, ``coord
    plans``), and every one of those callers would otherwise start paying
    for fields it discards. The returned dicts are a strict superset of
    ``get_repo_milestones``'s, so anything that accepts one accepts the
    other — :func:`coord.plans.aggregate_repo_plans` takes this list
    directly.

    Returns ``[]`` for a repo with no milestones (not an error).
    """
    raw = _gh(
        "api", "--paginate",
        f"repos/{repo}/milestones?state={state}",
        "--jq", MILESTONE_COUNTS_JQ,
        caller="github_ops.get_repo_milestones_with_counts")
    # One compact JSON object per line, same as get_repo_milestones — and the
    # same #1353 rule: skip a single malformed line rather than letting it
    # discard every well-formed milestone alongside it.
    results = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = _json_loads_or(line, default=None)
        if parsed is not None:
            results.append(parsed)
    return results


def get_milestone(repo: str, milestone_number: int) -> dict:
    """Fetch a single milestone by number; returns ``{number, title, ...}``.

    Used to resolve a milestone number → title so the local issues cache
    ``milestone_title`` column can be populated without listing all milestones.
    Raises RuntimeError (propagated from ``_gh``) when the milestone does not
    exist.
    """
    return _gh_json("api", f"repos/{repo}/milestones/{milestone_number}", default={},
        caller="github_ops.get_milestone")


def search_issues(
    repo: str,
    *,
    state: str = "open",
    search: str | None = None,
    milestone: str | None = None,
    label: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """General-purpose issue listing/search (#2484).

    The read-side counterpart to `create_issue` — wraps ``gh issue list``
    with the filters an interactive/coordinator session actually reaches for
    (``--search``, ``--milestone``, ``--label``) behind one function, rather
    than requiring a bespoke wrapper per filter combination the way
    ``get_open_issues``/``get_closed_epics``/``get_milestone_issues`` do.
    Backs ``coord issue list`` so a plain issue search never needs to fall
    back to raw ``gh issue list --search``.
    """
    args = [
        "issue", "list", "--repo", repo, "--state", state,
        "--json", "number,title,state,labels,milestone,assignees",
        "--limit", str(limit),
    ]
    if search:
        args.extend(["--search", search])
    if milestone:
        args.extend(["--milestone", milestone])
    if label:
        args.extend(["--label", label])
    return _gh_json(*args, default=[], caller="github_ops.search_issues")


def get_milestone_issues(
    repo: str, milestone_title: str, *, state: str = "all"
) -> list[dict]:
    """Return every issue under *milestone_title* in *repo* (open+closed by default).

    Each item has ``number``, ``title``, ``state`` ("OPEN"/"CLOSED"), and
    ``labels`` (list of ``{"name": ...}``). ``gh issue list --milestone`` takes
    the milestone TITLE, not its number (unlike most other milestone-related
    calls in this module) — matches the existing ``--milestone`` usage in
    :func:`create_issue`. Used by ``--audit-of`` (#885) to enumerate a
    milestone's issue states for the audit briefing without a separate call
    per issue.
    """
    return _gh_json(
        "issue", "list", "--repo", repo, "--milestone", milestone_title,
        "--state", state, "--json", "number,title,state,labels",
        "--limit", "200",
        default=[], caller="github_ops.get_milestone_issues")


def assign_issue_milestone(
    repo: str, issue_number: int, milestone_number: int
) -> None:
    """Assign *milestone_number* to *issue_number* on *repo* via the GitHub API.

    Uses ``gh api -X PATCH`` with ``-F milestone=<int>`` (capital -F so the
    value is sent as a JSON integer, as GitHub's REST API requires). Raises
    RuntimeError on any ``gh`` failure.
    """
    _gh(
        "api", "-X", "PATCH",
        f"repos/{repo}/issues/{issue_number}",
        "-F", f"milestone={milestone_number}", caller="github_ops.assign_issue_milestone")


def unassign_issue_milestone(repo: str, issue_number: int) -> None:
    """Clear *issue_number*'s milestone on *repo* via the GitHub API (#1003).

    The counterpart to :func:`assign_issue_milestone` — ``-F milestone=null``
    sends a JSON ``null`` (per ``gh api``'s typed-field convention: literal
    ``null``/``true``/``false``/numbers are sent as their JSON type, not a
    string), which GitHub's REST API treats as "remove the milestone".
    Idempotent — clearing an issue that has no milestone is a no-op on
    GitHub's side. Raises RuntimeError on any ``gh`` failure.
    """
    _gh(
        "api", "-X", "PATCH",
        f"repos/{repo}/issues/{issue_number}",
        "-F", "milestone=null", caller="github_ops.unassign_issue_milestone")


def close_pr(repo: str, number: int, *, comment: str | None = None) -> None:
    """Close an open PR, optionally posting a comment first.

    Posts *comment* (if given) via ``gh issue comment`` — PRs share the GitHub
    issue comment stream — then closes the PR via ``gh pr close``.  Raises
    RuntimeError on ``gh`` failure.
    """
    if comment:
        post_issue_comment(repo, number, comment)
    _gh("pr", "close", str(number), "--repo", repo, caller="github_ops.close_pr")


def branch_is_fully_merged(
    repo: str,
    branch: str,
    default_branch: str = "main",
) -> bool:
    """Return True when *branch* has 0 commits ahead of *default_branch*.

    Uses the GitHub three-dot compare API.  Returns False on any error —
    fail-safe so we never accidentally close a live PR.

    Note: only detects **fast-forward** merges.  After a squash or rebase
    merge the branch's original commits remain "ahead" (different SHAs) even
    though the work has landed.  The ``issue_is_closed`` check is the primary
    stale-PR signal for those cases.
    """
    if not branch or not default_branch or branch == default_branch:
        return False
    try:
        raw = _gh("api", f"repos/{repo}/compare/{default_branch}...{branch}",
            caller="github_ops.branch_is_fully_merged")
        cmp = json.loads(raw)
        return isinstance(cmp, dict) and cmp.get("ahead_by") == 0
    except Exception:  # noqa: BLE001 — fail-safe: keep the PR open on any error
        return False


def post_pr_review(repo: str, number: int, verdict: str, body: str) -> None:
    """Post a PR review via the gh CLI.

    *verdict* must be ``"approve"`` or ``"request-changes"``.  Any other value
    raises :class:`ValueError` before invoking gh.
    """
    if verdict == "approve":
        flag = "--approve"
    elif verdict == "request-changes":
        flag = "--request-changes"
    else:
        raise ValueError(f"Invalid review verdict: {verdict!r} (must be 'approve' or 'request-changes')")
    _gh("pr", "review", str(number), "--repo", repo, flag, "--body", body,
        caller="github_ops.post_pr_review")


# ── DR credential probes (#3129, rung D3 of epic #3117) ─────────────────────
#
# `coord dr promote` asks GitHub two read-only capability questions on a
# standby host: "may this token merge here?" and "may it read issues?". Those
# two argv constructions live HERE, not in coord/dr_promote.py, because this
# module is the repo's single `gh` chokepoint (#1902/#2135) — the invariant
# `scripts/check_gh_argv_containment.py` enforces, and the reason the eventual
# forge port is a refactor rather than a rewrite. A future non-GitHub forge
# reimplements these two functions and `dr promote` follows for free.
#
# What does NOT move here is the *execution* policy. These take the caller's
# runner (`coord.dr_promote._run`) rather than calling `_gh` themselves, for
# three reasons specific to the DR path:
#
#   * `_gh` raises on a non-zero exit; the promote checks need the exit code
#     and the output, because "the token cannot push to repo 4 of 5" is a
#     *verdict to render*, not an exception to swallow.
#   * `_gh` consults and feeds `coord.github_throttle`'s shared backoff and
#     records `forge_availability` telemetry — both of which write to the very
#     store this command is in the middle of restoring, on a host whose board
#     is not up yet.
#   * `dr_promote._run` scrubs credentials out of captured output and maps a
#     missing binary to exit 127 rather than an exception; the probes must
#     keep exactly those semantics.
#
# So: the seam owns the argv, the caller owns how it is run.

#: What the two probes below expect of their *run* argument: take an argv,
#: return ``(returncode, combined stdout+stderr)``, and do not raise for the
#: ordinary failures. :func:`coord.dr_promote._run` is the implementation.
GhProbeRunner = Callable[[Sequence[str]], tuple[int, str]]


def probe_repo_push_permission(slug: str, *, run: GhProbeRunner) -> tuple[int, str]:
    """Ask GitHub whether this token may **push to** (and so merge on) *slug*.

    Returns the runner's ``(returncode, output)`` verbatim. On success the
    output is GitHub's own ``.permissions.push`` answer for the authenticated
    identity — the literal string ``true`` or ``false``, possibly with
    surrounding whitespace; the caller parses and grades it. Anything else
    (a non-zero exit, an unparseable body) is the caller's *unknown*, never a
    silent pass: see :func:`coord.dr_promote.check_github_credential`.

    Per repo on purpose — a token can be fine on four repos and useless on the
    fifth, and a merge-capability check that averages that is not a check.
    """
    return run(["gh", "api", f"repos/{slug}", "--jq", ".permissions.push"])


def probe_issues_readable(slug: str, *, run: GhProbeRunner) -> tuple[int, str]:
    """Ask GitHub whether this token may **read issues** on *slug*.

    Separate from :func:`probe_repo_push_permission` because issue access is
    separately grantable — and separately revocable — from repo metadata, so
    a token that answers ``push: true`` can still be unable to read the issue
    bodies the coordinator's whole message bus is made of.

    Requests a single issue (``per_page=1``): the smallest response that still
    proves the endpoint answered for this identity.
    """
    return run(["gh", "api", f"repos/{slug}/issues?per_page=1"])
