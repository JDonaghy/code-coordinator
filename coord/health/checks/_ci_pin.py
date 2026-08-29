"""Shared machinery for the cross-repo "which `coord` does *their* CI install?" checks.

Extracted from :mod:`coord.health.checks.coord_web_ci_pin` (#2006) when #2900
added its second consumer, :mod:`coord.health.checks.coord_tui_ci_pin`. Both
answer the same question about a different downstream repo, so the parsing —
walk a checkout's workflow YAML, find the ``pip install`` steps, pull the
`coord` requirement out of each — is identical and lives here once. What is
*not* shared is the grading: what counts as a fault depends on why that repo
installs `coord` at all, and each check module owns its own verdicts.

Nothing in here is a check. The module is underscore-prefixed on purpose:
:func:`coord.health.registry.discover` skips ``_``-prefixed modules, so this
cannot accidentally register anything.

**Local-filesystem only.** Every function here reads a checkout already on
this machine. No ``gh`` call, no network — cheap enough for the health poll
tick, and absent on a machine with no such checkout, which each caller
reports as OK ("not present on this machine") the same way every other
checkout-derived lane reports absence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

#: The extra that carries uvicorn/Starlette. Since the base/`[server]` split
#: (#1237) the bare distribution installs a client-only coord: it cannot
#: serve anything, and it cannot even *import* ``coord.serve_app`` (Starlette
#: is imported at module scope there). Both downstream repos need the extra —
#: coord-web to boot ``coord web --fixture`` as its Playwright ``webServer``
#: (#1818), coord-tui because ``scripts/codegen.py --rust`` reads
#: ``coord.serve_app.openapi_spec()`` (#1941).
SERVER_EXTRA = "server"

#: PyPI cannot rename a project, so ``claude-coordinator`` is a permanent
#: tombstone that will never gain another release (#2106). CI still asking
#: for it does not get a stale coord — it gets whatever ancient version that
#: tombstone last published, forever.
TOMBSTONE_DIST = "claude-coordinator"

#: The live distribution name.
LIVE_DIST = "code-coordinator"

_INSTALL_RE = re.compile(r"\b(?:pip3?|uv\s+pip|pipx)\s+install\b", re.IGNORECASE)

# `code-coordinator[server]>=0.4.90` and friends. Deliberately tolerant of
# quoting (`'code-coordinator[server]'`) and of the underscore spelling pip
# normalises away, because this is reading someone else's hand-written YAML.
_REQ_RE = re.compile(
    r"(?P<dist>code[-_]coordinator|claude[-_]coordinator)"
    r"(?:\s*\[(?P<extras>[^\]]*)\])?"
    r"(?P<constraint>"
    r"(?:\s*(?:===|[<>!~=]=|[<>])\s*[^\s'\",;\\]+)"
    r"(?:\s*,\s*(?:===|[<>!~=]=|[<>])\s*[^\s'\",;\\]+)*"
    r")?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CoordPin:
    """One ``pip install <coord>`` requirement found in a downstream repo's CI."""

    workflow: str  # file name, e.g. "ci.yml"
    job: str  # job id, e.g. "e2e"
    spec: str  # the requirement as written, e.g. "code-coordinator[server]"
    dist: str  # normalised distribution name
    extras: tuple[str, ...] = ()
    constraint: str | None = None  # ">=0.4.90", "==0.4.90", or None

    @property
    def has_server_extra(self) -> bool:
        return SERVER_EXTRA in self.extras

    @property
    def is_tombstone(self) -> bool:
        return self.dist == TOMBSTONE_DIST

    @property
    def is_exact_pin(self) -> bool:
        """True for ``==``/``===``, the spec shape that rots silently.

        ``~=`` is deliberately included: ``~=0.4.90`` freezes the minor
        series just as effectively for a project on a single ``0.x`` line.
        """
        c = (self.constraint or "").lstrip()
        return c.startswith(("==", "===", "~="))

    @property
    def floor(self) -> str | None:
        """The ``>=`` version, when the spec declares one."""
        for part in (self.constraint or "").split(","):
            part = part.strip()
            if part.startswith(">="):
                return part[2:].strip()
        return None


