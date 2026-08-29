"""Is `coord-tui`'s CI still installing a `coord` — and still running the drift gate?

**The coupling this measures (#2900, Phase 4 of #2894).** Since #2899 the
`coord-tui` crate lives in its own repo, but two files it commits are not
written by hand — they are generated from *this* repo's live wire schema:

===============================================  ==============================
`coord-tui` file                                 generator (this repo)
===============================================  ==============================
``src/app/types/generated.rs``                   ``scripts/codegen.py --rust``
``src/app/types/generated_requests.rs``          ``scripts/codegen.py --rust``
``tests/fixtures/board_sample.json``             ``scripts/gen_board_fixture.py``
===============================================  ==============================

While the crate lived inside this repo, a single `pytest` run byte-compared
them, because generator and output shared a checkout. They do not any more.
The comparison now runs **downstream**, in `coord-tui`'s
``codegen-drift.yml``, pulling the generator from PyPI. That makes
`coord-tui`'s CI the *only* thing standing between a field rename here and a
`/board` payload that deserialises into the wrong shape and blanks the entire
TUI — the #632/#546/#628 failure class, silently.

**Why that needs a check here.** A guarantee that lives entirely in another
repo's workflow YAML is not assertable from the repo that can break it. Two
distinct ways it rots, both invisible from here without this check:

1. **The install rots.** `coord-tui`'s CI installs `code-coordinator[server]`
   to get the generator. Exact-pin it, or name the ``claude-coordinator``
   tombstone (#2106), or drop the ``[server]`` extra that carries the
   Starlette ``scripts/codegen.py --rust`` imports at module scope (#1237),
   and the gate is green while the contract it guards has already moved.
   That is precisely the ``~/.coord-cli-venv`` failure (found **three
   releases stale** on 2026-07-29) and vimcode#615 (#1629), one repo further
   away. :doc:`docs/ADR_COORD_WEB_CI` recorded the decision both downstream
   repos are graded against — **track latest, never exact-pin** — and
   :mod:`coord.health.checks.coord_web_ci_pin` is this check's sibling and
   template for the `coord-web` half.

2. **The gate disappears.** Worse than a stale pin, and unique to this repo
   pair: `coord-tui`'s CI can keep installing a perfectly good `coord` while
   no step actually runs ``codegen.py --rust --check``. Nothing goes red;
   there is simply no longer anything checking. So this check does not just
   grade the pin — it asserts the **drift step still exists**, which is the
   only reason the pin matters at all. That is the acceptance bar #2900 set:
   *"a deliberate field rename in ``coord/board_schema.py`` merged here turns
   coord-tui's CI red on its next run."*

**Annotate, don't gate**, exactly like ``toolchain``/``cli_venv``/
``coord_web_ci_pin``: nothing here blocks a dispatch, a routing decision, or
a merge. The severities say "go look at coord-tui's codegen-drift.yml", never
"stop".

**Local-filesystem only.** The workflow YAML is read from a `coord-tui`
checkout already on this machine (``repo_paths.coord-tui`` /
``health.coord_tui_checkout``). No ``gh`` call, no network — cheap enough for
the health poll tick, and absent on a machine with no `coord-tui` checkout,
which is reported as OK ("not present on this machine"), the same way every
other checkout-derived lane reports absence.
"""

from __future__ import annotations

import re
from pathlib import Path

from coord import __version__ as LOCAL_COORD_VERSION
from coord.health.checks._ci_pin import (
    LIVE_DIST,
    SERVER_EXTRA,
    TOMBSTONE_DIST,
    CoordPin,
    find_coord_pins,
    floor_exceeds,
    iter_run_steps,
    pin_values,
)
from coord.health.checks._ci_pin import where as _where
from coord.health.checks.deploy_lane_facts import resolve_coord_tui_checkout
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import shorten_path

__all__ = [
    "CHECK_ID",
    "GENERATED_ARTIFACTS",
    "CoordPin",
    "find_drift_gate_steps",
    "probe_coord_tui_ci_pin",
]

