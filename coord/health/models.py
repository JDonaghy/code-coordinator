"""Value types for the fleet-health check engine (#1628).

The whole point of this module is :class:`CheckResult`.  A result carries
**both** the raw values a probe measured **and** the already-rendered
headroom string — because the moment a renderer starts re-deriving "is 86%
used bad?" from ``values["used_pct"]``, the severity logic has forked and
every new surface (the CLI here, the board projection in H-3, the TUI/web
renderers in H-4) gets to disagree about what WARN means.

So the contract is:

* A **probe** owns thresholds.  It is the only thing allowed to turn raw
  numbers into a :class:`Severity`.
* A **renderer** owns layout.  It may reorder, truncate, colour, or drop
  results; it may NOT look at ``values`` to decide severity, and it may not
  reformat ``headroom`` from the raw numbers.
* ``values`` exists for machine consumers (trend series, alert routing,
  whoever comes next) — never as a second source of truth for "how bad".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Severity(str, Enum):
    """How much headroom is left, worst-first when sorted by :attr:`rank`.

    ``UNKNOWN`` is deliberately not "fine": a probe that could not run tells
    you nothing, and treating that as OK is how a silently-broken check
    becomes indistinguishable from a healthy machine.  It ranks *above* OK
    (so it surfaces) and *below* WARN (so it never pages).
    """

    OK = "ok"
    UNKNOWN = "unknown"
    WARN = "warn"
    CRIT = "crit"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @property
    def label(self) -> str:
        """Fixed-width-ish display token: ``OK`` / ``WARN`` / ``CRIT`` / ``?``."""
        return "?" if self is Severity.UNKNOWN else self.value.upper()

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.rank < other.rank
        return NotImplemented


_SEVERITY_RANK = {
    Severity.OK: 0,
    Severity.UNKNOWN: 1,
    Severity.WARN: 2,
    Severity.CRIT: 3,
}

# The three scopes a check can answer for.  ``fleet`` is defined here (so the
# registry shape is settled — that is this child's whole job) but no seed
# probe uses it; fleet-scope probes land in H-3.
SCOPES = ("machine", "checkout", "fleet")


def worst(severities: "list[Severity] | tuple[Severity, ...]") -> Severity:
    """The most severe of *severities* (``OK`` when empty)."""
    out = Severity.OK
    for s in severities:
        if s.rank > out.rank:
            out = s
    return out


@dataclass(frozen=True)
class CheckResult:
    """One answer to "how much headroom is left?".

    ``headroom`` is the load-bearing field for every renderer: a short,
    already-formatted phrase such as ``"86% used (22G free)"`` or
    ``"128.8h stale, hooks disabled -> will not self-heal"``.  It is produced
    by the probe, which is also the thing that chose ``severity`` — the two
    can never disagree because nothing downstream recomputes either.
    """

    check_id: str
    scope: str
    severity: Severity
    headroom: str
    # Display prefix, e.g. "disk" / "cargo targets" / "graph".  Defaults to
    # ``check_id`` when a probe doesn't override it.
    title: str = ""
    # What this result is *about* within the check: "/home", "vimcode", ...
    # ``None`` for whole-machine singletons like the claude-binary check.
    subject: str | None = None
    # Rendered threshold reminder, e.g. "crit at 93%".  Optional.
    threshold: str = ""
    # Extra rendered context shown under/after the headroom.  Optional.
    detail: str = ""
    # Rendered trend, where a probe can get one cheaply ("+4G since 6h ago").
    trend: str | None = None
    # Raw measurements, for machine consumers only.  NOT a severity source.
    values: dict[str, Any] = field(default_factory=dict)
    # Set iff the probe failed soft; ``severity`` is then UNKNOWN.
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.title:
            object.__setattr__(self, "title", self.check_id)

    @property
    def key(self) -> str:
        """Stable identity for this row: ``<check_id>`` or ``<check_id>:<subject>``."""
        return f"{self.check_id}:{self.subject}" if self.subject else self.check_id

    @property
    def label(self) -> str:
        """Human row label, e.g. ``"disk /home"`` or ``"claude binary"``."""
        return f"{self.title} {self.subject}" if self.subject else self.title

    def to_dict(self) -> dict[str, Any]:
        """The JSON contract H-3/H-4 consume.  Keys here are load-bearing."""
        return {
            "key": self.key,
            "check_id": self.check_id,
            "scope": self.scope,
            "subject": self.subject,
            "title": self.title,
            "label": self.label,
            "severity": self.severity.value,
            "headroom": self.headroom,
            "threshold": self.threshold,
            "detail": self.detail,
            "trend": self.trend,
            "values": dict(self.values),
            "error": self.error,
        }


def unknown_result(
    check_id: str,
    *,
    scope: str,
    error: str,
    title: str = "",
    subject: str | None = None,
) -> CheckResult:
    """A fail-soft placeholder: the probe blew up, the run carries on.

    Never raise out of a probe — a health engine that dies on its own
    weakest check reports nothing at all, which is strictly worse than
    reporting eight checks and one ``?``.
    """
    return CheckResult(
        check_id=check_id,
        scope=scope,
        severity=Severity.UNKNOWN,
        headroom=f"probe failed: {error}",
        title=title or check_id,
        subject=subject,
        error=error,
    )


@dataclass(frozen=True)
class FixOutcome:
    """One answer to "did applying this check's remedy do anything?" (#2581).

    Mirrors :class:`CheckResult`'s posture: a **fixer** (the ``fix`` callable
    on an allow-listed :class:`~coord.health.registry.Check`) is the only
    thing allowed to decide ``status`` — a renderer or the CLI may format
    this, never recompute it.

    ``status`` is one of:

    * ``"applied"`` — the remedy ran and changed something.
    * ``"no_action"`` — the fixer ran but found nothing to do (the
      condition already resolved, or a re-verified precondition no longer
      holds). This is what makes re-running ``--fix`` idempotent: a second
      pass over an already-fixed finding lands here, not on ``"applied"``
      again.
    * ``"suppressed"`` — covered by an unexpired entry in
      ``~/.coord/watchdog-suppress.json`` (the same intent sentinel #2580
      defines) — reported, never applied.
    * ``"not_allowlisted"`` — the check has a remedy string but is not one
      of the explicit opt-in checks (``Check.fix is None``) — reported,
      never applied. A check earns this by NOT setting ``fix=`` when it is
      registered; there is no separate list to keep in sync.
    * ``"error"`` — the fixer raised, or refused after re-verifying (e.g. a
      lock's holder became unconfirmable between detection and repair).
    """

    check_id: str
    subject: str | None
    status: str
    message: str
    error: str | None = None

    @property
    def key(self) -> str:
        """Stable identity, same shape as :attr:`CheckResult.key`."""
        return f"{self.check_id}:{self.subject}" if self.subject else self.check_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "check_id": self.check_id,
            "subject": self.subject,
            "status": self.status,
            "message": self.message,
            "error": self.error,
        }


@dataclass(frozen=True)
class Checkout:
    """One local git checkout a ``checkout``-scoped probe can run against."""

    name: str
    path: Path
    default_branch: str = "main"
    # #934's opt-in develop branch.  A repo parked on *this* is not "parked
    # on a non-default branch" — it's the configured integration branch.
    develop_branch: str | None = None

    @property
    def home_branches(self) -> tuple[str, ...]:
        out = [self.default_branch]
        if self.develop_branch:
            out.append(self.develop_branch)
        return tuple(out)


@dataclass
class FleetSnapshot:
    """What a ``fleet``-scope probe is allowed to know about the rest of the
    fleet (#1630).

    Only ever populated by the daemon (``coord.serve_app``'s health-poll
    tick), from data it already collected polling every agent's ``/health``
    plus a handful of facts that are genuinely local to the daemon process
    itself (its own ``coord-serve`` venv).  ``coord health`` run by hand on a
    single machine has no fleet view — ``HealthContext.fleet`` is ``None``
    there, and ``fleet``-scope checks are simply not run (see
    ``registry.run_all``'s ``scopes`` filter).

    #1806: the CLI-venv version and the ``tui/`` binary-vs-source comparison
    are NOT daemon-host facts, even though an earlier version of this
    dataclass's ``daemon_host`` blob carried them — both are facts about
    whichever machine the operator actually put them on, which is often not
    the daemon host.  They ride ``machines`` instead (each machine's own
    ``cli_venv``/``tui_binary`` checks, see
    :mod:`coord.health.checks.deploy_lane_facts`) and are aggregated fleet-
    wide from there, not read out of ``daemon_host``.
    """

    # machine_name -> {"state": "online"/"offline"/..., "reason": str,
    # "latency_ms": float | None, "received_at": float,
    # "checks": <agent's own H-1 report dict, or None if unreachable/old agent>}
    machines: dict[str, dict] = field(default_factory=dict)
    # Free-form daemon-host-local facts a fleet probe needs (its own
    # coord-serve venv version, /board latency + payload size, phantom-
    # running assignment ids, per-repo toolchain kinds + CI's pinned
    # versions, ...).  Kept untyped/free-form (like ``values`` on
    # CheckResult) so a new fleet fact doesn't require touching this
    # dataclass.  See the class docstring's #1806 note for what does NOT
    # belong here.
    daemon_host: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthContext:
    """Everything a probe is allowed to know about the machine it's on.

    Passing this rather than letting probes reach for ``Path.home()`` /
    ``coord.config.load()`` directly is what makes them unit-testable
    without a real filesystem or a real ``coordinator.yml``.
    """

    thresholds: Any  # coord.config.HealthConfig — untyped to avoid an import cycle
    home: Path
    coord_dir: Path
    now: float
    checkouts: tuple[Checkout, ...] = ()
    # The loaded coordinator.yml, when there is one.  Probes must tolerate
    # ``None`` (``coord health`` on a machine with no config still works).
    config: Any = None
    # False on a timer/offline run: probes marked ``cost="network"`` are
    # skipped entirely rather than left to time out.
    allow_network: bool = True
    # Set only by the daemon for a ``scopes=("fleet",)`` run (#1630).  ``None``
    # everywhere else, including every ``machine``/``checkout`` probe — a
    # fleet probe that forgets to guard against ``None`` fails soft via the
    # registry's fail-soft ``run_check`` wrapper, not by crashing the run.
    fleet: "FleetSnapshot | None" = None
