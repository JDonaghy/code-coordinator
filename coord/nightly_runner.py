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
   nothing on a clean pass).
4. Every step observed is persisted to :mod:`coord.nightly_store`,
   regardless of outcome — so ``coord release gate`` can read a repo's
   nightly artifacts without ``--from-json`` (#3660 acceptance).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Sequence

from coord.acceptance_drivers import DriverResult, run_driver
from coord.bugbash import (
    GUI_LANE_DRIVER_KINDS,
    BugbashLane,
    CoordRunner,
    subprocess_coord_runner,
)
from coord.nightly_smoke import (
    ArtifactPlan,
    NightlyStepObservation,
    NightlyStepOutcome,
    StepVerdict,
    classify_step,
    pick_nightly_host,
    process_nightly_step,
    resolve_artifact_plan,
)
from coord.nightly_store import NightlyResultRecord, record_nightly_result

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
    producing any; see :class:`coord.acceptance_drivers.DriverResult`'s own
    ``ok`` caveat) yields ONE observation naming the whole spec as the
    "step" — #2096 "a gate must be able to fail": a crash that produced
    zero structured output must never read as "zero steps, therefore
    nothing failed."
    """
    if not result.tests:
        return [(
            NightlyStepObservation(
                repo=repo, spec=spec, step="(spec)", sha=sha, passed=result.ok,
                checked_at=checked_at,
                detail=(
                    "driver produced no structured test results "
                    f"(exit_code={result.exit_code})"
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


def _default_resolve_ref_sha(github_slug: str, ref: str, *, timeout: float = 15.0) -> str:
    """The commit *ref* (a branch OR a tag name) resolves to on
    ``github.com/<github_slug>``, via a plain ``git ls-remote`` — no GitHub
    API token needed, and no local checkout required, so this works
    identically whether the artifact plan says BUILD (branch) or DOWNLOAD
    (release tag).

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


def _git_clone_ref(github_slug: str, ref: str, workdir: str, *, timeout: float = 600.0) -> None:
    subprocess.run(
        ["git", "clone", "--branch", ref, "--depth", "1",
         f"https://github.com/{github_slug}.git", workdir],
        check=True, timeout=timeout,
    )


def _default_build_artifact(
    plan: NightlyRunPlan, *, config: "Config", workdir: str,
) -> ObtainedArtifact:
    repo_cfg = config.repo(plan.repo)
    if repo_cfg is None:
        raise NightlyRunnerError(f"repo {plan.repo!r} is not declared in coordinator.yml")
    _git_clone_ref(repo_cfg.github, plan.ref, workdir)
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
    _git_clone_ref(repo_cfg.github, plan.ref, workdir)
    assets = fetch_release_assets(plan.ref, repo=repo_cfg.github)
    matches = [a for a in assets if plan.artifact in a.name]
    if not matches:
        have = sorted(a.name for a in assets)
        raise NightlyRunnerError(
            f"release {plan.ref!r} of {repo_cfg.github!r} has no asset "
            f"matching {plan.artifact!r} (have: {have})"
        )
    binary_path = download_asset(matches[0].download_url, workdir)
    return ObtainedArtifact(cwd=workdir, binary_path=str(binary_path))


# ── the full orchestration ─────────────────────────────────────────────────


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

    @property
    def infra_blocked(self) -> bool:
        return self.plan.infra_blocked

    @property
    def any_dropped(self) -> bool:
        """#2096: an alerting red step that got no issue, no comment, and
        no close — never silently folded into "nothing to report"."""
        return any(o.dropped for o in self.outcomes)


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
    failure) never runs, never files, never persists either — #3652's own
    rule ("a host that's locked/asleep/missing trust is an INFRA result,
    never an app red") extended to mean INFRA genuinely means "we didn't
    look", not "we looked and it was fine" or "we looked and it failed".
    """
    plan = plan_nightly_run(
        repo=repo, artifact=artifact, spec=spec, config=config, board=board,
        http_client=http_client, fetch_latest_release_tag_fn=fetch_latest_release_tag_fn,
    )
    if dry_run or plan.infra_blocked:
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
    sha = resolve_sha(repo_cfg.github, plan.ref)

    effective_workdir = workdir or _default_workdir(repo)
    obtained = (obtain_artifact_fn or obtain_artifact)(
        plan, config=config, workdir=effective_workdir,
    )

    observations = run_nightly_spec(
        plan, config=config, cwd=obtained.cwd, sha=sha, now=now,
        run_driver_fn=run_driver_fn,
    )

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
    lane = BugbashLane(
        platform=plan.driver_kind, driver_kind=plan.driver_kind,
        machine=plan.host.machine.name, capability=driver_cfg.capability,
    )

    checked_at = time.time() if now is None else now
    outcomes: list[NightlyStepOutcome] = []
    for obs, unavailable in observations:
        verdict: StepVerdict = classify_step(obs, known_bugs_raw.get(obs.step))
        outcome = process_nightly_step(
            verdict, open_issues=open_issues, closed_issues=closed_issues,
            lane=lane, runner=runner or subprocess_coord_runner, dry_run=dry_run,
            platform=plan.driver_kind,
        )
        outcomes.append(outcome)
        record_nightly_result(NightlyResultRecord(
            repo=repo, artifact=artifact, sha=sha, passed=obs.passed,
            checked_at=checked_at, detail=obs.detail, unavailable=unavailable,
            spec=obs.spec, step=obs.step, host=plan.host.machine.name,
            evidence=obs.evidence,
        ))

    return NightlyRunReport(plan=plan, ran=True, sha=sha, outcomes=tuple(outcomes))


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