def norm_dist(raw: str) -> str:
    """``Coord_TUI`` -> ``coord-tui``; ``code_coordinator`` -> ``code-coordinator``."""
    return raw.strip().lower().replace("_", "-")


def _version_tuple(s: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", s or "")
    return tuple(int(p) for p in parts) if parts else None


def release_tuple(s: str) -> tuple[int, ...] | None:
    """A comparable tuple, but only for a version that is a real release.

    ``coord.__version__`` falls back to ``0+unknown`` when it cannot resolve
    a distribution (a source checkout with no install, a wheel-less CI box).
    Comparing a CI floor against *that* would report every floor as "ahead of
    this machine" — a fabricated finding, and fabricating findings is how a
    check earns the right to be ignored. Unknown stays unknown.
    """
    if not s or "unknown" in s.lower():
        return None
    t = _version_tuple(s)
    return t if t and len(t) >= 2 else None


def floor_exceeds(floor: str, local: str) -> bool:
    """True iff *floor* is strictly newer than *local*, both being releases.

    Compares at equal precision — a floor of ``0.4`` is satisfied by a local
    ``0.4.91``, so the shorter tuple is padded rather than compared as-is
    (``(0, 4, 91) > (0, 4)`` would otherwise read as "ahead").
    """
    f, loc = release_tuple(floor), release_tuple(local)
    if not f or not loc:
        return False
    width = max(len(f), len(loc))

    def pad(t: tuple[int, ...]) -> tuple[int, ...]:
        return t + (0,) * (width - len(t))

    return pad(f) > pad(loc)


def iter_run_steps(checkout_path: Path) -> Iterator[tuple[str, str, str]]:
    """Yield ``(workflow_file_name, job_id, run_script)`` for every step.

    A sibling of :func:`coord.health.checks.toolchain._iter_workflow_steps`,
    but keeping the workflow/job labels: a finding here has to name *where*
    in someone else's repo to go fix it, or it is just as much of a mystery
    as the thing it replaced.
    """
    workflows_dir = checkout_path / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return
    try:
        files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    except OSError:
        return
    for wf_file in files:
        try:
            doc = yaml.safe_load(wf_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if isinstance(run, str) and run.strip():
                    yield wf_file.name, str(job_id), run


def find_coord_pins(checkout_path: Path) -> list[CoordPin]:
    """Every ``pip install <coord>`` requirement in the checkout's workflows.

    Only ``run:`` scripts that actually invoke an installer are scanned — a
    comment or an echo naming the package is not a pin, and treating it as
    one would grade prose.
    """
    found: list[CoordPin] = []
    seen: set[tuple[str, str, str]] = set()
    for workflow, job, run in iter_run_steps(checkout_path):
        for line in run.splitlines():
            if not _INSTALL_RE.search(line):
                continue
            for m in _REQ_RE.finditer(line):
                extras = tuple(
                    e.strip().lower() for e in (m.group("extras") or "").split(",") if e.strip()
                )
                constraint = (m.group("constraint") or "").strip() or None
                spec = m.group(0).strip()
                key = (workflow, job, spec)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    CoordPin(
                        workflow=workflow,
                        job=job,
                        spec=spec,
                        dist=norm_dist(m.group("dist")),
                        extras=extras,
                        constraint=constraint,
                    )
                )
    return found


def where(pin: CoordPin) -> str:
    """``ci.yml:e2e`` — the workflow:job a finding should send you to."""
    return f"{pin.workflow}:{pin.job}"


def pin_values(pins: list[CoordPin]) -> list[dict]:
    """The JSON-able ``values["pins"]`` payload both checks report."""
    return [
        {
            "workflow": p.workflow,
            "job": p.job,
            "spec": p.spec,
            "dist": p.dist,
            "extras": list(p.extras),
            "constraint": p.constraint,
        }
        for p in pins
    ]
