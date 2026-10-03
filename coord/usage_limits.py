"""Max-plan 5h/weekly usage-window probe and dispatch gate (#1466).

Unattended driving (``coord drive``, ``coord approve``) previously had no
idea how much of the account's Max-plan session/weekly usage was left, so it
would happily dispatch straight into a 5-hour or weekly wall — the first
sign was a worker dying mid-task with the branch stranded (see
``coord.worker_events.detect_usage_limit_kill`` for the *mid-flight* half of
that signal).  This module is the *pre-flight* half: probe the plan bars
before dispatching, so ``coord drive``'s ``preflight()`` and ``coord
approve`` can warn, refuse, or (#1649) reroute to a different provider's
rung of the escalation ladder instead of finding out the hard way.

THE PROBE.  ``claude -p "/usage" --output-format json`` returns the same
live plan bars the interactive statusline shows, at essentially no cost
(``total_cost_usd: 0``, ``num_turns: 0``, ~700ms measured).  Its ``.result``
field is prose, not a stable contract, e.g.::

    Current session: 57% used · resets Jul 27, 1:30am (America/Chicago)
    Current week (all models): 29% used · resets Aug 1, 12pm (America/Chicago)
    Current week (Fable): 0% used

:func:`parse_usage_probe_output` parses that defensively — a garbled or
reworded string yields ``PlanLimits(status="unknown")``, never a raised
exception, and never something that looks like an "ok" read of the wrong
numbers.

SCOPE OF THE PROBE.  The bars are **server-side and account-wide** — one
probe on the daemon host covers every agent machine sharing that login. (The
"what's contributing" breakdown underneath ``/usage``'s prose is
local-machine-only and is not parsed here — it answers a different
question than "how much is left".) The endpoint is itself rate-limited, and
Claude Code then serves the bars up to 60 minutes stale, so callers MUST NOT
poll it per-dispatch — :func:`get_plan_limits` caches for
:data:`CACHE_TTL_SECS`.

AUTH SCOPE.  This only means anything under subscription OAuth. Under an API
key, Bedrock, or Vertex the plan windows do not apply and ``/usage`` returns
nothing useful — :func:`parse_usage_probe_output` returns ``status="unknown"``
for that prose too (it doesn't match the session/week bars), so callers
degrade to skipping the gate automatically, with no separate auth check
needed here.

CAVEAT (leave this comment wherever the gate is wired in — the issue asked
for it explicitly).  Anthropic announced that ``claude -p`` / Agent SDK usage
moves *off* the subscription windows onto a separate monthly credit pool
($100 Max 5x / $200 Max 20x). That rollout is **paused as of 2026-06-15** —
the help article currently states nothing has changed, so today's headless
workers still draw the same pool ``/usage`` reports and this gate is
predictive of a worker running into it. If that rollout resumes, the 5h/
weekly bars stop predicting worker blocks and the gate would need to track
credit balance instead.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

# Reuse the #859 absolute-path resolution (systemd --user strips PATH) rather
# than re-deriving it — see coord.test_orchestrator.resolve_claude_bin's
# docstring for why a bare "claude" silently breaks under coord-serve.
from coord.test_orchestrator import resolve_claude_bin

if TYPE_CHECKING:
    from coord.config import ModelsConfig, UsageGateConfig

# How long a cached probe stays valid. The `/usage` endpoint is itself
# rate-limited and Claude Code serves bars up to 60 minutes stale anyway, so
# there is no benefit to probing more often than this — only more risk of
# tripping that same rate limit ourselves.
CACHE_TTL_SECS = 60.0

# The ONLY provider the `/usage` probe ever measures (module docstring, AUTH
# SCOPE section) — `evaluate_usage_gate` must never reroute/warn/block a
# dispatch whose *own* effective provider isn't this one, no matter how
# close to the wall Claude's own session/week windows are (#1649 review:
# `effective_provider_name` is whatever `resolve_provider_name` already
# resolved for THIS proposal — which can legitimately be a non-"claude"
# provider via `providers.labels`/a repo override, entirely independent of
# this probe's reading). See the guard at the top of `evaluate_usage_gate`.
PROBE_CONSTRAINED_PROVIDER = "claude"


# ── data classes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelWeekUsage:
    """One ``Current week (<model>): NN% used`` row (per-model breakdown)."""

    label: str
    used_pct: float
    resets_at: str | None = None


@dataclass(frozen=True)
class PlanLimits:
    """Parsed ``/usage`` bars, or the reason we don't have them.

    ``status`` is the load-bearing field: ``"ok"`` means *both* percentages
    were parsed from a probe that actually ran; anything else is
    ``"unknown"`` and callers must treat it as "no information", never as
    "usage is fine" — a probe failure silently degrading a gate to "always
    proceed" is fine (that's the intended fail-open behaviour); a probe
    failure being mistaken for "0% used" is not.
    """

    status: str  # "ok" | "unknown"
    session_pct: float | None = None
    session_resets_at: str | None = None
    week_pct: float | None = None
    week_resets_at: str | None = None
    week_by_model: tuple[ModelWeekUsage, ...] = ()
    error: str | None = None
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "session_pct": self.session_pct,
            "session_resets_at": self.session_resets_at,
            "week_pct": self.week_pct,
            "week_resets_at": self.week_resets_at,
            "week_by_model": [
                {"label": m.label, "used_pct": m.used_pct, "resets_at": m.resets_at}
                for m in self.week_by_model
            ],
            "error": self.error,
        }


_UNKNOWN = PlanLimits(status="unknown")


# ── parsing (defensive — `.result` is prose, not a contract) ────────────────

_PCT = r"(\d+(?:\.\d+)?)\s*%\s*used"
_RESETS = r"(?:\s*[·•]\s*resets\s+(.+))?"

_SESSION_RE = re.compile(
    r"Current\s+session:\s*" + _PCT + _RESETS, re.IGNORECASE
)
_WEEK_LINE_RE = re.compile(
    r"Current\s+week\s*\(([^)]*)\):\s*" + _PCT + _RESETS, re.IGNORECASE
)


def parse_usage_probe_output(text: str) -> PlanLimits:
    """Parse ``claude -p "/usage"``'s ``.result`` prose into a
    :class:`PlanLimits`.

    Never raises — a string that doesn't contain a recognisable "Current
    session" or "Current week" bar (wrong CLI version, changed wording,
    empty, garbage) yields ``PlanLimits(status="unknown", ...)`` with the
    original text attached for diagnostics.
    """
    if not isinstance(text, str) or not text.strip():
        return PlanLimits(status="unknown", error="empty /usage output", raw=text or "")

    session_pct: float | None = None
    session_resets: str | None = None
    week_pct: float | None = None
    week_resets: str | None = None
    week_by_model: list[ModelWeekUsage] = []

    m = _SESSION_RE.search(text)
    if m:
        try:
            session_pct = float(m.group(1))
        except ValueError:
            session_pct = None
        resets = m.group(2)
        session_resets = resets.strip() if resets else None

    for m in _WEEK_LINE_RE.finditer(text):
        label = (m.group(1) or "").strip()
        try:
            pct = float(m.group(2))
        except ValueError:
            continue
        resets = m.group(3)
        resets = resets.strip() if resets else None
        if label.lower() == "all models":
            week_pct = pct
            week_resets = resets
        elif label:
            week_by_model.append(ModelWeekUsage(label=label, used_pct=pct, resets_at=resets))

    if session_pct is None and week_pct is None:
        return PlanLimits(
            status="unknown",
            error="no recognisable usage bars in /usage output",
            raw=text,
        )

    return PlanLimits(
        status="ok",
        session_pct=session_pct,
        session_resets_at=session_resets,
        week_pct=week_pct,
        week_resets_at=week_resets,
        week_by_model=tuple(week_by_model),
        raw=text,
    )


# ── the probe (I/O) ──────────────────────────────────────────────────────────


def probe_plan_limits(*, timeout: float = 15.0) -> PlanLimits:
    """Run ``claude -p "/usage" --output-format json`` and parse the result.

    Never raises: a missing binary, a non-OAuth auth mode (API key / Bedrock
    / Vertex — the plan windows simply don't apply there), a timeout, or
    malformed JSON all come back as ``PlanLimits(status="unknown")`` rather
    than propagating, so a probe failure can never fail a dispatch by
    accident. Costs one ``claude -p`` round trip (~700ms, $0, 0 turns,
    verified against the real CLI) — callers that don't want that cost on
    every call should go through :func:`get_plan_limits` instead.
    """
    cmd = [resolve_claude_bin(), "-p", "/usage", "--output-format", "json"]
    try:
        result = subprocess.run(
            cmd,
            input="",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PlanLimits(status="unknown", error=f"{type(exc).__name__}: {exc}")

    if result.returncode != 0:
        return PlanLimits(
            status="unknown",
            error=f"claude -p /usage exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:300]}",
        )

    try:
        outer = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return PlanLimits(status="unknown", error="non-JSON output from claude -p /usage")

    if not isinstance(outer, dict):
        return PlanLimits(status="unknown", error="unexpected JSON shape from claude -p /usage")

    text = outer.get("result")
    if not isinstance(text, str):
        return PlanLimits(status="unknown", error="no .result field in claude -p /usage output")

    return parse_usage_probe_output(text)


@dataclass
class _Cache:
    at: float = 0.0
    limits: PlanLimits = field(default_factory=lambda: _UNKNOWN)


_cache = _Cache()


def get_plan_limits(*, force: bool = False, timeout: float = 15.0) -> PlanLimits:
    """Cached wrapper around :func:`probe_plan_limits` — the entry point
    dispatch gates should use.

    Cached for :data:`CACHE_TTL_SECS`: the endpoint is itself rate-limited
    and Claude Code already serves stale (up to 60m) bars, so a ``coord
    drive`` poll loop or a ``coord approve`` batch of several proposals must
    share one probe rather than shelling out to ``claude`` per call.
    """
    now = time.monotonic()
    if not force and (now - _cache.at) < CACHE_TTL_SECS:
        return _cache.limits
    limits = probe_plan_limits(timeout=timeout)
    _cache.at = now
    _cache.limits = limits
    return limits


def reset_cache() -> None:
    """Test/CLI-only escape hatch — drop the cached probe."""
    _cache.at = 0.0
    _cache.limits = _UNKNOWN


# ── the gate (pure) ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UsageGateResult:
    """What :func:`evaluate_usage_gate` decided, and why.

    ``route`` is set only when ``action == "reroute"`` (#1649): the chosen
    ``models.escalation`` rung — a bare model alias or a ``provider/model``
    pair (see :func:`coord.config.parse_model_route`) — that the caller
    should dispatch on instead of the constrained provider. Every other
    action leaves it ``None``.
    """

    action: str  # "proceed" | "warn" | "block" | "reroute"
    message: str = ""
    route: str | None = None

    @property
    def blocks(self) -> bool:
        return self.action == "block"


def _format_trigger(label: str, pct: float, resets_at: str | None) -> str:
    reset_note = f", resets {resets_at}" if resets_at else ""
    return f"{label} {pct:.0f}% used{reset_note}"


def select_reroute_route(escalation: list[str], constrained_provider: str) -> str | None:
    """The first rung of *escalation* whose provider differs from
    *constrained_provider* (#1649) — the route :func:`evaluate_usage_gate`
    reroutes a ``mode="reroute"`` dispatch to.

    Each rung is resolved via :func:`coord.config.parse_model_route` (same
    ``"/"`` convention as ``models.pinned`` — a bare alias is implicitly on
    ``"claude"``). ``None`` when every rung shares *constrained_provider*,
    which is every deployment today: until #55's cross-provider routes
    land, ``models.escalation`` is entirely bare aliases, so this always
    returns ``None`` and :func:`evaluate_usage_gate` falls back to
    ``reroute_fallback`` — exactly the safety property the issue asked for
    (no rung to escape to is never treated as "dispatch anyway").
    """
    from coord.config import parse_model_route  # noqa: PLC0415 — avoid import cycle at module load

    for rung in escalation:
        provider_name, _ = parse_model_route(rung)
        if provider_name != constrained_provider:
            return rung
    return None


def _reroute_fallback_result(gate_cfg: "UsageGateConfig", message: str) -> UsageGateResult:
    """``mode="reroute"``'s own fallback — never a silent "dispatch
    anyway" (see :attr:`coord.config.UsageGateConfig.reroute_fallback`)."""
    action = "block" if gate_cfg.reroute_fallback == "block" else "warn"
    return UsageGateResult(action, message)


def _evaluate_reroute(
    *,
    message: str,
    gate_cfg: "UsageGateConfig",
    models_cfg: "ModelsConfig | None",
    effective_provider_name: str,
    assignment_type: str | None,
) -> UsageGateResult:
    """``mode="reroute"``'s branch of :func:`evaluate_usage_gate` — split out
    for readability, not reused elsewhere.

    *message* already names what tripped (trigger + reset time); this adds
    the "what it rerouted from and to" half the issue asked be loud about,
    or — when reroute can't apply — exactly why, before falling back to
    :attr:`coord.config.UsageGateConfig.reroute_fallback`.
    """
    if models_cfg is not None and models_cfg.model_for_type(assignment_type) is not None:
        # #1650: a pinned stage (``"review"`` by default) must never be
        # degraded by ANY mechanism, including this one — warn/block per
        # the configured fallback instead of rerouting it.
        return _reroute_fallback_result(
            gate_cfg,
            f"{message} — type={assignment_type!r} is pinned (models.pinned); "
            "usage-gate reroute does not apply to pinned stages "
            f"(usage_gate.reroute_fallback: {gate_cfg.reroute_fallback})",
        )

    escalation = models_cfg.escalation if models_cfg is not None else []
    route = select_reroute_route(escalation, effective_provider_name)
    if route is None:
        return _reroute_fallback_result(
            gate_cfg,
            f"{message} — usage_gate.mode is reroute, but no models.escalation "
            f"rung avoids provider {effective_provider_name!r} "
            f"(usage_gate.reroute_fallback: {gate_cfg.reroute_fallback})",
        )

    return UsageGateResult(
        "reroute",
        f"{message} — rerouting from {effective_provider_name!r} to "
        f"{route!r} (usage_gate.mode: reroute)",
        route=route,
    )


def evaluate_usage_gate(
    limits: PlanLimits,
    gate_cfg: "UsageGateConfig",
    *,
    models_cfg: "ModelsConfig | None" = None,
    effective_provider_name: str = PROBE_CONSTRAINED_PROVIDER,
    assignment_type: str | None = None,
) -> UsageGateResult:
    """Pure decision: given a probed :class:`PlanLimits` and a
    ``coord.config.UsageGateConfig``, what should dispatch do?

    - ``gate_cfg.mode == "disabled"`` (the on/off switch) → always proceed, no
      message — the gate is not consulted at all.
    - ``limits`` not ``ok`` (probe unavailable/unknown, including non-OAuth
      auth) → always proceed. A probe we can't trust must never block,
      warn, or reroute — see the module docstring.
    - ``mode == "reroute"`` and *effective_provider_name* is not
      :data:`PROBE_CONSTRAINED_PROVIDER` (``"claude"``) → always proceed, no
      message. The ``/usage`` probe only ever measures Claude's own
      session/week windows (module docstring, AUTH SCOPE), so a dispatch
      that was already resolved onto a different provider (``providers.
      labels``, a repo/global override) was never going near the window
      this gate is reading — applying ANY ``reroute`` action to it would
      either reroute a dispatch that didn't need it, or (worse) reroute it
      straight onto the very provider under capacity pressure (#1649
      review). This check only applies to ``mode="reroute"``: ``"warn"``/
      ``"block"`` never took `effective_provider_name` into account before
      this issue and are unchanged (regression guard).
    - Below both thresholds → proceed, no message.
    - At/above a threshold:
      - ``"warn"``/``"block"`` (default ``"warn"`` — see ``UsageGateConfig``
        for why) → a message naming which window(s) tripped and their reset
        time(s).
      - ``"reroute"`` (#1649) → pick the first ``models.escalation`` rung
        whose provider is NOT *effective_provider_name* (the provider this
        dispatch was about to use) and return ``action="reroute"`` with
        that rung on :attr:`UsageGateResult.route`. Falls back to
        ``gate_cfg.reroute_fallback`` (``"warn"``/``"block"``) when
        *assignment_type* is pinned (``models.pinned`` — #1650, never
        degraded) or no rung escapes the constrained provider — see
        :func:`_evaluate_reroute`.

    Args:
        limits: The already-probed usage snapshot.
        gate_cfg: ``coordinator.yml``'s ``usage_gate:`` block.
        models_cfg: ``coordinator.yml``'s ``models:`` block — only
            consulted for ``mode="reroute"`` (``models.escalation``,
            ``models.pinned``). ``None`` behaves like an empty ladder (no
            rung to reroute to), never raises.
        effective_provider_name: The provider this dispatch was about to
            use, before any reroute — defaults to
            :data:`PROBE_CONSTRAINED_PROVIDER` (``"claude"``), the only
            provider the ``/usage`` probe ever measures. Only consulted for
            ``mode="reroute"`` — see the guard above.
        assignment_type: The dispatch's assignment ``type`` (e.g.
            ``"work"``, ``"review"``), checked against ``models.pinned``
            for the reroute pin-exemption. Only consulted for
            ``mode="reroute"``.
    """
    if gate_cfg.mode == "disabled":
        return UsageGateResult("proceed")
    if not limits.ok:
        return UsageGateResult("proceed")
    if gate_cfg.mode == "reroute" and effective_provider_name != PROBE_CONSTRAINED_PROVIDER:
        # #1649 review: this dispatch's own effective provider isn't the
        # one the probe constrains — the gate does not apply to it at all,
        # regardless of trigger state. Never fall through to the trigger
        # check below, which would otherwise reroute (or warn/block) a
        # dispatch that was never headed for the constrained window.
        return UsageGateResult("proceed")

    triggers: list[str] = []
    if limits.session_pct is not None and limits.session_pct >= gate_cfg.session_threshold_pct:
        triggers.append(_format_trigger("session", limits.session_pct, limits.session_resets_at))
    if limits.week_pct is not None and limits.week_pct >= gate_cfg.week_threshold_pct:
        triggers.append(_format_trigger("week", limits.week_pct, limits.week_resets_at))

    if not triggers:
        return UsageGateResult("proceed")

    message = "Max-plan usage near limit: " + "; ".join(triggers)

    if gate_cfg.mode == "reroute":
        return _evaluate_reroute(
            message=message,
            gate_cfg=gate_cfg,
            models_cfg=models_cfg,
            effective_provider_name=effective_provider_name,
            assignment_type=assignment_type,
        )

    action = "block" if gate_cfg.mode == "block" else "warn"
    return UsageGateResult(action, message)


# ── human-readable rendering (``coord usage --limits``) ────────────────────


def format_plan_limits(limits: PlanLimits) -> str:
    """Render *limits* for ``coord usage --limits``."""
    if not limits.ok:
        detail = f": {limits.error}" if limits.error else ""
        return f"Plan limits: unknown (probe unavailable{detail})"

    lines = ["Plan limits (subscription 5h/weekly windows):"]
    if limits.session_pct is not None:
        reset = f" (resets {limits.session_resets_at})" if limits.session_resets_at else ""
        lines.append(f"  session : {limits.session_pct:.0f}% used{reset}")
    if limits.week_pct is not None:
        reset = f" (resets {limits.week_resets_at})" if limits.week_resets_at else ""
        lines.append(f"  week    : {limits.week_pct:.0f}% used{reset}")
    for m in limits.week_by_model:
        reset = f" (resets {m.resets_at})" if m.resets_at else ""
        lines.append(f"  week ({m.label}): {m.used_pct:.0f}% used{reset}")
    return "\n".join(lines)
