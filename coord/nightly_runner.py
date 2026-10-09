"""The nightly real-platform smoke runner's I/O shell (#3660).

#3652 shipped the pure decision core (:mod:`coord.nightly_smoke`): which
artifact to use, which host to run it on, what a result MEANS, and what to
do about it. Nothing in production called any of that — this module is the
caller. ``coord smoke nightly`` (``coord/commands/smoke.py``) is its only
CLI entry point; everything here is plain functions so the orchestration
itself is unit-testable without a fleet, a real build toolchain, or network
access (every I/O boundary — fetching a release tag, resolving a ref to a
sha, picking a host, running the acceptance driver — is an injectable
parameter with a real default).

**End to end, in order (#3660 Wanted):**

1. :func:`plan_nightly_run` — :func:`coord.nightly_smoke.
   resolve_artifact_plan` (build from the repo's integration branch —
   :attr:`coord.models.Repo.develop_branch`, the same field #934's
   develop+feature-branch model already defines — or download the latest
   release, :func:`coord.tui_release.fetch_latest_release_tag`), then
   :func:`coord.nightly_smoke.pick_nightly_host`, then — new in #3660 — the
   GUI-lane pre-flight (#3651, :mod:`coord.health.checks.
   gui_lane_preflight`), read straight off the chosen host's own live
   ``/health`` the same way :func:`coord.smoke._capability_probe_reasons`
   already does (#2096 "one question, one answer": no second, hand-rolled
   lock/trust check). A host that's locked, asleep, or missing AX/UIA trust
   comes back ``infra_blocked=True`` — never a plain app red (#3652's own
   rule, extended here to the host-pick step itself).
2. :func:`run_nightly_smoke` — obtains the artifact (:func:`obtain_artifact`,
   build or download per the plan), runs the spec through the existing
   acceptance drivers (:func:`coord.acceptance_drivers.run_driver`) on
   *this* host, and only on this host: #966 is this codebase's own settled
   answer to "a driver needs a capability this host lacks, but some OTHER
   configured machine has it" — fail loudly, name the right host, never
   silently run wrong hardware's answer. :func:`plan_nightly_run` already
   picked the right host; when that host isn't the one actually invoking
   this command, this reports it as plainly as #966 does rather than
   inventing new remote-exec plumbing to chase it there.
3. Each driver step becomes a :class:`coord.nightly_smoke.
   NightlyStepObservation`, classified (:func:`coord.nightly_smoke.
   classify_step`) against the spec's own ``known_bug:`` declarations
   (:func:`known_bugs_from_spec_text` — the "caller's spec parser" #3652's
   module docstring deferred to), and acted on
   (:func:`coord.nightly_smoke.process_nightly_step` — files/updates one
   issue per red step, alerts on a known-bug step going green, touches
   nothing on a clean pass) — EXCEPT a step the driver itself reported
   ``"unavailable"`` (#3510), which is never classified into a filing
   decision at all (:func:`run_nightly_smoke`'s loop — mirrors
   :mod:`coord.bugbash`'s own "#3510: an unavailable lane is not a bug
   finding", which skips the lane rather than filing anything from it).
   The lane acted through is resolved via :func:`coord.bugbash.
   discover_lanes` (:func:`_resolve_bugbash_lane`), not hand-built from
   ``plan.driver_kind`` — #2096 "one question, one answer": a route that
   sets ``label:``/``platforms:`` must get the SAME per-route dedupe label
   a real ``coord bugbash`` lane would (#3615/#3581).
4. Every step observed is persisted to :mod:`coord.nightly_store` BEFORE
   it is acted on, regardless of outcome, tagged with a per-run ``run_id``
   and the run's own ``steps_total`` — so ``coord release gate`` can read a
   repo's nightly artifacts without ``--from-json`` (#3660 acceptance), a
   later clean re-run can clear an earlier red/unavailable one at the same
   SHA (#3660 review round 1), and a run INTERRUPTED part-way through (the
   filing call raising, a Ctrl-C) can neither lose the red it already
   observed nor have its surviving partial rows read as a pass (#3660
   review round 2).
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from coord.acceptance_drivers import DriverResult, run_driver
from coord.bugbash import (
    GUI_LANE_DRIVER_KINDS,
    LANE_DRIVER_KINDS,
    BugbashLane,
    CoordRunner,
    discover_lanes,
    subprocess_coord_runner,
)
from coord.nightly_smoke import (
    ArtifactPlan,
    NightlyStepObservation,
    NightlyStepOutcome,
    StepVerdict,
    StepVerdictKind,
    classify_step,
    pick_nightly_host,
    process_nightly_step,
    resolve_artifact_plan,
)
from coord.nightly_store import (
    NightlyResultRecord,
    record_nightly_result,
    set_nightly_issue_number,
)

if TYPE_CHECKING:  # pragma: no cover - import-cycle-avoidance only
    import httpx

    from coord.config import AcceptanceDriverConfig, Config
    from coord.models import Board, Machine
    from coord.smoke import SmokeMachineChoice


class NightlyRunnerError(Exception):
    """Raised when there is nothing sensible to plan/run at all — a repo
    with no acceptance driver configured, an unknown repo. #2096 "a gate
    must be able to fail": a caller that asks for a plan this module
    cannot actually build must get a loud error, never a plan that silently
    describes doing nothing."""


# ── resolving which driver/branch this repo's nightly run uses ────────────


def _resolve_driver_cfg(config: "Config", repo: str, spec: str) -> "AcceptanceDriverConfig":
    driver_cfg = config.acceptance.driver_for(repo, spec) or config.acceptance.driver_for(repo)
    if driver_cfg is None:
        raise NightlyRunnerError(
            f"repo {repo!r} has no acceptance driver configured "
            "(add it under acceptance.drivers in coordinator.yml) — nothing "
            "for the nightly runner to run"
        )
    return driver_cfg


def _default_fetch_latest_release_tag(github_slug: str) -> str | None:
    from coord.tui_release import EmptyReleaseChannelError, fetch_latest_release_tag

    try:
        return fetch_latest_release_tag(repo=github_slug)
    except EmptyReleaseChannelError:
        # A real, definitive "this channel has never published a release" —
        # resolve_artifact_plan's own ValueError (no integration branch AND
        # no release tag) is the right failure to surface, not a swallowed
        # one here. Any OTHER exception (network/5xx/auth) is left to raise
        # as-is — that is a genuine "could not check", never "nothing
        # published yet" (mirrors fetch_latest_release_tag's own docstring).
        return None


def resolve_nightly_artifact_plan(
    *, repo: str, artifact: str, config: "Config",
    fetch_latest_release_tag_fn: Callable[[str], str | None] | None = None,
) -> ArtifactPlan:
    """#3660 Wanted #1: ``resolve_artifact_plan`` with its two I/O-bound
    inputs actually wired — the repo's own integration branch
    (:attr:`coord.models.Repo.develop_branch`), or (only when that's unset)
    the latest published release tag.

    Fetching the release tag is skipped entirely when an integration branch
    is configured — #3652's module docstring is explicit that
    ``resolve_artifact_plan`` prefers the integration branch, so paying for
    a network call whose answer can never be consulted would be pure waste
    on every repo that has one configured.
    """
    repo_cfg = config.repo(repo)
    if repo_cfg is None:
        raise NightlyRunnerError(f"repo {repo!r} is not declared in coordinator.yml")

    integration_branch = repo_cfg.develop_branch
    latest_release_tag = None
    if not integration_branch:
        fetch = fetch_latest_release_tag_fn or _default_fetch_latest_release_tag
        latest_release_tag = fetch(repo_cfg.github)

    return resolve_artifact_plan(
        repo=repo, artifact=artifact,
        integration_branch=integration_branch,
        latest_release_tag=latest_release_tag,
    )


# ── GUI-lane pre-flight, read off the chosen host's own live /health ──────


def _fetch_health(
    machine: "Machine", *, http_client: "httpx.Client | None" = None, timeout: float = 5.0,
) -> dict:
    import httpx

    from coord.dispatch import AGENT_PORT

    client = http_client or httpx
    try:
        resp = client.get(f"http://{machine.host}:{AGENT_PORT}/health", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError):
        # Same fail-skip posture as `coord.smoke._capability_probe_reasons`:
        # a connectivity hiccup here just skips the extra check — the
        # actual dispatch/run right after this is the real reachability
        # test and fails closed on its own if the host is truly down.
        return {}
    return data if isinstance(data, dict) else {}


def gui_lane_preflight_blockers(
    machine: "Machine", lane: str, *, http_client: "httpx.Client | None" = None,
    timeout: float = 5.0,
) -> list[str]:
    """CRIT ``gui_lane_preflight`` (#3651) reasons for *lane* on *machine*,
    read straight off its own live ``/health`` — empty when the lane is OK,
    unknown (no result at all, e.g. a pre-#3651 agent), or not a GUI lane
    at all.

    Reuses the SAME health-report shape :func:`coord.commands.status.
    _gui_lane_preflight_lines` already renders for ``coord doctor`` (#2096
    "one question, one answer" — no second, hand-rolled reading of
    ``health["health"]["results"]``). An ``unknown``/absent result is
    deliberately NOT a blocker here — #3651's own silence-gap note applies
    doubly for a nightly run: refusing every dispatch on missing telemetry
    (a pre-#3651 agent, a cold cache) would be strictly worse than running
    and letting the driver's own ``session_available()`` precheck (#3510)
    catch it the normal way.
    """
    health = _fetch_health(machine, http_client=http_client, timeout=timeout)
    results = (health.get("health") or {}).get("results") or []
    blockers: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("check_id") != "gui_lane_preflight" or r.get("subject") != lane:
            continue
        if r.get("severity") != "crit":
            continue
        headroom = r.get("headroom", "") or "gui_lane_preflight CRIT"
        detail = r.get("detail", "")
        blockers.append(f"{headroom} — {detail}" if detail else headroom)
    return blockers


# ── the plan (what --dry-run prints, and what a real run follows) ─────────


@dataclass(frozen=True)
class NightlyRunPlan:
    """Everything :func:`plan_nightly_run` decided, before anything is
    built/downloaded/run (#3660 acceptance: "``--dry-run`` prints the plan
    ... and changes nothing"). Also the input :func:`run_nightly_smoke`
    acts on for a real run."""

    repo: str
    artifact: str
    spec: str
    driver_kind: str
    source: str  # ArtifactSource.value: "build" | "download"
    ref: str
    detail: str
    host: "SmokeMachineChoice | None"
    host_rationale: str
    infra_blocked: bool
    infra_reason: str

    @property
    def machine_name(self) -> str | None:
        return self.host.machine.name if self.host is not None else None

    def render(self) -> str:
        lines = [
            f"nightly smoke plan: {self.repo} / {self.artifact} (spec={self.spec!r})",
            f"  driver: {self.driver_kind}",
            f"  artifact: {self.source} {self.ref!r} — {self.detail}",
        ]
        if self.host is None:
            lines.append("  host: NONE — no machine qualifies tonight")
        else:
            lines.append(f"  host: {self.machine_name} — {self.host_rationale}")
        if self.infra_blocked:
            lines.append(f"  INFRA BLOCKED: {self.infra_reason}")
        else:
            lines.append("  ready to run")
        return "\n".join(lines)


def plan_nightly_run(
    *, repo: str, artifact: str, spec: str, config: "Config", board: "Board",
    http_client: "httpx.Client | None" = None,
    fetch_latest_release_tag_fn: Callable[[str], str | None] | None = None,
) -> NightlyRunPlan:
    """#3660 Wanted #1/#2: resolve the artifact plan, pick a host, and run
    the GUI-lane pre-flight against it — pure decision-making over
    injectable I/O, so ``--dry-run`` and a real run both start from
    EXACTLY this (#2096 "one question, one answer": there is no second,
    looser plan a real run computes for itself)."""
    driver_cfg = _resolve_driver_cfg(config, repo, spec)
    artifact_plan = resolve_nightly_artifact_plan(
        repo=repo, artifact=artifact, config=config,
        fetch_latest_release_tag_fn=fetch_latest_release_tag_fn,
    )

    required_caps = [driver_cfg.capability] if driver_cfg.capability else []
    choice = pick_nightly_host(required_caps, repo, board, config, http_client=http_client)
    if choice is None:
        return NightlyRunPlan(
            repo=repo, artifact=artifact, spec=spec, driver_kind=driver_cfg.kind,
            source=artifact_plan.source.value, ref=artifact_plan.ref,
            detail=artifact_plan.detail, host=None, host_rationale="",
            infra_blocked=True,
            infra_reason=(
                f"no machine both declares {required_caps!r} for {repo!r} "
                "and passed its own live /health probe — nothing qualifies "
                "tonight"
            ),
        )

    infra_reason = ""
    if driver_cfg.kind in GUI_LANE_DRIVER_KINDS:
        blockers = gui_lane_preflight_blockers(
            choice.machine, driver_cfg.kind, http_client=http_client,
        )
        infra_reason = "; ".join(blockers)

    return NightlyRunPlan(
        repo=repo, artifact=artifact, spec=spec, driver_kind=driver_cfg.kind,
        source=artifact_plan.source.value, ref=artifact_plan.ref,
        detail=artifact_plan.detail, host=choice, host_rationale=choice.rationale,
        infra_blocked=bool(infra_reason), infra_reason=infra_reason,
    )


# ── known_bug: parsing — the "caller's spec parser" #3652 deferred to ─────


def known_bugs_from_spec_text(spec_text: str) -> dict[str, str]:
    """``{step_id: "<repo>#<N>"}`` for every step in *spec_text* (a smoke
    spec's raw YAML) that declares a ``known_bug:`` field.

    #3652's :func:`coord.nightly_smoke.classify_nightly_run` takes this map
    pre-resolved — "the smoke-spec's own ``known_bug:`` declarations,
    already resolved to a dict by the caller's spec parser" — but no step
    schema (:mod:`coord.tui_pty_driver`/:mod:`coord.win_native_driver`/
    :mod:`coord.mac_native_driver`/:mod:`coord.gtk_native_driver`) actually
    recognizes that field yet. Rather than teach four separate (two of them
    additive-only-sealed) parsers a field only this one caller needs, this
    reads the same YAML a SECOND time, generically — tolerant of any shape
    (a non-mapping document, a missing/non-list ``steps:``, a step with no
    ``id``/``name``) by returning less, never raising or guessing: a
    malformed spec still runs every step through the real driver; it just
    can't suppress alerts for the undeclared ones.
    """
    import yaml

    try:
        data = yaml.safe_load(spec_text)
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    steps = data.get("steps")
    if not isinstance(steps, list):
        return {}

    out: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        ref = step.get("known_bug")
        step_id = step.get("id") or step.get("name")
        if not ref or not step_id:
            continue
        out[str(step_id)] = str(ref)
    return out


# ── running the spec, and turning its output into observations ────────────


def observations_from_driver_result(
    result: DriverResult, *, repo: str, spec: str, sha: str, checked_at: float,
) -> list[tuple[NightlyStepObservation, bool]]:
    """Every :class:`coord.nightly_smoke.NightlyStepObservation`
    :func:`coord.acceptance_drivers.run_driver`'s result describes, paired
    with whether that step reported the driver's own ``"unavailable"``
    status (#3510 — a locked/absent GUI session or missing display, not an
    app bug) — kept alongside the observation rather than folded into it
    because :class:`NightlyStepObservation` (#3652) has no ``unavailable``
    field of its own; :mod:`coord.nightly_store`'s ``NightlyResultRecord``
    is where that distinction is actually persisted.

    A driver result with NO tests at all (the run command crashed before
    producing any, or ran and exited 0 without ever emitting a parseable
    report; see :class:`coord.acceptance_drivers.DriverResult`'s own ``ok``
    caveat) yields ONE FAILING observation naming the whole spec as the
    "step" — #2096 "a gate must be able to fail": a run that produced zero
    structured output must never read as "zero steps, therefore nothing
    failed," REGARDLESS of ``exit_code`` (#3660 review round 1: the
    previous ``passed=result.ok`` let a ``run:`` wrapper that exits 0
    without writing a report, or a native spec whose ``steps:`` is empty,
    certify the artifact with zero steps ever verified).
    """
    if not result.tests:
        return [(
            NightlyStepObservation(
                repo=repo, spec=spec, step="(spec)", sha=sha, passed=False,
                checked_at=checked_at,
                detail=(
                    "driver produced no structured test results at all "
                    f"(exit_code={result.exit_code}) — a run with zero "
                    "observed steps can never certify this artifact"
                ),
                evidence=((result.raw_output[-2000:],) if result.raw_output else ()),
            ),
            False,
        )]

    out: list[tuple[NightlyStepObservation, bool]] = []
    for t in result.tests:
        status = t.get("status")
        unavailable = status == "unavailable"
        evidence: list[str] = []
        if t.get("capture_b64"):
            evidence.append("capture_b64 attached (see driver log)")
        out.append((
            NightlyStepObservation(
                repo=repo, spec=spec, step=str(t.get("id", "")), sha=sha,
                passed=status == "pass", checked_at=checked_at,
                detail=str(t.get("message", "")), evidence=tuple(evidence),
            ),
            unavailable,
        ))
    return out


def run_nightly_spec(
    plan: NightlyRunPlan, *, config: "Config", cwd: str, sha: str,
    now: float | None = None,
    run_driver_fn: "Callable[..., DriverResult] | None" = None,
) -> list[tuple[NightlyStepObservation, bool]]:
    """#3660 Wanted #3: run *plan*'s spec through the existing acceptance
    drivers (:func:`coord.acceptance_drivers.run_driver`) in *cwd* — the
    obtained artifact's checkout — and build one observation per step.

    *run_driver_fn* defaults to the real :func:`coord.acceptance_drivers.
    run_driver` — injectable so a test can exercise the observation/
    classify/act pipeline without actually launching a native driver.
    """
    driver_cfg = _resolve_driver_cfg(config, plan.repo, plan.spec)
    entrypoint = plan.spec or driver_cfg.entrypoint
    fn = run_driver_fn or run_driver
    result = fn(
        driver_cfg.kind, driver_cfg.run, cwd,
        setup_command=driver_cfg.setup, entrypoint=entrypoint,
    )
    checked_at = time.time() if now is None else now
    return observations_from_driver_result(
        result, repo=plan.repo, spec=entrypoint, sha=sha, checked_at=checked_at,
    )


# ── obtaining the artifact (build, or download) ────────────────────────────


def _git_ref_for_plan(plan: "NightlyRunPlan") -> str:
    """The actual branch/tag name to ``git ls-remote``/``git clone`` for
    *plan*, on GitHub.

    A BUILD plan's ``ref`` is a branch name — used verbatim. A DOWNLOAD
    plan's ``ref`` is deliberately the BARE version
    :func:`coord.tui_release.fetch_latest_release_tag` returns (see that
    function's own docstring: "coord-tui's newest published release, as a
    bare version (no ``v``)") — the real git tag on GitHub carries a ``v``
    prefix, which :func:`coord.tui_release.fetch_release_assets` already
    adds back itself before querying the Releases API. ``git ls-remote``/
    ``git clone`` have no such normalization of their own, so without this
    they look for a tag that doesn't exist and the run dies on "could not
    resolve ... to a commit sha" (#3660 review round 1).
    """
    from coord.nightly_smoke import ArtifactSource

    if plan.source == ArtifactSource.DOWNLOAD.value and not plan.ref.startswith("v"):
        return f"v{plan.ref}"
    return plan.ref


def _default_resolve_ref_sha(github_slug: str, ref: str, *, timeout: float = 15.0) -> str:
    """The commit *ref* resolves to on ``github.com/<github_slug>``, via a
    plain ``git ls-remote`` — no GitHub API token needed, and no local
    checkout required.

    *ref* must already be the REAL git ref name — a branch for a BUILD
    plan, or a ``v``-prefixed tag for a DOWNLOAD plan (see
    :func:`_git_ref_for_plan`, which every production caller applies
    before calling this; #3660 review round 1 — this function used to
    receive the plan's bare, un-prefixed version string for a DOWNLOAD
    plan and could never resolve it).

    Raises :class:`NightlyRunnerError` when *ref* doesn't resolve at all —
    #2096 "a gate must be able to fail": persisting a result against a
    guessed/empty sha would make the release gate's own sha-equality check
    silently useless.
    """
    url = f"https://github.com/{github_slug}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, ref],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NightlyRunnerError(
            f"could not resolve {github_slug}@{ref} to a commit sha: {exc}"
        ) from exc
    stdout = result.stdout.strip()
    if result.returncode != 0 or not stdout:
        raise NightlyRunnerError(
            f"could not resolve {github_slug}@{ref} to a commit sha "
            f"(git ls-remote exit {result.returncode}): "
            f"{result.stderr.strip() or 'no matching ref'}"
        )
    return stdout.splitlines()[0].split()[0]


@dataclass(frozen=True)
class ObtainedArtifact:
    """Where :func:`obtain_artifact` put things — *cwd* is where the repo's
    spec/harness live (a checkout at *sha*); *binary_path* is set only for
    a DOWNLOAD plan, naming the release asset actually downloaded (a BUILD
    plan's binary is wherever the repo's own ``build_command`` leaves it,
    inside *cwd*)."""

    cwd: str
    binary_path: str | None = None


def obtain_artifact(
    plan: NightlyRunPlan, *, config: "Config", workdir: str,
    build_fn: "Callable[..., ObtainedArtifact] | None" = None,
    download_fn: "Callable[..., ObtainedArtifact] | None" = None,
) -> ObtainedArtifact:
    """#3660 Wanted #1: materialize *plan*'s artifact — build from the
    integration branch, or download the latest release — in *workdir*.

    Both the BUILD and DOWNLOAD paths are injectable (*build_fn*/
    *download_fn*) with real production defaults: BUILD does a bare
    ``git clone --branch <ref> --depth 1`` into *workdir* and runs the
    repo's own ``build_command`` there; DOWNLOAD clones the same way (the
    spec/harness files live in the repo, not in a release asset) and
    additionally fetches+downloads the named *plan.artifact* release asset
    (:func:`coord.tui_release.fetch_release_assets`/``download_asset``)
    alongside it. Neither default path is exercised by this module's own
    test suite (no fixture fleet / real git remote in CI) — they're real,
    minimal, and meant to be overridden by a caller with a faster/cached
    checkout strategy; the decision of WHICH path to take
    (:func:`resolve_nightly_artifact_plan`) is what's actually tested.
    """
    from coord.nightly_smoke import ArtifactSource

    if plan.source == ArtifactSource.BUILD.value:
        fn = build_fn or _default_build_artifact
    else:
        fn = download_fn or _default_download_artifact
    return fn(plan, config=config, workdir=workdir)


def _github_clone_url(github_slug: str) -> str:
    return f"https://github.com/{github_slug}.git"


def _git_clone_ref(remote_url: str, ref: str, workdir: str, *, timeout: float = 600.0) -> None:
    """Make *workdir* a checkout of *remote_url* at *ref*.

    A fresh ``git clone --depth 1 --branch <ref>`` the first time; a
    ``fetch``+``checkout``+``clean`` of the SAME checkout on every
    subsequent call for the same *workdir* (#3660 review round 1: a bare
    ``git clone`` refuses a non-empty destination, and
    :func:`_default_workdir` deliberately returns the SAME path every
    night for a given repo so an operator never has to configure a fresh
    one — before this, a second-ever nightly run for any repo died on
    exactly that). Detecting "already a checkout of this repo" by the
    presence of ``workdir/.git`` is enough here: *workdir* is this
    module's own private, per-repo scratch directory
    (``<coord-dir>/nightly_workdirs/<repo>`` — never shared with, or
    pointed at, anything else), so finding a ``.git`` there always means
    "a previous run of THIS function left it," never some unrelated
    checkout this function should refuse to touch.

    Takes the remote URL directly (production builds it via
    :func:`_github_clone_url`) rather than a bare GitHub slug — so a test
    can exercise the real ``git`` reuse logic above against a local
    ``file://`` remote, with no network and no GitHub dependency.
    """
    git_dir = Path(workdir) / ".git"
    if git_dir.is_dir():
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", ref],
            cwd=workdir, check=True, timeout=timeout,
        )
        subprocess.run(
            ["git", "checkout", "--force", "FETCH_HEAD"],
            cwd=workdir, check=True, timeout=timeout,
        )
        subprocess.run(
            ["git", "clean", "-fdx"], cwd=workdir, check=True, timeout=timeout,
        )
        return
    Path(workdir).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--branch", ref, "--depth", "1", remote_url, workdir],
        check=True, timeout=timeout,
    )


def _default_build_artifact(
    plan: NightlyRunPlan, *, config: "Config", workdir: str,
) -> ObtainedArtifact:
    repo_cfg = config.repo(plan.repo)
    if repo_cfg is None:
        raise NightlyRunnerError(f"repo {plan.repo!r} is not declared in coordinator.yml")
    _git_clone_ref(_github_clone_url(repo_cfg.github), _git_ref_for_plan(plan), workdir)
    if repo_cfg.build_command:
        subprocess.run(repo_cfg.build_command, shell=True, cwd=workdir, check=True)
    return ObtainedArtifact(cwd=workdir)


def _default_download_artifact(
    plan: NightlyRunPlan, *, config: "Config", workdir: str,
) -> ObtainedArtifact:
    from coord.tui_release import download_asset, fetch_release_assets

    repo_cfg = config.repo(plan.repo)
    if repo_cfg is None:
        raise NightlyRunnerError(f"repo {plan.repo!r} is not declared in coordinator.yml")
    # The spec/harness files this run drives live in the repo itself, never
    # in a release asset — clone at the release tag so the harness matches
    # exactly what shipped, same as the BUILD path does for its branch.
    # `_git_ref_for_plan` adds back the `v` prefix `plan.ref` deliberately
    # lacks (see that function's docstring) — `git clone`/`git fetch` need
    # the real tag name, unlike `fetch_release_assets` just below, which
    # adds the prefix itself and wants the bare version.
    _git_clone_ref(_github_clone_url(repo_cfg.github), _git_ref_for_plan(plan), workdir)
    assets = fetch_release_assets(plan.ref, repo=repo_cfg.github)
    matches = [a for a in assets if plan.artifact in a.name]
    if not matches:
        have = sorted(a.name for a in assets)
        raise NightlyRunnerError(
            f"release {plan.ref!r} of {repo_cfg.github!r} has no asset "
            f"matching {plan.artifact!r} (have: {have})"
        )
    # `download_asset` is `coord.tui_release.download_asset(url, dest_dir:
    # Path)` — it immediately calls `dest_dir.mkdir(...)`, so passing the
    # bare `workdir` str here raised `AttributeError` on every download run
    # (#3660 review round 1).
    binary_path = download_asset(matches[0].download_url, Path(workdir))
    return ObtainedArtifact(cwd=workdir, binary_path=str(binary_path))


# ── resolving the acting BugbashLane through the one real answerer ────────


def _resolve_bugbash_lane(
    config: "Config", repo: str, driver_cfg: "AcceptanceDriverConfig",
    machine_name: str, *, http_client: "httpx.Client | None" = None,
) -> tuple[BugbashLane, str]:
    """The :class:`~coord.bugbash.BugbashLane` a nightly red step should
    file/comment/close through — resolved via :func:`coord.bugbash.
    discover_lanes`, the SAME lane discovery ``coord bugbash`` itself uses,
    rather than hand-built from ``plan.driver_kind`` (#3660 review round 1:
    hand-building re-opened #3615's dedupe collision — a route that sets
    ``label:``/``platforms:`` on its :class:`~coord.config.
    AcceptanceDriverConfig` got a lane whose ``platform`` was just the bare
    ``driver_kind``, identical to every sibling route sharing that kind,
    e.g. vimcode's ``win-gui``/``win-terminal`` routes — both ``kind:
    win-native`` — folding two distinct platform findings into one issue).

    Matched on the ROUTE — ``(driver_kind, capability, setup, run)`` —
    and NOT on the machine (#3660 review round 2). ``setup``/``run`` (the
    route's own provisioning/launch strings, carried onto
    :attr:`~coord.bugbash.BugbashLane.setup`/:attr:`~coord.bugbash.
    BugbashLane.launch_command`) are what disambiguate two sibling routes
    sharing a kind — exactly the ``win-gui``/``win-terminal`` case, which
    ``discover_lanes`` has no OTHER way to tell apart from a resolved
    :class:`BugbashLane` alone (it does not echo back which ``routes:``
    entry produced each lane). The machine is then overwritten with
    *machine_name* — the host :func:`pick_nightly_host` actually chose —
    rather than used as a match key, because the two machine pickers are
    deliberately different questions and routinely disagree:
    ``discover_lanes`` takes the FIRST configured machine claiming the
    capability (:func:`coord.bugbash._pick_lane_machine`, no pause/cordon
    filter), while :func:`pick_nightly_host` ranks idle-first and filters
    cordoned/paused hosts. Keying the match on ``lane.machine ==
    machine_name`` meant that any such disagreement — a capability claimed
    by two machines where the first-configured one is busy or cordoned —
    silently fell through to the hand-built lane, i.e. back to the exact
    colliding ``platform=driver_kind`` label this function exists to
    prevent.

    Returns ``(lane, fallback_reason)``. *fallback_reason* is ``""`` on a
    real route match, and otherwise NAMES why a hand-built lane
    (``platform=driver_kind``, no ``setup``/``launch_command``) was used —
    surfaced in :class:`NightlyRunReport`/``coord smoke nightly``'s own
    output rather than happening silently. The common, benign case is a
    *driver_cfg* whose kind isn't one of :data:`coord.bugbash.
    LANE_DRIVER_KINDS` at all (e.g. ``cli-pytest``, which has no bugbash
    lane concept to begin with) — hence a usable fallback rather than a
    crash on the first nightly repo that isn't ALSO a ``coord bugbash``
    target.
    """
    import dataclasses

    lanes = list(discover_lanes(config, repo, http_client=http_client))
    for lane in lanes:
        if (
            lane.driver_kind == driver_cfg.kind
            and lane.capability == driver_cfg.capability
            and lane.setup == driver_cfg.setup
            and lane.launch_command == driver_cfg.run
        ):
            if lane.machine == machine_name:
                return lane, ""
            return dataclasses.replace(lane, machine=machine_name), ""
    reason = (
        f"no coord-bugbash lane matches this route (kind={driver_cfg.kind!r}, "
        f"capability={driver_cfg.capability!r}) — using a hand-built lane "
        f"labelled {driver_cfg.kind!r}"
        + (
            "; it is not one of coord.bugbash's lane driver kinds, which is "
            "expected for a non-GUI driver"
            if driver_cfg.kind not in LANE_DRIVER_KINDS
            else f"; discover_lanes offered {sorted(x.platform for x in lanes)!r}"
        )
    )
    return BugbashLane(
        platform=driver_cfg.kind, driver_kind=driver_cfg.kind,
        machine=machine_name, capability=driver_cfg.capability,
    ), reason


# ── the full orchestration ─────────────────────────────────────────────────


def observation_key(obs: NightlyStepObservation) -> str:
    """``"<spec>::<step>"`` — the ONE formatter for the key
    :attr:`NightlyRunReport.unavailable_steps` holds, so the runner that
    writes it, the report property that reads it, and ``coord smoke
    nightly``'s renderer can never disagree about its shape (#2096 "one
    question, one answer")."""
    return f"{obs.spec}::{obs.step}"


def step_key(outcome: NightlyStepOutcome) -> str:
    """:func:`observation_key` for the observation behind *outcome*."""
    return observation_key(outcome.verdict.observation)


@dataclass(frozen=True)
class NightlyRunReport:
    """What :func:`run_nightly_smoke` actually did (#3660 acceptance:
    distinguishing a green run, a red run, an INFRA-blocked run, and a
    dry-run preview is the whole point of this type existing).
    """

    plan: NightlyRunPlan
    ran: bool
    sha: str = ""
    outcomes: tuple[NightlyStepOutcome, ...] = ()
    #: ``"<spec>::<step>"`` for every step the DRIVER itself reported as
    #: ``"unavailable"`` (#3510 — a locked/absent GUI session, a missing
    #: display). Such a step deliberately takes no filing action at all
    #: (:func:`run_nightly_smoke`'s loop), so without this channel it was
    #: indistinguishable, in this report and in ``coord smoke nightly``'s
    #: output and exit code, from a clean green — the exact #3566 defect
    #: ("a lane that never ran read identically to a clean pass") that
    #: :func:`coord.bugbash.run_bugbash` answers with its own
    #: ``lanes_unavailable``.
    unavailable_steps: tuple[str, ...] = ()
    #: Non-empty when the acting :class:`~coord.bugbash.BugbashLane` could
    #: not be resolved through :func:`coord.bugbash.discover_lanes` and a
    #: hand-built one was used instead — see :func:`_resolve_bugbash_lane`.
    #: Reported rather than silent (#3660 review round 2) because for a
    #: route carrying ``label:``/``platforms:`` the fallback's bare
    #: ``driver_kind`` platform label is the #3615 dedupe collision.
    lane_fallback: str = ""

    @property
    def infra_blocked(self) -> bool:
        return self.plan.infra_blocked

    @property
    def any_dropped(self) -> bool:
        """#2096: an alerting red step that got no issue, no comment, and
        no close — never silently folded into "nothing to report"."""
        return any(o.dropped for o in self.outcomes)

    @property
    def any_unavailable(self) -> bool:
        """Whether any step never actually ran (driver-reported
        ``unavailable``, #3510) — "nothing was observed here" rather than
        "everything observed was fine"."""
        return bool(self.unavailable_steps)

    @property
    def any_app_red(self) -> bool:
        """Whether the run OBSERVED a genuine app failure needing a bug
        (:attr:`~coord.nightly_smoke.StepVerdictKind.RED_NEEDS_FILING`) —
        true even when every such step was successfully filed/commented,
        which is precisely the state ``coord smoke nightly``'s exit code
        must not fold into success.

        An ``unavailable`` step is deliberately NOT an app red, even though
        :func:`~coord.nightly_smoke.classify_step` (which has no notion of
        the driver's ``unavailable`` status) labels its verdict
        ``RED_NEEDS_FILING``: #3510's rule is that a locked/absent session
        is an environment condition, and :func:`run_nightly_smoke` already
        keeps such a step away from every filing decision for that reason.
        """
        unavailable = set(self.unavailable_steps)
        return any(
            o.verdict.kind is StepVerdictKind.RED_NEEDS_FILING
            and step_key(o) not in unavailable
            for o in self.outcomes
        )


def run_nightly_smoke(
    *, repo: str, artifact: str, spec: str, config: "Config", board: "Board",
    dry_run: bool = True,
    http_client: "httpx.Client | None" = None,
    fetch_latest_release_tag_fn: Callable[[str], str | None] | None = None,
    resolve_ref_sha_fn: Callable[[str, str], str] | None = None,
    local_machine_name_fn: Callable[["Config"], str | None] | None = None,
    obtain_artifact_fn: Callable[..., ObtainedArtifact] | None = None,
    run_driver_fn: "Callable[..., DriverResult] | None" = None,
    workdir: str | None = None,
    open_issues: Sequence[dict] = (),
    closed_issues: Sequence[dict] = (),
    runner: CoordRunner | None = None,
    now: float | None = None,
    run_id: str | None = None,
) -> NightlyRunReport:
    """The whole #3660 pipeline: plan, (maybe) obtain + run + classify +
    act + persist.

    *dry_run* (default ``True`` — same safe-by-default posture
    :func:`coord.nightly_smoke.process_nightly_step` already has):
    computes and returns the plan WITHOUT building/downloading/running
    anything, filing nothing, and persisting nothing (#3660 acceptance:
    "``--dry-run`` prints the plan ... and changes nothing"). The classify
    + act step still runs its OWN dry-run preview internally when invoked
    with real observations (``action="would-file"`` etc.) — this top-level
    *dry_run* short-circuits even earlier, before anything is observed at
    all, since there is nothing to classify yet.

    An INFRA-blocked plan (no qualifying host, or a GUI-lane pre-flight
    failure) never runs or files anything — #3652's own rule ("a host
    that's locked/asleep/missing trust is an INFRA result, never an app
    red") extended to mean it never runs the spec or acts on a fabricated
    result. It DOES persist one ``unavailable=True`` record (#3660 review,
    non-blocking item 2) so ``coord release gate`` reports the distinct
    "unavailable at ... unlock the host and re-run" verdict instead of "no
    nightly result recorded at all" for a host that genuinely was checked
    and found locked.

    *run_id* (default: a fresh :func:`uuid.uuid4` hex string) tags every
    :class:`~coord.nightly_store.NightlyResultRecord` this call persists —
    the grouping key that lets a later clean run clear an earlier
    red/unavailable one at the same ``(artifact, sha)`` (#3660 review
    round 1, :mod:`coord.nightly_store`). Injectable so a test can assert
    on a deterministic value; production leaves it to generate its own.
    """
    plan = plan_nightly_run(
        repo=repo, artifact=artifact, spec=spec, config=config, board=board,
        http_client=http_client, fetch_latest_release_tag_fn=fetch_latest_release_tag_fn,
    )
    effective_run_id = run_id or uuid.uuid4().hex
    if dry_run or plan.infra_blocked:
        if not dry_run and plan.infra_blocked:
            _persist_infra_unavailable(
                plan, repo=repo, artifact=artifact, config=config,
                resolve_ref_sha_fn=resolve_ref_sha_fn, now=now, run_id=effective_run_id,
            )
        return NightlyRunReport(plan=plan, ran=False)

    assert plan.host is not None  # infra_blocked is False -> a host was chosen

    resolve_local = local_machine_name_fn or _default_local_machine_name
    local_name = resolve_local(config)
    if local_name != plan.host.machine.name:
        # #966's own settled answer, extended to this seam: `coord smoke
        # nightly` has no remote-exec plumbing of its own (and growing one
        # here, alongside #966's still-open gap for `coord acceptance
        # run`/`record`, would be a second, independently-drifting
        # implementation of the exact same "run this on that other host"
        # problem). Fail loudly, name the right host, change nothing.
        raise NightlyRunnerError(
            f"plan_nightly_run picked {plan.host.machine.name!r} to run "
            f"{repo!r}'s nightly spec, but this is "
            f"{local_name or '(an unrecognized host)'} — capability-matched "
            "remote routing isn't implemented yet (mirrors #966's own "
            "`coord acceptance run`/`record` gap); run this command on "
            f"{plan.host.machine.name!r} directly."
        )

    resolve_sha = resolve_ref_sha_fn or _default_resolve_ref_sha
    repo_cfg = config.repo(repo)
    if repo_cfg is None:  # pragma: no cover - plan_nightly_run already proved this exists
        raise NightlyRunnerError(f"repo {repo!r} is not declared in coordinator.yml")
    sha = resolve_sha(repo_cfg.github, _git_ref_for_plan(plan))

    effective_workdir = workdir or _default_workdir(repo)
    # #3660 review: every real failure mode of these two I/O-heavy steps
    # must reach the operator as the `exit 2` every other one produces, not
    # as a bare traceback — `git clone`/`build_command` raise
    # `subprocess.CalledProcessError`, `fetch_release_assets` raises
    # `httpx` errors, and `run_driver` raises `DriverError` for a timeout,
    # an unsupported kind, or a failing `setup:`. The CLI catches only
    # `NightlyRunnerError`, so they are translated here (at the one seam
    # that knows WHICH step was in flight) rather than by widening the
    # CLI's except clause to bare `Exception`.
    try:
        obtained = (obtain_artifact_fn or obtain_artifact)(
            plan, config=config, workdir=effective_workdir,
        )
    except NightlyRunnerError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised, never swallowed
        raise NightlyRunnerError(
            f"could not obtain {artifact!r} for {repo!r} "
            f"({plan.source} {plan.ref!r}) in {effective_workdir}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        observations = run_nightly_spec(
            plan, config=config, cwd=obtained.cwd, sha=sha, now=now,
            run_driver_fn=run_driver_fn,
        )
    except NightlyRunnerError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised, never swallowed
        raise NightlyRunnerError(
            f"could not run {repo!r}'s nightly spec "
            f"({plan.driver_kind} driver, spec={plan.spec or '(driver entrypoint)'}): "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    spec_path_for_known_bugs = _read_spec_text(obtained.cwd, observations)
    known_bugs_raw = known_bugs_from_spec_text(spec_path_for_known_bugs) if spec_path_for_known_bugs else {}

    # #3652's `file_finding` raises when asked to actually file (not a
    # dry-run, not a DUPLICATE) with no `BugbashLane` — it needs
    # `lane.machine` to queue the filed issue (`coord drive-queue add
    # ... --machine <lane.machine>`). `plan_nightly_run` already resolved
    # exactly this host/driver pairing; a real `BugbashLane` built from it
    # here means a brand-new red step files through the SAME path (and the
    # SAME queue-onto-a-machine step) every bugbash finding already does
    # (#2096 "one question, one answer"), instead of `process_nightly_step`
    # silently defaulting to `lane=None` and this crashing on the first
    # genuinely new finding a real run ever produces.
    driver_cfg = _resolve_driver_cfg(config, repo, spec)
    lane, lane_fallback = _resolve_bugbash_lane(
        config, repo, driver_cfg, plan.host.machine.name, http_client=http_client,
    )

    checked_at = time.time() if now is None else now
    steps_total = len(observations)
    outcomes: list[NightlyStepOutcome] = []
    unavailable_steps: list[str] = []
    for obs, unavailable in observations:
        # PERSIST BEFORE ACTING (#3660 review round 2). The row records
        # what the DRIVER observed, which is already true regardless of
        # whether the acting step below succeeds — and acting first was a
        # false-green: `process_nightly_step` -> `file_finding` ->
        # `subprocess_coord_runner` RAISES on any non-zero `coord` exit (as
        # does a `coord issue create` whose output doesn't parse), nothing
        # here catches it, and a 2-step run whose second step was a new red
        # then left the store holding only its first, PASSING row. Grouped
        # per run, that reduced to "1 step(s) passed" with a fresh
        # timestamp and outranked the complete red run at the same SHA.
        # `steps_total` is the second half of the fix: it is known HERE,
        # before the loop, so the store can refuse to grade a group holding
        # fewer rows than the run promised (see coord.nightly_store).
        record_nightly_result(NightlyResultRecord(
            repo=repo, artifact=artifact, sha=sha, passed=obs.passed,
            checked_at=checked_at, detail=obs.detail, unavailable=unavailable,
            spec=obs.spec, step=obs.step, host=plan.host.machine.name,
            evidence=obs.evidence, run_id=effective_run_id,
            steps_total=steps_total,
        ))
        verdict: StepVerdict = classify_step(obs, known_bugs_raw.get(obs.step))
        if unavailable:
            # #3510, mirroring coord.bugbash's own "#3510: an unavailable
            # lane is not a bug finding" (`run_bugbash` skips the lane for
            # the round rather than running the checklist against it or
            # filing anything from it): a driver-reported `unavailable`
            # step is an environment condition — a locked/absent GUI
            # session or missing display — never an app bug. It is still
            # recorded below (so the store/gate can report it distinctly,
            # #3510), but it never reaches `process_nightly_step` at all
            # (#3660 review round 1 — before this fix it classified as a
            # plain `RED_NEEDS_FILING` red and filed/commented an app-repo
            # bug for a locked host).
            outcome = NightlyStepOutcome(verdict=verdict, action="none")
            # ...but it must not read like a clean green in the runner's
            # own output/exit code either (#3566: "a lane that never ran
            # read identically to a clean pass"), which is why the report
            # carries it as a named, separate channel — `coord.bugbash`
            # reports `lanes_unavailable` for exactly this reason.
            unavailable_steps.append(observation_key(obs))
        else:
            outcome = process_nightly_step(
                verdict, open_issues=open_issues, closed_issues=closed_issues,
                lane=lane, runner=runner or subprocess_coord_runner, dry_run=dry_run,
                platform=lane.platform,
            )
            # #3661: anneal the filed/updated issue number onto the row
            # `record_nightly_result` already wrote above, best-effort —
            # the status surface (`coord.nightly_status`) wants "which
            # issue(s)" for a red result. Never required for correctness:
            # the row's own pass/fail/unavailable verdict was already
            # durable before this acting step ever ran (#3660 review round
            # 2's crash-safety property is untouched by this), so losing
            # this annotation to a crash here only costs one status-surface
            # link, never the gate's own verdict.
            if outcome.issue_number is not None:
                set_nightly_issue_number(
                    repo=repo, run_id=effective_run_id, spec=obs.spec,
                    step=obs.step, issue_number=outcome.issue_number,
                )
        outcomes.append(outcome)

    return NightlyRunReport(
        plan=plan, ran=True, sha=sha, outcomes=tuple(outcomes),
        unavailable_steps=tuple(unavailable_steps), lane_fallback=lane_fallback,
    )


def _persist_infra_unavailable(
    plan: NightlyRunPlan, *, repo: str, artifact: str, config: "Config",
    resolve_ref_sha_fn: Callable[[str, str], str] | None, now: float | None, run_id: str,
) -> None:
    """Persist ONE ``unavailable=True`` record for an INFRA-blocked *plan*
    (#3660 review, non-blocking item 2).

    Before this, the pre-flight-blocked path persisted nothing at all —
    ``coord release gate`` then read "no nightly real-platform smoke
    result recorded," indistinguishable from a repo nobody has ever run,
    rather than the distinctly-labelled "unavailable at ... unlock the
    host and re-run" verdict that exists for exactly this case
    (:func:`coord.release_gate._nightly_artifact_step`).

    Best-effort: when the sha can't even be resolved (no network, or a
    genuinely bad ref), there is nothing meaningful to persist against —
    this silently does nothing rather than raising, since the plan's own
    ``render()``/``infra_reason`` already told the operator what's wrong,
    and a dry-run-adjacent INFRA short-circuit failing loudly on a
    SEPARATE, best-effort bookkeeping step would be a worse failure mode
    than just not persisting.

    This record can never DOWNGRADE an already-verified artifact: a
    never-ran ``unavailable`` row is dropped at read time when a complete
    passing run exists for the same ``(artifact, sha)``
    (:func:`coord.nightly_store.nightly_artifact_results_for_release_gate`,
    #3660 review round 2) — so a laptop that happens to be locked tonight
    does not flip a PASSing ``nightly:<artifact>`` for a SHA that was
    already fully observed, while still blocking a SHA that wasn't.
    """
    repo_cfg = config.repo(repo)
    if repo_cfg is None:  # pragma: no cover - plan_nightly_run already proved this exists
        return
    resolve_sha = resolve_ref_sha_fn or _default_resolve_ref_sha
    try:
        sha = resolve_sha(repo_cfg.github, _git_ref_for_plan(plan))
    except NightlyRunnerError:
        return
    checked_at = time.time() if now is None else now
    record_nightly_result(NightlyResultRecord(
        repo=repo, artifact=artifact, sha=sha, passed=False,
        checked_at=checked_at, detail=plan.infra_reason, unavailable=True,
        spec=plan.spec, step="(preflight)", host=plan.machine_name or "",
        evidence=(), run_id=run_id,
        # A one-row run, and it IS complete: the pre-flight verdict is the
        # whole of what this tick observed. Stating 1 (rather than leaving
        # the "unstated" 0) keeps the store's completeness check meaningful
        # for this group too (#3660 review round 2).
        steps_total=1,
    ))


def _default_local_machine_name(config: "Config") -> str | None:
    from coord.config import resolve_local_machine

    machine = resolve_local_machine(config)
    return machine.name if machine is not None else None


def _default_workdir(repo: str) -> str:
    from coord.platform_paths import default_coord_dir

    path = default_coord_dir() / "nightly_workdirs" / repo
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _read_spec_text(cwd: str, observations: list[tuple[NightlyStepObservation, bool]]) -> str | None:
    """Best-effort read of the spec file :func:`run_nightly_spec` just drove
    — every observation in one run shares the same ``spec`` path — so
    :func:`known_bugs_from_spec_text` can look up each step's
    ``known_bug:``. ``None`` (never raises) when there's nothing to read:
    a crashed run with zero real observations, or a spec path that
    genuinely isn't there — in both cases every step simply gets no
    known-bug suppression, which is the safe failure mode (#2096: silently
    ALWAYS suppressing would be the dangerous direction, not this)."""
    import os

    if not observations:
        return None
    spec_path = observations[0][0].spec
    full_path = os.path.join(cwd, spec_path)
    try:
        with open(full_path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None