CHECK_ID = "coord_tui_ci_pin"

#: The generated files `coord-tui` commits, relative to its checkout root —
#: named in this check's output so the reader knows *what* the pin protects
#: rather than just that a pin exists. Kept in step with
#: ``scripts/codegen.py``'s ``RUST_OUTPUT_RELPATH`` /
#: ``RUST_REQUESTS_OUTPUT_RELPATH`` and ``scripts/gen_board_fixture.py``.
GENERATED_ARTIFACTS: tuple[str, ...] = (
    "src/app/types/generated.rs",
    "src/app/types/generated_requests.rs",
    "tests/fixtures/board_sample.json",
)

# The drift step, as `coord-tui`'s codegen-drift.yml writes it:
#
#     COORD_TUI_SRC="$GITHUB_WORKSPACE" python scripts/codegen.py --rust --check
#
# Matched structurally (the script name, then `--rust`, then `--check`, in
# that order on one line) rather than as a fixed string, because the env-var
# prefix, the interpreter, and the path to `scripts/` are all things that
# workflow is entitled to change without weakening the guarantee. What it is
# *not* entitled to drop is any of the three tokens: `--rust` without
# `--check` regenerates and silently succeeds, which is the exact shape of a
# gate that reports green while drifting.
_DRIFT_STEP_RE = re.compile(r"codegen\.py\b[^\n]*?--rust\b[^\n]*?--check\b")


