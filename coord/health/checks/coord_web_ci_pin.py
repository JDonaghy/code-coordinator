"""Is `coord-web`'s CI still installing a `coord` that can boot `coord web`?

**The coupling this measures.** `coord-web` (epic #2002) is nominally a pure
HTTP client of the coord daemon, but its Playwright suites are not: the
fixture spec, and the sealed acceptance config
(`playwright.acceptance.config.ts`), boot a **real** ``coord web --fixture
<file> --dist dist`` process as their ``webServer`` (#1818). That means
`coord-web`'s CI has to install *this* repo's package on the runner, and
`coord-web`'s CI therefore encodes a cross-repo contract: which `coord` its
frontend is proven against.

**Why that needs a check at all (#2006).** A version spec sitting in another
repo's workflow YAML is exactly the shape of thing this fleet has already
been burned by, twice:

* ``~/.coord-cli-venv`` was found **three releases stale** on 2026-07-29 —
  silently, because nothing measured it. That incident is why
  ``deploy_lane_facts.probe_cli_venv`` exists.
* vimcode#615 (#1629): CI built on rustc 1.97.1 while every fleet machine
  was months behind, and six snapshot tests were green everywhere and red in
  CI. That is why :mod:`coord.health.checks.toolchain` learned to read a
  repo's workflow YAML at all.

`coord-web`'s `coord` spec is the same class of fact, one repo further away.
:doc:`docs/ADR_COORD_WEB_CI` records the decision it is graded against —
**track latest, never exact-pin** — and this check is the "visible and
assertable" half that ADR promises. Without it the decision is a paragraph
in a document; with it, ``coord health`` prints the actual spec string, from
the actual file, on every tick.

**Annotate, don't gate**, exactly like ``toolchain``/``cli_venv``: nothing
here blocks a dispatch, a routing decision, or a merge. The severities say
"go look at coord-web's ci.yml", never "stop".

**Local-filesystem only.** The workflow YAML is read from a `coord-web`
checkout already on this machine (``repo_paths.coord-web`` in
coordinator.yml). No ``gh`` call, no network — cheap enough for the health
poll tick, and absent on a machine with no `coord-web` checkout, which is
reported as OK ("not present on this machine"), the same way every other
checkout-derived lane reports absence.
"""

from __future__ import annotations

from pathlib import Path

from coord import __version__ as LOCAL_COORD_VERSION
from coord.health.checks._ci_pin import (
    SERVER_EXTRA,
    TOMBSTONE_DIST,
    CoordPin,
    find_coord_pins,
    floor_exceeds,
    norm_dist,
    pin_values,
)
from coord.health.checks._ci_pin import where as _where
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import expand, shorten_path

# #2900 re-exports: the parsing half of this module moved to `_ci_pin` when
# `coord_tui_ci_pin` became its second consumer. These names stay importable
# from here — they are this module's documented surface, and the split is an
# internal reorganisation, not a contract change.
__all__ = [
    "CHECK_ID",
    "COORD_WEB_MARKER",
    "COORD_WEB_REPO_NAME",
    "SERVER_EXTRA",
    "TOMBSTONE_DIST",
    "CoordPin",
    "find_coord_pins",
    "probe_coord_web_ci_pin",
    "resolve_coord_web_checkout",
]

CHECK_ID = "coord_web_ci_pin"

#: The repo name (``repos[].name`` / ``repo_paths`` key in coordinator.yml)
#: whose CI this check reads.
COORD_WEB_REPO_NAME = "coord-web"

#: A file at a checkout's root that identifies it as `coord-web` even if the
#: repo is renamed or checked out under a different directory name: it is the
#: config whose ``webServer`` boots ``coord web --fixture`` (#1818), i.e. the
#: literal reason this cross-repo coupling exists.
COORD_WEB_MARKER = "playwright.acceptance.config.ts"


def resolve_coord_web_checkout(ctx: HealthContext) -> Path | None:
    """This machine's `coord-web` checkout, if it has one.

    Same convention as every other path in :class:`coord.config.HealthConfig`:
    a configured ``health.coord_web_checkout`` wins outright, ``None`` means
    "discover it", never "disable the lane".
    """
    configured = getattr(ctx.thresholds, "coord_web_checkout", None)
    if configured:
        return expand(configured, ctx.home)
    for checkout in ctx.checkouts:
        if norm_dist(checkout.name) == COORD_WEB_REPO_NAME:
            return checkout.path
    # Fall back to the structural marker so a rename of the repo doesn't
    # silently turn this lane off — an off lane is indistinguishable from a
    # healthy one, which is the whole failure mode being guarded against.
    for checkout in ctx.checkouts:
        if (checkout.path / COORD_WEB_MARKER).is_file():
            return checkout.path
    return None


