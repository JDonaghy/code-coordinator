"""The check registry (#1628).

**The acceptance bar for this abstraction is that adding a check touches
exactly one file: the new check module.**  Not the renderer, not the CLI,
not a transport.  That is enforced two ways:

1. Registration is a decorator (:func:`check`) applied in the check's own
   module — there is no central list of checks to append to.
2. Discovery is :func:`pkgutil.iter_modules` over ``coord.health.checks`` —
   dropping ``coord/health/checks/foo.py`` into the package is the whole
   installation step.  ``checks/__init__.py`` deliberately imports nothing.

The other half of the contract is fail-soft.  :func:`run_all` wraps every
probe in a bare ``except Exception`` and converts a raised probe into an
``unknown`` result carrying the error text.  A health engine that dies on
its weakest check reports nothing, which is worse than reporting the rest
plus one ``?``.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import pkgutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coord.health.models import (
    SCOPES,
    CheckResult,
    FixOutcome,
    HealthContext,
    Severity,
    unknown_result,
)

# What a probe costs to run.  ``cheap`` probes are local syscalls/subprocesses
# and the whole cheap set is budgeted under ~2s, because this eventually runs
# on a timer on every agent.  ``network`` probes (the one PyPI simple-index
# fetch, the ``claude -p /usage`` round trip) are skipped when the caller
# passes ``allow_network=False``.
COST_CHEAP = "cheap"
COST_NETWORK = "network"

ProbeFn = Callable[[HealthContext], CheckResult | Sequence[CheckResult] | None]

# A fixer takes the context and ONE of its own check's results and applies
# (or declines to apply) that result's remedy.  Returning ``None``/an empty
# sequence means "nothing to report" (rare — usually a fixer returns exactly
# one FixOutcome, or several when one CheckResult row bundles several
# independently-repairable items, e.g. index_lock's per-checkout locks).
#
# #2581: this is deliberately opt-in per check (``Check.fix``), not "every
# check with a `detail` string" — a check earns machine-applicability by
# setting ``fix=`` when it registers, nothing infers it from the presence of
# remedy text.
FixFn = Callable[[HealthContext, CheckResult], "FixOutcome | Sequence[FixOutcome] | None"]


@dataclass(frozen=True)
class Check:
    """A self-contained unit of fleet-degradation signal."""

    id: str
    scope: str
    probe: ProbeFn
    title: str = ""
    description: str = ""
    cost: str = COST_CHEAP
    # Display/run ordering.  Lives on the check so a new module can slot
    # itself into the report without anyone editing a renderer's list.
    order: int = 100
    # #2581: the explicit allow-list.  ``None`` (the default for every check
    # that doesn't set it) means "report only" — `coord health --fix` never
    # touches it, no matter how actionable its `detail` string reads.
    fix: FixFn | None = None

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"check {self.id!r}: scope must be one of {SCOPES}, got {self.scope!r}")
        if self.cost not in (COST_CHEAP, COST_NETWORK):
            raise ValueError(f"check {self.id!r}: cost must be 'cheap' or 'network'")
        if not self.title:
            object.__setattr__(self, "title", self.id)


_REGISTRY: dict[str, Check] = {}
_discovered = False


def register(chk: Check) -> Check:
    """Add *chk* to the registry.  Re-registering the same id replaces it
    (module reload under pytest must not raise)."""
    _REGISTRY[chk.id] = chk
    return chk


def check(
    *,
    id: str,  # noqa: A002 — matches the field name in the issue's spec
    scope: str,
    title: str = "",
    description: str = "",
    cost: str = COST_CHEAP,
    order: int = 100,
    fix: FixFn | None = None,
) -> Callable[[ProbeFn], ProbeFn]:
    """Decorator form: register the decorated function as a check's probe.

    The probe returns one :class:`CheckResult`, a sequence of them (one per
    disk / per checkout / ...), or ``None`` for "nothing to report here".

    *fix* is the #2581 opt-in: pass it only for a check whose remedy is
    idempotent, reversible, and safe to run unattended. Omitting it (the
    default) is what keeps a check report-only — `coord health --fix`
    reports its finding and its remedy string exactly like today, and never
    calls anything.
    """

    def _wrap(fn: ProbeFn) -> ProbeFn:
        register(
            Check(
                id=id,
                scope=scope,
                probe=fn,
                title=title,
                description=description,
                cost=cost,
                order=order,
                fix=fix,
            )
        )
        return fn

    return _wrap


def discover(force: bool = False) -> None:
    """Import every module under ``coord.health.checks`` so its decorators run.

    This is the *entire* registration mechanism.  There is no list to edit.
    """
    global _discovered
    if _discovered and not force:
        return
    from coord.health import checks as _checks_pkg

    for mod in pkgutil.iter_modules(_checks_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{_checks_pkg.__name__}.{mod.name}")
    _discovered = True


def all_checks() -> list[Check]:
    """Every registered check, in stable report order."""
    discover()
    return sorted(_REGISTRY.values(), key=lambda c: (c.order, c.id))


def get(check_id: str) -> Check | None:
    discover()
    return _REGISTRY.get(check_id)


def run_check(chk: Check, ctx: HealthContext) -> list[CheckResult]:
    """Run one check, fail-soft.

    A probe that raises — any exception, including ``KeyboardInterrupt``'s
    non-``Exception`` siblings excluded — yields a single ``unknown`` result
    naming the error, and the run continues.
    """
    try:
        out = chk.probe(ctx)
    except Exception as exc:  # noqa: BLE001 — fail soft is the requirement
        return [
            unknown_result(
                chk.id,
                scope=chk.scope,
                title=chk.title,
                error=f"{type(exc).__name__}: {exc}",
            )
        ]
    if out is None:
        return []
    results = [out] if isinstance(out, CheckResult) else list(out)
    # A probe that forgets its own title/scope shouldn't produce rows the
    # renderer can't label.  Backfill from the check definition.
    return [
        dataclasses.replace(r, title=chk.title) if r.title == r.check_id else r
        for r in results
    ]


@dataclass
class HealthReport:
    """The full outcome of one registry run."""

    results: list[CheckResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    duration_secs: float = 0.0

    @property
    def severity(self) -> Severity:
        from coord.health.models import worst

        return worst([r.severity for r in self.results])

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for r in self.results:
            out[r.severity.value] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        """The ``coord health --json`` contract (H-3/H-4 consume this)."""
        return {
            "schema": 1,
            "severity": self.severity.value,
            "counts": self.counts(),
            "skipped": list(self.skipped),
            "duration_secs": round(self.duration_secs, 3),
            "results": [r.to_dict() for r in self.results],
        }


def run_all(
    ctx: HealthContext,
    *,
    scopes: Iterable[str] | None = None,
    only: Iterable[str] | None = None,
) -> HealthReport:
    """Run the registry against *ctx*.

    * ``scopes`` — restrict to these scopes (``coord health --local`` passes
      ``("machine", "checkout")``; ``fleet`` probes arrive in H-3).
    * ``only`` — restrict to these check ids.
    * ``ctx.allow_network`` False skips ``cost="network"`` checks, recording
      their ids in :attr:`HealthReport.skipped` so "we didn't look" never
      reads as "nothing wrong".
    * ``thresholds.disabled_checks`` skips by operator config, same way.
    """
    import time  # noqa: PLC0415 — local so a frozen-clock test can patch it

    started = time.monotonic()
    scope_filter = set(scopes) if scopes is not None else None
    only_filter = set(only) if only is not None else None
    disabled = set(getattr(ctx.thresholds, "disabled_checks", ()) or ())

    report = HealthReport()
    for chk in all_checks():
        if scope_filter is not None and chk.scope not in scope_filter:
            continue
        if only_filter is not None and chk.id not in only_filter:
            continue
        if chk.id in disabled:
            report.skipped.append(f"{chk.id} (disabled in coordinator.yml)")
            continue
        if chk.cost == COST_NETWORK and not ctx.allow_network:
            report.skipped.append(f"{chk.id} (network probe, --no-network)")
            continue
        report.results.extend(run_check(chk, ctx))
    report.duration_secs = time.monotonic() - started
    return report


# ---------------------------------------------------------------------------
# #2581: `coord health --fix` — applying an allow-listed check's own remedy.
#
# The whole engine here is deliberately small: a check earns machine-
# applicability by passing `fix=` at registration (see `Check.fix` /
# `check()` above) — there is no separate list of "fixable" ids to keep in
# sync, same acceptance bar as the check registry itself. Everything below
# is generic apparatus that works for any such check: the intent sentinel
# (shared with `scripts/fleet_watchdog.py`'s #2580 design) and the fail-soft
# wrapper that keeps one broken fixer from aborting the rest.
# ---------------------------------------------------------------------------

# Same file, same shape, as `scripts/fleet_watchdog.py`'s intent sentinel
# (#2580): `{"<key>": {"reason": str, "set": str, "expires": iso8601|null}}`.
# Deliberately duplicated rather than imported — that script is stdlib-only
# and constitutionally forbidden from `import coord` (#2580 constraint 3),
# so the two readers of this one file cannot share code across that
# boundary. The *format* is what must never drift, not the parsing code.
SUPPRESS_FILENAME = "watchdog-suppress.json"


def _read_json_object(path: Path) -> dict:
    try:
        raw = path.read_text()
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_suppressions(coord_dir: Path) -> dict:
    """Read ``<coord_dir>/watchdog-suppress.json``, empty dict if absent/bad.

    Never raises: a missing or malformed sentinel file must default to
    "nothing is suppressed" (report/fix normally), not to a crashed
    ``--fix`` run.
    """
    return _read_json_object(Path(coord_dir) / SUPPRESS_FILENAME)


def is_suppressed(
    suppressions: dict, keys: Iterable[str], *, now: float
) -> tuple[bool, dict | None]:
    """Is any of *keys* covered by an unexpired sentinel entry?

    Ported from ``scripts/fleet_watchdog.py``'s function of the same name
    (#2580) — same contract, including its most load-bearing edge case: an
    ``"expires"`` value that isn't parseable ISO-8601 is treated as *still
    active* rather than silently ignored, because a typo in an operator's
    suppression file must fail closed (never fixed) rather than fail open
    (silently un-suppressed).
    """
    for key in keys:
        entry = suppressions.get(key)
        if not isinstance(entry, dict):
            continue
        expires = entry.get("expires")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(str(expires))
            except ValueError:
                return True, entry
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if now >= exp_dt.timestamp():
                continue  # lapsed — falls through to normal handling
        return True, entry
    return False, None


def _suppress_keys_for(chk: Check, result: CheckResult) -> tuple[str, ...]:
    """Default suppression keys for a whole ``CheckResult`` row.

    Both the specific (``check_id:subject``) and the bare check id are
    checked, so an operator can suppress one checkout's graph
    (``"graph:claude-coordinator"``) or every graph finding at once
    (``"graph"``) with the same file. A fixer that bundles several
    independently-repairable items in one row (index_lock, worktrees) does
    its own finer-grained, per-item suppression check on top of this one.
    """
    return (result.key, chk.id)


def fixable_checks() -> list[Check]:
    """Every registered check that opted in a machine-applicable remedy."""
    return [c for c in all_checks() if c.fix is not None]


def run_fix(
    chk: Check,
    ctx: HealthContext,
    result: CheckResult,
    suppressions: dict,
) -> list[FixOutcome]:
    """Apply *chk*'s remedy for *result*, fail-soft, honouring the sentinel.

    Mirrors :func:`run_check`'s contract: a fixer that raises never aborts
    the run, it just yields one ``error`` outcome naming what happened.
    """
    if chk.fix is None:
        return [
            FixOutcome(
                check_id=chk.id,
                subject=result.subject,
                status="not_allowlisted",
                message="no machine-applicable remedy for this check — reported only",
            )
        ]

    suppressed, entry = is_suppressed(suppressions, _suppress_keys_for(chk, result), now=ctx.now)
    if suppressed:
        reason = (entry or {}).get("reason") or "suppressed"
        return [
            FixOutcome(
                check_id=chk.id,
                subject=result.subject,
                status="suppressed",
                message=f"suppressed: {reason}",
            )
        ]

    try:
        out = chk.fix(ctx, result)
    except Exception as exc:  # noqa: BLE001 — fail soft, same contract as run_check
        return [
            FixOutcome(
                check_id=chk.id,
                subject=result.subject,
                status="error",
                message="fixer raised",
                error=f"{type(exc).__name__}: {exc}",
            )
        ]
    if out is None:
        return []
    return [out] if isinstance(out, FixOutcome) else list(out)


def apply_fixes(
    ctx: HealthContext,
    report: HealthReport,
    *,
    suppressions: dict | None = None,
) -> list[FixOutcome]:
    """Apply every allow-listed, unsuppressed remedy for *report*'s findings.

    ``OK`` rows are skipped outright — there is nothing to fix — which is
    also what makes running ``--fix`` twice in a row a no-op by construction:
    fix a finding once, the next ``run_all`` reports it ``OK``, and a second
    ``apply_fixes`` over that report never even calls the fixer for it.
    """
    if suppressions is None:
        suppressions = load_suppressions(ctx.coord_dir)
    outcomes: list[FixOutcome] = []
    for result in report.results:
        if result.severity is Severity.OK:
            continue
        chk = get(result.check_id)
        if chk is None:  # pragma: no cover - defensive, can't happen via run_all
            continue
        outcomes.extend(run_fix(chk, ctx, result, suppressions))
    return outcomes