def find_drift_gate_steps(checkout_path: Path) -> list[tuple[str, str]]:
    """``(workflow, job)`` for every step that runs the Rust codegen drift gate.

    Empty means *nothing downstream is comparing the committed Rust wire
    types against this repo's served schema* — see this module's docstring
    for why that is the finding this check exists for.
    """
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for workflow, job, run in iter_run_steps(checkout_path):
        for line in run.splitlines():
            if _DRIFT_STEP_RE.search(line):
                key = (workflow, job)
                if key not in seen:
                    seen.add(key)
                    found.append(key)
    return found


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
    title="coord-tui ci pin",
    order=47,
    description=(
        "The coord spec coord-tui's CI installs to run `codegen.py --rust --check` "
        "(#2900) — must carry the [server] extra, must not exact-pin, and the "
        "drift step itself must still be there."
    ),
)
def probe_coord_tui_ci_pin(ctx: HealthContext) -> CheckResult:
    checkout = resolve_coord_tui_checkout(ctx)
    if checkout is None or not Path(checkout).is_dir():
        # The overwhelming common case: only the machines that list coord-tui
        # in `repo_paths` have one. Absence is not a fault.
        return _result(
            Severity.OK,
            "not present on this machine",
            {
                "present": False,
                "checkout": str(checkout) if checkout else None,
                "pins": [],
                "drift_gate_steps": [],
            },
        )

    checkout = Path(checkout)
    short = shorten_path(str(checkout), str(ctx.home))
    pins = find_coord_pins(checkout)
    gate_steps = find_drift_gate_steps(checkout)
    values: dict = {
        "present": True,
        "checkout": str(checkout),
        "local_version": LOCAL_COORD_VERSION,
        "pins": pin_values(pins),
        "drift_gate_steps": [f"{wf}:{job}" for wf, job in gate_steps],
        "generated_artifacts": list(GENERATED_ARTIFACTS),
    }

    if not pins:
        return _result(
            Severity.WARN,
            "coord-tui CI installs no coord CLI",
            values,
            detail=(
                f"{short}/.github/workflows has no "
                f"`pip install {LIVE_DIST}[{SERVER_EXTRA}]` — the codegen drift gate "
                f"cannot import `coord.serve_app.openapi_spec()`, so nothing "
                f"compares {GENERATED_ARTIFACTS[0]} against this repo's wire "
                "schema. See docs/ADR_COORD_TUI_CI.md."
            ),
        )

    specs = ", ".join(f"{p.spec} ({_where(p)})" for p in pins)

    tombstoned = [p for p in pins if p.is_tombstone]
    if tombstoned:
        return _result(
            Severity.CRIT,
            f"coord-tui CI installs the dead distribution name: {specs}",
            values,
            detail=(
                f"`{TOMBSTONE_DIST}` is a permanent PyPI tombstone that will never gain "
                f"another release (#2106) — the drift gate is frozen on its last-ever "
                f"wire schema and will stay green through any change made here. "
                f"Rename to `{LIVE_DIST}[{SERVER_EXTRA}]` in "
                f"{', '.join(sorted({_where(p) for p in tombstoned}))}."
            ),
        )

    clientonly = [p for p in pins if not p.has_server_extra]
    if clientonly:
        return _result(
            Severity.CRIT,
            f"coord-tui CI installs a client-only coord: {specs}",
            values,
            detail=(
                f"`scripts/codegen.py --rust` imports `coord.serve_app`, which imports "
                f"Starlette at module scope — that lives behind the [{SERVER_EXTRA}] "
                f"extra since the base/server split (#1237), so the drift gate raises "
                f"ModuleNotFoundError instead of comparing anything. Add it in "
                f"{', '.join(sorted({_where(p) for p in clientonly}))}."
            ),
        )

    if not gate_steps:
        # The finding this check exists for: a healthy-looking install with
        # nothing downstream actually asserting the wire contract. Graded
        # below the CRITs above because those two break the job loudly on
        # its next run; THIS one is the silent variant — CI stays green
        # forever while the contract drifts.
        return _result(
            Severity.WARN,
            f"coord-tui CI runs no codegen drift gate (installs {specs})",
            values,
            detail=(
                f"no step in {short}/.github/workflows runs "
                "`scripts/codegen.py --rust --check`, so a field rename in "
                "coord/board_schema.py merged here would NOT turn coord-tui red — "
                f"{', '.join(GENERATED_ARTIFACTS[:2])} would just rot into a "
                "wrong-shape `/board` parse that blanks every panel "
                "(#632/#546/#628). See docs/ADR_COORD_TUI_CI.md."
            ),
        )

    gate_where = ", ".join(f"{wf}:{job}" for wf, job in gate_steps)

    exact = [p for p in pins if p.is_exact_pin]
    if exact:
        return _result(
            Severity.WARN,
            f"coord-tui CI exact-pins coord: {specs}",
            values,
            detail=(
                "docs/ADR_COORD_WEB_CI.md decided track-latest precisely because an "
                "exact pin rots silently — the same failure as ~/.coord-cli-venv found "
                "three releases stale on 2026-07-29. An exact-pinned drift gate proves "
                "coord-tui matches the coord it was frozen against, not the one the "
                f"fleet actually runs. Relax to a `>=` floor. Gate at {gate_where}."
            ),
        )

    floors = {p.floor for p in pins if p.floor}
    unsatisfiable = sorted(f for f in floors if floor_exceeds(f, LOCAL_COORD_VERSION))
    if unsatisfiable:
        return _result(
            Severity.WARN,
            f"coord-tui CI floor is ahead of this machine's coord: {specs}",
            values,
            detail=(
                f"floor {', '.join(unsatisfiable)} > local coord {LOCAL_COORD_VERSION} — "
                "either the floor names a release that is not out yet, or this machine "
                "is behind the coord coord-tui's drift gate proves its wire types "
                f"against. Gate at {gate_where}."
            ),
        )

    floor_note = f" (floor {', '.join(sorted(floors))})" if floors else ""
    return _result(
        Severity.OK,
        f"tracks latest{floor_note}: {specs}; drift gate at {gate_where}",
        values,
        detail=(
            f"local coord {LOCAL_COORD_VERSION}; source {short}; "
            f"guards {', '.join(GENERATED_ARTIFACTS)}"
        ),
    )