def _result(
    severity: Severity,
    headroom: str,
    values: dict,
    *,
    detail: str = "",
) -> CheckResult:
    return CheckResult(
        check_id=CHECK_ID,
        scope="machine",
        severity=severity,
        headroom=headroom,
        detail=detail,
        values=values,
    )


@check(
    id=CHECK_ID,
    scope="machine",
    title="coord-web ci pin",
    order=46,
    description=(
        "The coord spec coord-web's CI installs to boot `coord web --fixture` "
        "(#2006) — must carry the [server] extra and must not exact-pin."
    ),
)
def probe_coord_web_ci_pin(ctx: HealthContext) -> CheckResult:
    checkout = resolve_coord_web_checkout(ctx)
    if checkout is None or not checkout.is_dir():
        # The overwhelming common case: only the machines that list coord-web
        # in `repo_paths` have one. Absence is not a fault.
        return _result(
            Severity.OK,
            "not present on this machine",
            {"present": False, "checkout": str(checkout) if checkout else None, "pins": []},
        )

    short = shorten_path(str(checkout), str(ctx.home))
    pins = find_coord_pins(checkout)
    values: dict = {
        "present": True,
        "checkout": str(checkout),
        "local_version": LOCAL_COORD_VERSION,
        "pins": pin_values(pins),
    }

    if not pins:
        # coord-web's e2e/acceptance jobs shell out to `coord web --fixture`
        # as their Playwright webServer. No install step means either the
        # job was deleted or it is about to fail with "coord: not found".
        return _result(
            Severity.WARN,
            "coord-web CI installs no coord CLI",
            values,
            detail=(
                f"{short}/.github/workflows has no `pip install code-coordinator[server]` — "
                "the Playwright webServer that boots `coord web --fixture` (#1818) "
                "cannot start. See docs/ADR_COORD_WEB_CI.md."
            ),
        )

    specs = ", ".join(f"{p.spec} ({_where(p)})" for p in pins)

    tombstoned = [p for p in pins if p.is_tombstone]
    if tombstoned:
        return _result(
            Severity.CRIT,
            f"coord-web CI installs the dead distribution name: {specs}",
            values,
            detail=(
                f"`{TOMBSTONE_DIST}` is a permanent PyPI tombstone that will never gain "
                f"another release (#2106) — CI is frozen on its last-ever version. "
                f"Rename to `code-coordinator[{SERVER_EXTRA}]` in "
                f"{', '.join(sorted({_where(p) for p in tombstoned}))}."
            ),
        )

    clientonly = [p for p in pins if not p.has_server_extra]
    if clientonly:
        return _result(
            Severity.CRIT,
            f"coord-web CI installs a client-only coord: {specs}",
            values,
            detail=(
                f"`coord web` is a server command; uvicorn/Starlette live behind the "
                f"[{SERVER_EXTRA}] extra since the base/server split (#1237). "
                f"Add it in {', '.join(sorted({_where(p) for p in clientonly}))}."
            ),
        )

    exact = [p for p in pins if p.is_exact_pin]
    if exact:
        return _result(
            Severity.WARN,
            f"coord-web CI exact-pins coord: {specs}",
            values,
            detail=(
                "docs/ADR_COORD_WEB_CI.md decided track-latest precisely because an "
                "exact pin rots silently — the same failure as ~/.coord-cli-venv found "
                "three releases stale on 2026-07-29. An exact-pinned coord-web stays "
                "green while the coord its users actually run has already broken the "
                "contract. Relax to a `>=` floor."
            ),
        )

    floors = {p.floor for p in pins if p.floor}
    unsatisfiable = sorted(f for f in floors if floor_exceeds(f, LOCAL_COORD_VERSION))
    if unsatisfiable:
        return _result(
            Severity.WARN,
            f"coord-web CI floor is ahead of this machine's coord: {specs}",
            values,
            detail=(
                f"floor {', '.join(unsatisfiable)} > local coord {LOCAL_COORD_VERSION} — "
                "either the floor names a release that is not out yet, or this machine "
                "is behind the coord coord-web's CI proves its frontend against."
            ),
        )

    floor_note = f" (floor {', '.join(sorted(floors))})" if floors else ""
    return _result(
        Severity.OK,
        f"tracks latest{floor_note}: {specs}",
        values,
        detail=f"local coord {LOCAL_COORD_VERSION}; source {short}",
    )
