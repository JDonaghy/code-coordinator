"""Restore onto a tailnet standby and bring the board back up (#3129).

**Rung D3 of epic #3117 — the Domain-A recovery path.** D0 (:mod:`coord.backup`)
made a restorable artifact exist off the machine; D2 (:mod:`coord.dr_verify`)
proves continuously that it restores. Neither gets the *fleet* working again:
both stop at a verified SQLite file in a scratch directory. Between that file
and a serving board sit a dozen steps that are otherwise folklore — which units
to start and in what order, where ``coordinator.yml`` comes from, which
credentials the host needs, and how to avoid two daemons writing to two
divergent copies of the store.

That last one is why this is a command and not a runbook. ``coord serve`` is
the **sole writer** by design (#584); two daemons serving two restored copies
is not a degraded fleet, it is a split brain with no reconciliation path. So
step one is not "restore", it is "prove the incumbent is dead".

Operator-initiated, always. No automatic failover, no leader election, no
heartbeat trigger — out of scope per #3117, and deliberately so: the refusal
below is only safe because a human decided the incumbent is gone.

The shape of a run
------------------
Every check runs **before** any mutation, and the whole report is printed
before anything is started — a daemon that comes up and cannot merge a PR
looks recovered and is not (pin 4 of #3117):

1. :func:`probe_incumbent` — GET ``/healthz`` against the board every client
   is pinned to. If it answers, refuse unless ``--force``, naming what
   answered.
2. :func:`check_settings_checkout` — ``coordinator.yml`` comes from the
   ``coord-settings`` checkout. Absent, dirty, or behind its remote is a
   refusal: a fleet running on a stale config is a subtler outage than a
   stopped one.
3. :func:`check_credentials` — can this host *read issues and merge*, not
   "does a token file exist". Capability, probed.
4. :func:`plan_units` — ``deploy_manifest.ROLE_UNITS[ROLE_DAEMON]``, in
   order, with each unit's real systemd state.
5. …then, and only then, restore + start + :func:`verify_board`, which
   derives its verdict from a ``GET /board`` taken **after** the units are
   up, never from the absence of an exception (#2096).

``--dry-run`` is the primary interface
--------------------------------------
The real execution destroys a machine's coord state and is unrunnable in CI,
so ``--dry-run`` does everything except mutate: probe, resolve, check
credentials, enumerate units, print the ordered plan. It is the mode the tests
drive and the mode an operator rehearses with, so a passing ``--dry-run`` on a
healthy fleet is an acceptance criterion in its own right — which is why it
exits **zero** even while reporting that the incumbent is alive. A rehearsal
that reports "dellserver is still serving" has succeeded at rehearsing.

Clients still have to find the new board
----------------------------------------
Every ``client.toml`` in the fleet pins ``board_service =
"http://dellserver:7435"`` and the tailnet ACL grants ``tag:coord-worker``
exactly one destination. Cutting that pin is D5. What this rung owes the
operator is honesty about it: :data:`REMAINING_MANUAL_STEPS` is printed on
success, because "you are 90% recovered, here are the two edits left" is a
better outcome than a green checkmark on a board nothing can reach.

Nothing here re-implements a neighbour
--------------------------------------
The restore is :func:`coord.backup.restore` (D0), the store-side row counts are
:func:`coord.dr_verify.table_counts` (D2), the drift verdict is
:func:`coord.health.checks.config_drift.probe_config_drift` (D1), the unit list
is :data:`coord.deploy_manifest.ROLE_UNITS` and each unit's state comes from
the same batched ``systemctl show`` the health check and ``coord.deploy_units``
already share. Two implementations that agree today are a split brain waiting
to happen (#2085).

**No credential reaches argv or a log line.** Every probe runs a fixed argv
that names no secret, and everything printed goes through
:func:`coord.dr_verify.scrub` first.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from coord import backup, deploy_manifest, dr_verify, github_ops

LOGGER_NAME = "coord.dr_promote"

#: Named so a step can be referred to by both the plan and the report without
#: two spellings drifting apart.
STEP_INCUMBENT = "incumbent"
STEP_RESTORE = "restore"
STEP_CONFIG = "config"
STEP_CREDENTIALS = "credentials"
STEP_UNITS = "units"
STEP_VERIFY = "verify"

#: The three credentials the daemon needs to be *useful*, not merely to boot.
CRED_GITHUB = "github-token"
CRED_GIT_PUSH = "git-push-identity"
CRED_BOARD_TOKEN = "board-token"

#: Verdicts a credential probe can return. ``UNKNOWN`` is deliberately **not**
#: an alias for ok: a credential whose capability could not be established is
#: reported as a blocker, because the permissive default ("the attribute was
#: missing so we skipped the comparison") is exactly the unreachable-failure
#: shape #2096 rejects.
CRED_OK = "ok"
CRED_MISSING = "missing"
CRED_INCAPABLE = "incapable"
CRED_UNKNOWN = "unknown"

#: A ref name no branch-protection rule matches, used only as the target of a
#: ``git push --dry-run``. Nothing is ever created: ``--dry-run`` negotiates
#: with ``git-receive-pack`` (which GitHub refuses outright to a token without
#: write access) and stops before sending the pack.
PUSH_PROBE_REF = "refs/heads/coord-dr-promote-probe"

#: ``UnitFileState`` values meaning an operator said "never run this on its
#: own until I say otherwise" (#2812). Same set :mod:`coord.deploy_units`
#: honours — a masked unit is *skipped*, never a failure, and promotion is not
#: the moment to start overriding deliberate operator intent.
MASKED_STATES = frozenset({"masked", "masked-runtime"})

_HEALTH_TIMEOUT = 5.0
_PROBE_TIMEOUT = 20.0
_UNIT_TIMEOUT = 90.0
_VERIFY_TIMEOUT = 90.0

#: The two edits this rung explicitly does **not** make (D5 owns them). Printed
#: on every successful promotion, and on every dry run, because a promoted
#: standby under its own name is unreachable by every worker and every thin
#: client until both land.
REMAINING_MANUAL_STEPS: tuple[str, ...] = (
    "Tailnet ACL: scripts/azure-workers/tailnet-acl.hujson still grants "
    "tag:coord-worker exactly one board destination, `dellserver:7435`, with "
    "`hosts:` mapping that name to dellserver's fixed IP. Repoint the `hosts:` "
    "entry (or add this host's `<name>:7435` to every `dst`/`accept` rule that "
    "names dellserver:7435) and re-apply the ACL.",
    "Client pins: every `~/.coord/client.toml` in the fleet still carries "
    "`board_service = \"http://dellserver:7435\"`. Point each one at this host "
    "(or export $COORD_SERVICE_URL) — workers and thin clients cannot find the "
    "promoted board until they do.",
)


class PromoteError(RuntimeError):
    """A promotion could not proceed, or failed partway.

    Always safe to print: every message is built through :func:`_scrub`.
    """


def _log() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def _scrub(text: str) -> str:
    """:func:`coord.dr_verify.scrub` — the one redactor this lane owns.

    Routed through D2's rather than re-deriving the credential list here, so
    "which environment values are secret" has a single answer
    (:data:`coord.backup.CREDENTIAL_ENV_VARS`).
    """
    return dr_verify.scrub(text)


def _run(
    argv: Sequence[str],
    *,
    runner: Callable[..., Any] | None = None,
    timeout: float = _PROBE_TIMEOUT,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """``(returncode, stdout+stderr)`` — never raises for the ordinary failures.

    *argv* is always a fixed list built in this module: no shell, and no
    credential ever reaches it (see the module docstring).
    """
    run = runner or subprocess.run
    try:
        proc = run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **({"cwd": str(cwd)} if cwd is not None else {}),
        )
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found on this host"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    out = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    return int(getattr(proc, "returncode", 1)), _scrub(out.strip())


# --------------------------------------------------------------------------
# The pieces a plan is made of
# --------------------------------------------------------------------------


#: The order steps are reported in, so a refusal list reads in the order a real
#: run would have hit them rather than in the order the checks happened to run.
_STEP_ORDER = (
    STEP_INCUMBENT,
    STEP_RESTORE,
    STEP_CONFIG,
    STEP_CREDENTIALS,
    STEP_UNITS,
    STEP_VERIFY,
)


@dataclass(frozen=True)
class Blocker:
    """One reason a real promotion must not proceed.

    *forceable* is the whole waiver model, and it is deliberately per-blocker
    rather than a global "``--force`` skips the checks". ``--force`` means "I
    have decided the incumbent is gone and I accept losing this host's store"
    — two judgements a human can actually make. It does **not** mean "merge
    without a token" or "come up on a config nobody can vouch for", so those
    blockers carry ``forceable=False`` and no flag waives them.
    """

    step: str
    reason: str
    override: str = ""
    forceable: bool = False

    def render(self) -> str:
        suffix = f" (override with {self.override})" if self.override else ""
        return f"{self.step}: {self.reason}{suffix}"

    @property
    def _order(self) -> int:
        return _STEP_ORDER.index(self.step) if self.step in _STEP_ORDER else len(_STEP_ORDER)


@dataclass(frozen=True)
class Incumbent:
    """What, if anything, is currently serving the board clients are pinned to."""

    url: str | None
    alive: bool
    detail: str

    @property
    def responder(self) -> str:
        """The host:port that answered — what a refusal has to name."""
        if not self.url:
            return "(no board service configured)"
        return self.url.split("://", 1)[-1]


@dataclass(frozen=True)
class SettingsCheckout:
    """The ``coord-settings`` checkout ``coordinator.yml`` is served from."""

    config_path: Path
    real_path: Path | None
    present: bool
    in_git: bool
    dirty_count: int | None
    unpushed_count: int | None
    behind: bool | None
    upstream: str | None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class Credential:
    """One credential the daemon needs, and whether it can actually do the job."""

    name: str
    verdict: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.verdict == CRED_OK


@dataclass(frozen=True)
class UnitPlan:
    """One daemon unit, its queried systemd state, and what promote will do."""

    name: str
    file_state: str
    active_state: str
    action: str  # "start" | "skip (masked)" | "blocked (not installed)"

    @property
    def masked(self) -> bool:
        return self.file_state in MASKED_STATES

    @property
    def installed(self) -> bool:
        return bool(self.file_state)


@dataclass
class Plan:
    """Everything a real run would do, resolved without mutating anything."""

    incumbent: Incumbent
    settings: SettingsCheckout
    credentials: list[Credential]
    units: list[UnitPlan]
    snapshot_id: str | None
    snapshot_detail: str
    live_db: Path
    live_db_bytes: int
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def units_to_start(self) -> list[UnitPlan]:
        return [u for u in self.units if u.action == "start"]


@dataclass
class PromoteReport:
    """What a run actually did. ``elapsed_seconds`` is #3117's Domain-A RTO."""

    ok: bool
    dry_run: bool
    plan: Plan
    started_at: float
    elapsed_seconds: float = 0.0
    steps: list[dr_verify.StepResult] = field(default_factory=list)
    failure: str | None = None


# --------------------------------------------------------------------------
# Step 1 — refuse if the incumbent is alive
# --------------------------------------------------------------------------


def probe_incumbent(
    *,
    url: str | None = None,
    timeout: float = _HEALTH_TIMEOUT,
    fetcher: Callable[..., dict] | None = None,
) -> Incumbent:
    """Is something already serving the board?

    Uses :func:`coord.client.resolve_board_service` and
    :func:`coord.client.fetch_healthz` — the same seam every other coord
    command uses to answer "where is the board and is it up", so promote can
    never disagree with ``coord status`` about whether dellserver is alive.
    ``/healthz`` is never auth-gated, so this answers even from a host whose
    token is stale.

    An unreachable board is reported as ``alive=False`` with the transport
    error named: that is the *expected* state during a real promotion, and the
    reason has to survive into the operator's log.
    """
    from coord import client as _client  # noqa: PLC0415

    svc = _client.resolve_board_service(url)
    if svc is None:
        return Incumbent(
            url=None,
            alive=False,
            detail=(
                "no board service is configured on this host (no --board-url, "
                "$COORD_SERVICE_URL or ~/.coord/client.toml), so there is "
                "nothing to prove dead — promotion cannot rule out a split "
                "brain it cannot see"
            ),
        )
    fetch = fetcher or _client.fetch_healthz
    try:
        payload = fetch(svc, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — any failure means "not serving"
        return Incumbent(
            url=svc.url,
            alive=False,
            detail=f"no answer from {svc.url}/healthz ({type(exc).__name__}: {exc})",
        )
    if not isinstance(payload, dict):
        return Incumbent(
            url=svc.url,
            alive=True,
            detail=f"{svc.url}/healthz answered with a non-JSON-object body",
        )
    bits = ", ".join(
        f"{k}={payload[k]}"
        for k in ("status", "schema_version", "store_backend")
        if k in payload
    )
    return Incumbent(
        url=svc.url,
        alive=True,
        detail=f"{svc.url}/healthz answered ({bits})" if bits else f"{svc.url}/healthz answered",
    )


# --------------------------------------------------------------------------
# Step 3 — the config the daemon will come up on
# --------------------------------------------------------------------------


def check_settings_checkout(
    *,
    coord_dir: Path | None = None,
    runner: Callable[..., Any] | None = None,
    now: float | None = None,
) -> SettingsCheckout:
    """Is ``coordinator.yml``'s checkout present, clean, and level with its remote?

    The dirty/unpushed half is :func:`~coord.health.checks.config_drift.
    probe_config_drift` (#3120) called directly rather than re-derived — that
    probe already owns the two properties that make this answer trustworthy
    (resolve the symlink, never the link; a failed ``git status`` is UNKNOWN,
    never "clean"), and a second implementation here would be free to disagree
    with ``coord health`` about whether the fleet's only off-box config copy
    has drifted.

    The *behind* half is this module's own, because D1 does not compute it: a
    daemon host cares about what it has not pushed, a **standby** cares about
    what it has not pulled. It is measured with ``git ls-remote`` +
    ``merge-base --is-ancestor``, both read-only — this function deliberately
    never runs ``git fetch`` or ``git pull``, for the same reason
    ``config_drift`` never runs ``git push``: silently pulling an unreviewed
    config onto the host that is about to become the fleet's brain is a worse
    failure than refusing and naming it.

    Refusals: absent, dirty, behind, no upstream, or an unreadable git state.
    An *ahead* checkout (unpushed commits) is a note, not a refusal — it is
    #3120's concern and cannot make the promoted daemon run on stale config.

    *runner* is injectable for the behind-check only; ``probe_config_drift``
    owns its own ``subprocess`` seam by design (its whole "this module runs
    exactly five read-only git invocations and nothing else" assertion is
    written against it) and is not re-plumbed through this one.

    **One deliberate divergence from ``coord.config.resolve_config_path``**:
    this asks about ``$COORD_CONFIG`` then ``<coord_dir>/coordinator.yml``,
    and *not* the CWD-relative ``./coordinator.yml`` dev fallback — the same
    two rules ``config_drift`` replicates, for the same reason (a
    systemd-launched daemon has no meaningful current directory, so a config
    that only resolves from a shell's CWD is not one the promoted daemon would
    come up on). A host where only the CWD fallback exists is therefore
    reported *absent* here, which is the refusal an operator wants.
    """
    from coord.health.checks.config_drift import probe_config_drift  # noqa: PLC0415
    from coord.health.context import build_context  # noqa: PLC0415

    ctx = build_context(coord_dir=coord_dir, now=now)
    drift = probe_config_drift(ctx)
    values = drift.values or {}
    config_path = Path(str(values.get("path") or ctx.coord_dir / "coordinator.yml"))
    raw_real = values.get("real_path")
    real_path = Path(str(raw_real)) if raw_real else None

    problems: list[str] = []
    notes: list[str] = []

    present = bool(real_path is not None and real_path.exists())
    if not present:
        problems.append(
            f"no coordinator.yml at {config_path} — the promoted daemon has no "
            "config to come up on. Clone the coord-settings repo and symlink "
            f"its coordinator.yml at {config_path}."
        )
        return SettingsCheckout(
            config_path=config_path,
            real_path=real_path,
            present=False,
            in_git=False,
            dirty_count=None,
            unpushed_count=None,
            behind=None,
            upstream=None,
            problems=tuple(problems),
            notes=tuple(notes),
        )

    in_git = bool(values.get("has_upstream") is not None and "dirty_count" in values)
    dirty_count = values.get("dirty_count")
    unpushed_count = values.get("unpushed_count")
    has_upstream = bool(values.get("has_upstream"))

    if not in_git:
        # config_drift reported WARN "not inside a git work tree" — it never
        # populates dirty_count in that branch.
        problems.append(
            f"{real_path} is not inside a git work tree — there is no "
            "coord-settings checkout here, so nothing can say whether this "
            "config is current"
        )
        return SettingsCheckout(
            config_path=config_path,
            real_path=real_path,
            present=True,
            in_git=False,
            dirty_count=None,
            unpushed_count=None,
            behind=None,
            upstream=None,
            problems=tuple(problems),
            notes=tuple(notes),
        )

    if dirty_count is None:
        problems.append(
            f"could not read git status in {real_path.parent} ({drift.error or 'no detail'})"
        )
    elif dirty_count:
        problems.append(
            f"{dirty_count} uncommitted change(s) in {real_path.parent} — "
            "promote will not bring the fleet up on a half-finished config edit"
        )

    if not has_upstream:
        problems.append(
            f"{real_path.parent} has no upstream branch — nothing can say "
            "whether this config is behind the fleet's"
        )

    if unpushed_count:
        notes.append(
            f"{unpushed_count} unpushed commit(s) in {real_path.parent} — not a "
            "refusal (a local-only commit cannot make this host run on stale "
            "config) but #3120's condition is true here"
        )

    upstream: str | None = None
    behind: bool | None = None
    if has_upstream:
        upstream, behind, why = _behind_upstream(real_path.parent, runner=runner)
        if behind is None:
            problems.append(
                f"could not determine whether {real_path.parent} is behind "
                f"{upstream or 'its upstream'}: {why}"
            )
        elif behind:
            problems.append(
                f"{real_path.parent} is behind {upstream} ({why}) — a fleet "
                "running on a stale config is a subtler outage than a stopped "
                "one. `git -C "
                f"{real_path.parent} pull` first; promote will not pull for you."
            )

    return SettingsCheckout(
        config_path=config_path,
        real_path=real_path,
        present=True,
        in_git=True,
        dirty_count=dirty_count,
        unpushed_count=unpushed_count,
        behind=behind,
        upstream=upstream,
        problems=tuple(problems),
        notes=tuple(notes),
    )


def _behind_upstream(
    repo_dir: Path, *, runner: Callable[..., Any] | None = None
) -> tuple[str | None, bool | None, str]:
    """``(upstream, behind, why)`` for *repo_dir*, without fetching.

    ``behind`` is ``None`` when it could not be established — never ``False``.
    "We could not reach the remote" must not read as "we are up to date"; that
    is the permissive-default failure mode #2096 calls out, and here it would
    wave a stale config onto the machine that is about to become the fleet's
    only writer.
    """
    code, upstream_out = _run(
        ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref",
         "--symbolic-full-name", "@{u}"],
        runner=runner,
    )
    if code != 0 or not upstream_out.strip():
        return None, None, f"no upstream ref ({upstream_out or 'git failed'})"
    upstream = upstream_out.strip().splitlines()[0]
    remote, _, branch = upstream.partition("/")
    if not branch:
        return upstream, None, f"cannot split {upstream!r} into remote/branch"

    code, ls_out = _run(
        ["git", "-C", str(repo_dir), "ls-remote", remote, f"refs/heads/{branch}"],
        runner=runner,
    )
    if code != 0 or not ls_out.strip():
        return upstream, None, f"git ls-remote {remote} failed ({ls_out or 'no output'})"
    remote_sha = ls_out.split()[0]

    code, head_out = _run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], runner=runner
    )
    if code != 0 or not head_out.strip():
        return upstream, None, f"git rev-parse HEAD failed ({head_out or 'no output'})"
    head_sha = head_out.strip().splitlines()[0]
    if head_sha == remote_sha:
        return upstream, False, f"level with {upstream} at {remote_sha[:12]}"

    code, why = _run(
        ["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", remote_sha, "HEAD"],
        runner=runner,
    )
    if code == 0:
        # The remote head is already in our history: ahead-only, not behind.
        return upstream, False, f"ahead of {upstream} at {remote_sha[:12]}"
    if code == 1:
        return upstream, True, f"remote is at {remote_sha[:12]}, HEAD at {head_sha[:12]}"
    # 128 (object missing locally) or anything else: the remote carries commits
    # this checkout has never seen, which is the definition of behind.
    return upstream, True, f"remote {remote_sha[:12]} is not in this checkout ({why})"


# --------------------------------------------------------------------------
# Step 4 — credentials, probed for capability
# --------------------------------------------------------------------------


def check_credentials(
    config: Any,
    *,
    runner: Callable[..., Any] | None = None,
    checkout: Path | None = None,
    network: bool = True,
) -> list[Credential]:
    """Can this host do the daemon's job, or only boot?

    Deliberately *capability* probes, not file-presence checks. #3117's pin 4:
    a daemon that serves ``/board`` and cannot merge a PR looks recovered and
    is not, and a "the token file exists" check reports green on a machine that
    has never held the coordination token.
    """
    creds = [
        check_github_credential(config, runner=runner, network=network),
        check_git_push_credential(
            config, runner=runner, checkout=checkout, network=network
        ),
        check_board_token_credential(),
    ]
    return creds


def _configured_repos(config: Any) -> list[str]:
    """``owner/repo`` for every repo in ``coordinator.yml``, in order."""
    out: list[str] = []
    for repo in getattr(config, "repos", ()) or ():
        slug = (getattr(repo, "github", "") or "").strip()
        if slug and slug not in out:
            out.append(slug)
    return out


def check_github_credential(
    config: Any,
    *,
    runner: Callable[..., Any] | None = None,
    network: bool = True,
) -> Credential:
    """Can the GitHub token *read issues* and *merge*, per repo?

    Two read-only probes, both owned by ``coord.github_ops`` — this repo's
    single ``gh`` chokepoint (#1902/#2135), which is where every ``gh`` argv
    construction lives:

    * :func:`coord.github_ops.probe_repo_push_permission` per configured repo,
      reading ``.permissions.push``. Authenticating at all proves read;
      ``push: true`` is GitHub's own answer to "may this identity merge here",
      per repo, which is the granularity that actually matters — a token can
      be fine on four repos and useless on the fifth.
    * :func:`coord.github_ops.probe_issues_readable` once, because issue
      access is separately grantable (and separately disable-able) from repo
      metadata.

    Both are handed :func:`_run` as their runner rather than calling
    ``github_ops._gh``: this path must grade a refusal instead of raising on
    it, and must not write throttle/telemetry state into the store the
    promotion is still restoring. See the comment above those two functions.

    A repo that answers ``push: false``, an unparseable answer, or a ``gh``
    that is not installed all come back not-ok and named. Nothing here reads a
    token value, so nothing here can leak one.
    """
    slugs = _configured_repos(config)
    if not slugs:
        return Credential(
            CRED_GITHUB,
            CRED_UNKNOWN,
            "coordinator.yml names no repos, so there is nothing to probe "
            "merge capability against",
        )
    if not network:
        return Credential(
            CRED_GITHUB, CRED_UNKNOWN, "not probed (--no-network)"
        )

    def probe(argv: Sequence[str]) -> tuple[int, str]:
        return _run(argv, runner=runner)

    mergeable: list[str] = []
    refused: list[str] = []
    unknown: list[str] = []
    for slug in slugs:
        code, out = github_ops.probe_repo_push_permission(slug, run=probe)
        if code != 0:
            unknown.append(f"{slug} ({out.splitlines()[-1] if out else 'gh failed'})")
            continue
        answer = out.strip().splitlines()[-1].strip().lower() if out.strip() else ""
        if answer == "true":
            mergeable.append(slug)
        elif answer == "false":
            refused.append(slug)
        else:
            unknown.append(f"{slug} (unparseable .permissions.push: {answer!r})")

    code, issues_out = github_ops.probe_issues_readable(slugs[0], run=probe)
    issues_ok = code == 0
    issues_note = (
        f"reads issues on {slugs[0]}"
        if issues_ok
        else f"CANNOT read issues on {slugs[0]} "
        f"({issues_out.splitlines()[-1] if issues_out else 'gh failed'})"
    )

    detail = (
        f"merge on {len(mergeable)}/{len(slugs)} repo(s): "
        f"{', '.join(mergeable) or 'none'}; {issues_note}"
    )
    if refused:
        detail += f"; no push permission on {', '.join(refused)}"
    if unknown:
        detail += f"; could not probe {', '.join(unknown)}"

    if refused:
        return Credential(CRED_GITHUB, CRED_INCAPABLE, detail)
    if unknown or not issues_ok:
        return Credential(CRED_GITHUB, CRED_UNKNOWN, detail)
    return Credential(CRED_GITHUB, CRED_OK, detail)


def check_git_push_credential(
    config: Any,
    *,
    runner: Callable[..., Any] | None = None,
    checkout: Path | None = None,
    network: bool = True,
) -> Credential:
    """Is there a git identity here, and can it actually push?

    ``user.name``/``user.email`` are presence — a daemon with neither cannot
    author the merge commits or the conflict-fix rebases the fleet depends on.
    The capability half is ``git push --dry-run`` at a ref name no protection
    rule matches: it negotiates with ``git-receive-pack``, which GitHub refuses
    outright to an identity without write access, and stops before sending the
    pack. Nothing is created on the remote, on any exit path.

    No local checkout to probe from is *not* an ok verdict — it is
    ``unknown``, and it blocks. "We could not check" must never render as
    "fine".
    """
    name_code, name_out = _run(["git", "config", "--get", "user.name"], runner=runner)
    mail_code, mail_out = _run(["git", "config", "--get", "user.email"], runner=runner)
    missing = [
        label
        for label, code, out in (
            ("user.name", name_code, name_out),
            ("user.email", mail_code, mail_out),
        )
        if code != 0 or not out.strip()
    ]
    if missing:
        return Credential(
            CRED_GIT_PUSH,
            CRED_MISSING,
            f"git {' and '.join(missing)} not configured — the daemon cannot "
            "author a merge or a rebase",
        )
    identity = f"{name_out.strip()} <{mail_out.strip()}>"

    if not network:
        return Credential(
            CRED_GIT_PUSH, CRED_UNKNOWN, f"{identity}; push not probed (--no-network)"
        )

    probe_dir = checkout or _first_local_checkout(config)
    if probe_dir is None:
        return Credential(
            CRED_GIT_PUSH,
            CRED_UNKNOWN,
            f"{identity}; no local checkout on this host to probe a push from, "
            "so push capability is unproven",
        )
    code, out = _run(
        [
            "git", "-C", str(probe_dir), "push", "--dry-run", "origin",
            f"HEAD:{PUSH_PROBE_REF}",
        ],
        runner=runner,
    )
    if code == 0:
        return Credential(
            CRED_GIT_PUSH,
            CRED_OK,
            f"{identity}; `git push --dry-run` accepted by origin in {probe_dir}",
        )
    lowered = out.lower()
    if any(
        marker in lowered
        for marker in ("denied", "403", "permission", "authentication", "not authorized")
    ):
        return Credential(
            CRED_GIT_PUSH,
            CRED_INCAPABLE,
            f"{identity}; origin refused a dry-run push in {probe_dir}: "
            f"{out.splitlines()[-1] if out else 'no detail'}",
        )
    return Credential(
        CRED_GIT_PUSH,
        CRED_UNKNOWN,
        f"{identity}; could not establish push capability in {probe_dir}: "
        f"{out.splitlines()[-1] if out else 'git failed'}",
    )


def _first_local_checkout(config: Any) -> Path | None:
    """A checkout on *this* host to probe a push from, or ``None``.

    Reuses :func:`coord.health.context.local_checkouts` — the existing answer
    to "which of coordinator.yml's checkouts actually exist here", including
    its hostname-first resolution — rather than re-walking ``machines``.
    """
    from coord.health.context import local_checkouts  # noqa: PLC0415

    for checkout in local_checkouts(config):
        if (checkout.path / ".git").exists():
            return checkout.path
    return None


def check_board_token_credential() -> Credential:
    """Is the daemon's own bearer token configured on this host?

    Presence, and honestly labelled as such: nothing can prove a bearer token
    is *accepted* before the daemon that would accept it is running. Its
    capability gate is :func:`verify_board`, which authenticates with this
    exact token against the promoted daemon and fails the whole run on a 401 —
    a real gate, one step later, rather than a claim made here that nothing
    could contradict.
    """
    from coord.serve_app import SERVE_TOKEN_FILE, resolve_serve_token  # noqa: PLC0415

    token = resolve_serve_token()
    if not token:
        return Credential(
            CRED_BOARD_TOKEN,
            CRED_MISSING,
            f"no bearer token in $COORD_SERVE_TOKEN or {SERVE_TOKEN_FILE} — the "
            "promoted daemon would serve the whole board unauthenticated, "
            "relying on the tailnet ACL alone",
        )
    return Credential(
        CRED_BOARD_TOKEN,
        CRED_OK,
        "configured (presence only — proven for real when step "
        f"'{STEP_VERIFY}' authenticates against the promoted board)",
    )


# --------------------------------------------------------------------------
# Step 5 — the units, in manifest order
# --------------------------------------------------------------------------


def plan_units(*, runner: Callable[..., Any] | None = None) -> list[UnitPlan]:
    """The daemon role's units, in :data:`~coord.deploy_manifest.ROLE_UNITS` order.

    State comes from :func:`coord.health.checks.timer_active._timer_states` —
    the single batched ``systemctl --user show`` that the health check and
    ``coord.deploy_units`` already share, so promote cannot develop a third
    private opinion about what "enabled" or "masked" means.

    A masked unit is ``skip``, never a failure (#2812: masking is always a
    deliberate operator act). A unit systemd does not know about is
    ``blocked``: ``coord-serve.service`` missing is not a detail, it is the
    reason this host cannot be the board.
    """
    from coord.health.checks.timer_active import _timer_states  # noqa: PLC0415

    names = deploy_manifest.units_for_role(deploy_manifest.ROLE_DAEMON)
    states = _timer_states(tuple(names), runner=runner)
    out: list[UnitPlan] = []
    for name in names:
        fields = states.get(name) or {}
        file_state = (fields.get("UnitFileState") or "").strip()
        active_state = (fields.get("ActiveState") or "unknown").strip()
        if file_state in MASKED_STATES:
            action = "skip (masked)"
        elif not file_state:
            action = "blocked (not installed)"
        else:
            action = "start"
        out.append(
            UnitPlan(
                name=name,
                file_state=file_state or "(unknown)",
                active_state=active_state,
                action=action,
            )
        )
    return out


def start_units(
    units: Sequence[UnitPlan],
    *,
    runner: Callable[..., Any] | None = None,
    timeout: float = _UNIT_TIMEOUT,
) -> list[dr_verify.StepResult]:
    """``systemctl --user enable --now`` each startable unit, in order.

    ``enable`` as well as ``start``: promotion is precisely the case where a
    host's *role* changed, so these units must also survive its next reboot —
    unlike ``coord.deploy_units.enable_timers``, which is running inside
    somebody else's deploy and must not decide which services a host runs.

    Success is not "systemctl exited 0". After the whole ordered pass, systemd
    is re-queried and each unit's ``ActiveState`` read back: a unit that exits
    0 and lands ``failed`` a second later is exactly the shape a "reported
    success that only proves the request was issued" takes (#2096).
    """
    from coord.health.checks.timer_active import _timer_states  # noqa: PLC0415

    results: list[dr_verify.StepResult] = []
    started: list[str] = []
    for unit in units:
        if unit.action != "start":
            results.append(
                dr_verify.StepResult(f"unit:{unit.name}", True, f"{unit.action} — left alone")
            )
            continue
        _log().info("dr promote: systemctl --user enable --now %s", unit.name)
        code, out = _run(
            ["systemctl", "--user", "enable", "--now", unit.name],
            runner=runner,
            timeout=timeout,
        )
        if code != 0:
            results.append(
                dr_verify.StepResult(
                    f"unit:{unit.name}", False, f"enable --now failed: {out or 'no detail'}"
                )
            )
            continue
        started.append(unit.name)
        results.append(dr_verify.StepResult(f"unit:{unit.name}", True, "enable --now ok"))

    if not started:
        return results

    # The confirmation pass: what systemd says *after* the fact.
    states = _timer_states(tuple(started), runner=runner)
    for name in started:
        active = (states.get(name, {}).get("ActiveState") or "").strip()
        ok = active == "active"
        results.append(
            dr_verify.StepResult(
                f"unit:{name}:confirmed",
                ok,
                f"ActiveState={active or 'unreadable'}"
                + ("" if ok else " — enable --now returned 0 but the unit is not running"),
            )
        )
    return results


# --------------------------------------------------------------------------
# Step 6 — verify against the board that is now actually running
# --------------------------------------------------------------------------


def verify_board(
    *,
    db_path: Path,
    url: str,
    token: str | None = None,
    timeout: float = _VERIFY_TIMEOUT,
    fetcher: Callable[..., dict] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dr_verify.StepResult:
    """Prove the promoted daemon is serving the store that was just restored.

    D2's parity property — *"a real daemon answered GET /board from this
    store"* — asserted against the **live** daemon rather than a throwaway
    one. :func:`coord.dr_verify.check_parity` is deliberately *not* called
    here even though it asserts the same thing: it boots its own ``coord
    serve`` with ``COORD_DIR`` pointed at the restored file's directory, which
    after a promotion is ``~/.coord`` — a second writer on the live store, the
    exact split brain this command exists to avoid. The store side of the
    comparison is still D2's :func:`coord.dr_verify.table_counts`, so the two
    lanes count rows the same way.

    The count assertion is a bound, not an equality, and that is not laziness:
    #762 caps ``/board``'s assignments and the live daemon (started by systemd)
    has no ``COORD_BOARD_RETENTION_DAYS=0`` override, so an exact match would
    be wrong. It can still fail, in three distinct ways that all matter — the
    board never answers, it answers with something that is not a board, or it
    serves **zero** assignments from a store that holds some (the shape a
    restored-but-unprojectable store takes).
    """
    from coord import client as _client  # noqa: PLC0415

    store_rows = int(dr_verify.table_counts(Path(db_path)).get("assignments", 0))
    svc = _client.ServiceConfig(url=url.rstrip("/"), token=token)
    fetch = fetcher or _client.fetch_board_payload

    deadline = now() + timeout
    last_error = "never answered"
    payload: Any = None
    while now() < deadline:
        try:
            payload = fetch(svc, timeout=10.0)
            break
        except Exception as exc:  # noqa: BLE001 — still booting, or genuinely dead
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                # A definitive answer, not a daemon still booting: this is the
                # capability gate `check_board_token_credential` deliberately
                # deferred to here. Retrying it would only turn a precise
                # failure into a timeout.
                return dr_verify.StepResult(
                    STEP_VERIFY,
                    False,
                    f"the promoted board at {svc.url} rejected the daemon's own "
                    f"bearer token (HTTP {status}) — $COORD_SERVE_TOKEN / "
                    "~/.coord/serve_token is not the token this daemon is "
                    "serving with, so nothing on the fleet can read the board",
                )
            last_error = f"{type(exc).__name__}: {exc}"
            sleep(0.5)
    else:
        return dr_verify.StepResult(
            STEP_VERIFY,
            False,
            f"the promoted board at {svc.url} did not serve GET /board within "
            f"{timeout:.0f}s ({_scrub(last_error)})",
        )

    if not isinstance(payload, dict) or "schema_version" not in payload:
        return dr_verify.StepResult(
            STEP_VERIFY,
            False,
            f"{svc.url}/board answered with something that is not a board payload",
        )
    rows = payload.get("assignments")
    if not isinstance(rows, list):
        return dr_verify.StepResult(
            STEP_VERIFY,
            False,
            f"{svc.url}/board carried no `assignments` list — the restored "
            "store did not project as a board",
        )
    served = len(rows)
    if store_rows > 0 and served == 0:
        return dr_verify.StepResult(
            STEP_VERIFY,
            False,
            f"{svc.url}/board served 0 assignments from a restored store "
            f"holding {store_rows} — the restore does not come up as the board "
            "it was taken from",
        )
    if served > store_rows:
        return dr_verify.StepResult(
            STEP_VERIFY,
            False,
            f"{svc.url}/board served {served} assignments from a restored store "
            f"holding only {store_rows} — this daemon is not serving the store "
            "that was just restored",
        )
    return dr_verify.StepResult(
        STEP_VERIFY,
        True,
        f"the promoted daemon at {svc.url} served GET /board with {served} "
        f"assignment(s) from a store holding {store_rows}",
    )


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def build_plan(
    *,
    board_url: str | None = None,
    coord_dir: Path | None = None,
    runner: Callable[..., Any] | None = None,
    network: bool = True,
    snapshot_id: str | None = None,
    restic_runner: backup.ResticRunner | None = None,
) -> Plan:
    """Resolve everything a real run would do — probing, never mutating.

    Every blocker this collects is a refusal a real run would make *before*
    the first mutation, which is what makes the credential report land before
    any unit is started.

    Deliberately takes no ``force``: a plan is what is *true about this host*,
    not what the operator is willing to override, so ``--dry-run`` and
    ``--dry-run --force`` report the identical refusal list and mark which
    entries ``--force`` would waive. Applying the waiver is :func:`promote`'s
    job, in one place.
    """
    from coord import db as _db  # noqa: PLC0415

    # One resolution of "where is this host's coord state", so the config the
    # daemon would load and the store it would open can never come from two
    # different directories. `$COORD_DIR` drives both (`coord.db` re-resolves
    # it on every access, #2781) exactly as it does for the running daemon.
    coord_dir = Path(coord_dir) if coord_dir else Path(_db.COORD_DIR)
    blockers: list[Blocker] = []

    incumbent = probe_incumbent(url=board_url)
    if incumbent.alive:
        blockers.append(
            Blocker(
                STEP_INCUMBENT,
                f"{incumbent.responder} is serving a live board — {incumbent.detail}. "
                "Promotion is for a dead daemon; two daemons on two restored "
                "copies is a split brain with no reconciliation path (#584)",
                "--force",
                forceable=True,
            )
        )
    elif incumbent.url is None:
        blockers.append(
            Blocker(
                STEP_INCUMBENT,
                incumbent.detail,
                "--board-url (to name the incumbent) or --force (to assert "
                "there is none)",
                forceable=True,
            )
        )

    settings = check_settings_checkout(coord_dir=coord_dir, runner=runner)
    for problem in settings.problems:
        blockers.append(Blocker(STEP_CONFIG, problem))

    config: Any = None
    if settings.present:
        try:
            from coord.config import load  # noqa: PLC0415

            config = load(settings.config_path)
        except Exception as exc:  # noqa: BLE001 — a bad config is a blocker, not a crash
            blockers.append(
                Blocker(STEP_CONFIG, f"could not load {settings.config_path}: {exc}")
            )

    credentials = check_credentials(config, runner=runner, network=network)
    for cred in credentials:
        if not cred.ok:
            blockers.append(
                Blocker(STEP_CREDENTIALS, f"{cred.name}: {cred.verdict} — {cred.detail}")
            )

    units = plan_units(runner=runner)
    missing_units = [u.name for u in units if u.action.startswith("blocked")]
    if missing_units:
        # All of them missing usually means the *query* failed, not that ten
        # separate units were each forgotten — `_timer_states` returns `{}`
        # when there is no systemd user session to ask at all. Saying so is
        # the difference between an operator running the install step and an
        # operator wondering why `systemctl --user` is unavailable.
        whole_query = len(missing_units) == len(units)
        blockers.append(
            Blocker(
                STEP_UNITS,
                f"{len(missing_units)} daemon unit(s) systemd does not know "
                f"about here ({', '.join(missing_units)}) — "
                + (
                    "systemd returned no state for ANY of them, so either none "
                    "is installed on this host or there is no `systemctl "
                    "--user` session to ask. "
                    if whole_query
                    else ""
                )
                + "Install them first (`coord release propagate`, or copy "
                "deploy/ into ~/.config/systemd/user and `systemctl --user "
                "daemon-reload`)",
            )
        )

    live_db = backup.live_db_path()
    try:
        live_bytes = live_db.stat().st_size
    except OSError:
        live_bytes = 0
    if live_bytes > 0:
        blockers.append(
            Blocker(
                STEP_RESTORE,
                f"a non-empty coord store already exists at {live_db} "
                f"({backup.format_bytes(live_bytes)}) — restoring over it "
                "destroys whatever this host was holding",
                "--force",
                forceable=True,
            )
        )

    resolved_snapshot, snapshot_detail = _resolve_snapshot(
        snapshot_id, runner=restic_runner
    )
    if resolved_snapshot is None:
        blockers.append(Blocker(STEP_RESTORE, snapshot_detail))

    blockers.sort(key=lambda b: b._order)
    return Plan(
        incumbent=incumbent,
        settings=settings,
        credentials=credentials,
        units=units,
        snapshot_id=resolved_snapshot,
        snapshot_detail=snapshot_detail,
        live_db=live_db,
        live_db_bytes=live_bytes,
        blockers=blockers,
    )


def _resolve_snapshot(
    snapshot_id: str | None, *, runner: backup.ResticRunner | None = None
) -> tuple[str | None, str]:
    """Which off-site snapshot a real run would restore, and why.

    Uses D2's :func:`coord.dr_verify.latest_snapshot`, which already refuses an
    empty repository rather than silently "restoring nothing".
    """
    if snapshot_id:
        return snapshot_id, f"snapshot {snapshot_id[:12]} (explicitly requested)"
    try:
        config = backup.BackupConfig.from_env()
        snapshot = dr_verify.latest_snapshot(config, runner=runner)
    except (backup.BackupError, dr_verify.DRVerifyError) as exc:
        return None, _scrub(str(exc))
    except Exception as exc:  # noqa: BLE001 — restic missing, network down, ...
        return None, _scrub(f"could not list off-site snapshots: {type(exc).__name__}: {exc}")
    sid = str(snapshot.get("id", ""))
    return sid, f"snapshot {sid[:12]} taken {snapshot.get('time', '?')}"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_plan(plan: Plan, *, dry_run: bool) -> list[str]:
    """The ordered plan, the credential report, and every refusal — as lines."""
    verb = "would" if dry_run else "will"
    lines: list[str] = []
    lines.append(
        f"coord dr promote{' --dry-run' if dry_run else ''}: plan for this host"
    )
    lines.append("")

    lines.append(f"1. {STEP_INCUMBENT}: probe the board clients are pinned to")
    lines.append(
        f"   {'LIVE' if plan.incumbent.alive else 'no answer'} — {plan.incumbent.detail}"
    )

    lines.append(f"2. {STEP_RESTORE}: {plan.snapshot_id or 'UNRESOLVED'}")
    lines.append(f"   {plan.snapshot_detail}")
    lines.append(
        f"   {verb} restore into {plan.live_db} "
        f"(currently {backup.format_bytes(plan.live_db_bytes)})"
    )

    lines.append(f"3. {STEP_CONFIG}: {plan.settings.config_path}")
    if plan.settings.real_path and plan.settings.real_path != plan.settings.config_path:
        lines.append(f"   resolves to {plan.settings.real_path}")
    if plan.settings.ok:
        lines.append(
            "   clean and level with " + (plan.settings.upstream or "its upstream")
        )
    else:
        for problem in plan.settings.problems:
            lines.append(f"   PROBLEM: {problem}")
    for note in plan.settings.notes:
        lines.append(f"   note: {note}")

    lines.append(f"4. {STEP_CREDENTIALS}: what this host can actually do")
    for cred in plan.credentials:
        mark = "ok  " if cred.ok else "MISS"
        lines.append(f"   [{mark}] {cred.name}: {cred.verdict} — {cred.detail}")

    lines.append(
        f"5. {STEP_UNITS}: ROLE_UNITS[{deploy_manifest.ROLE_DAEMON}], in order"
    )
    for i, unit in enumerate(plan.units, start=1):
        lines.append(
            f"   {i}. {unit.name} [{unit.file_state}/{unit.active_state}] → {unit.action}"
        )

    lines.append(
        f"6. {STEP_VERIFY}: GET /board against the promoted daemon and compare "
        "it to the restored store"
    )
    lines.append("7. report the measured elapsed time (this fleet's Domain-A RTO)")
    lines.append("")

    if plan.blockers:
        waivable = sum(1 for b in plan.blockers if b.forceable)
        lines.append(
            f"REFUSALS ({len(plan.blockers)}, of which {waivable} waivable "
            "with --force):"
        )
        for blocker in plan.blockers:
            lines.append(f"  - {blocker.render()}")
    else:
        lines.append("REFUSALS: none — a real run would proceed")
    lines.append("")
    lines.extend(render_remaining_steps())
    return lines


def render_remaining_steps() -> list[str]:
    """The D5 work this rung reports rather than does."""
    lines = [
        "REMAINING MANUAL STEPS (D5 — not done by this command; until both "
        "land, no worker or thin client can reach the promoted board):"
    ]
    for i, step in enumerate(REMAINING_MANUAL_STEPS, start=1):
        lines.append(f"  {i}. {step}")
    return lines


def render_report(report: PromoteReport) -> list[str]:
    """The post-run summary: every step, the elapsed time, and what is left."""
    lines: list[str] = []
    for step in report.steps:
        lines.append(f"  [{'ok ' if step.ok else 'FAIL'}] {step.name}: {step.detail}")
    lines.append("")
    if report.ok:
        lines.append(
            f"coord dr promote: OK in {report.elapsed_seconds:.1f}s elapsed "
            f"({report.elapsed_seconds / 60.0:.1f} min) — this fleet's observed "
            "Domain-A RTO for the restore-and-serve half"
        )
        lines.append("")
        lines.extend(render_remaining_steps())
    else:
        lines.append(
            f"coord dr promote: FAILED after {report.elapsed_seconds:.1f}s: "
            f"{report.failure}"
        )
    return lines


# --------------------------------------------------------------------------
# The lane
# --------------------------------------------------------------------------


def promote(
    *,
    dry_run: bool = True,
    force: bool = False,
    board_url: str | None = None,
    coord_dir: Path | None = None,
    runner: Callable[..., Any] | None = None,
    network: bool = True,
    snapshot_id: str | None = None,
    restic_runner: backup.ResticRunner | None = None,
    local_board_url: str | None = None,
    verify_timeout: float = _VERIFY_TIMEOUT,
    plan: Plan | None = None,
) -> PromoteReport:
    """Restore onto this standby and bring it up as the board.

    Never mutates anything when *dry_run* — that mode resolves the identical
    plan and stops, which is what makes it a rehearsal rather than a
    different code path.

    In a real run, every blocker is a hard stop **before** the first mutation.
    ``--force`` waives only the two blockers that name it (a live incumbent, a
    non-empty local store); a missing credential or a stale config checkout is
    never waived, because forcing past those produces exactly the
    looks-recovered-but-is-not outcome this rung exists to prevent.
    """
    started_at = time.time()
    started_mono = time.monotonic()
    plan = plan or build_plan(
        board_url=board_url,
        coord_dir=coord_dir,
        runner=runner,
        network=network,
        snapshot_id=snapshot_id,
        restic_runner=restic_runner,
    )
    report = PromoteReport(ok=False, dry_run=dry_run, plan=plan, started_at=started_at)

    if dry_run:
        report.ok = True
        report.elapsed_seconds = time.monotonic() - started_mono
        return report

    remaining = [b for b in plan.blockers if not (force and b.forceable)]
    if remaining:
        report.failure = (
            f"{len(remaining)} refusal(s) — nothing was restored and no unit was "
            "started: " + "; ".join(b.render() for b in remaining)
        )
        report.elapsed_seconds = time.monotonic() - started_mono
        return report

    try:
        report.steps.append(_do_restore(plan, restic_runner=restic_runner))
        report.steps.append(_write_role(coord_dir))
        report.steps.extend(start_units(plan.units, runner=runner))
        if any(not s.ok for s in report.steps):
            raise PromoteError(
                "a step failed before the board could be verified: "
                + "; ".join(s.detail for s in report.steps if not s.ok)
            )
        url = local_board_url or f"http://127.0.0.1:{_serve_port()}"
        from coord.serve_app import resolve_serve_token  # noqa: PLC0415

        verified = verify_board(
            db_path=plan.live_db,
            url=url,
            token=resolve_serve_token(),
            timeout=verify_timeout,
        )
        report.steps.append(verified)
        if not verified.ok:
            raise PromoteError(verified.detail)
        report.ok = True
    except (PromoteError, backup.BackupError, dr_verify.DRVerifyError) as exc:
        report.failure = _scrub(str(exc))
    finally:
        report.elapsed_seconds = time.monotonic() - started_mono
    return report


def _serve_port() -> int:
    from coord.serve_app import SERVE_PORT  # noqa: PLC0415

    return int(SERVE_PORT)


def _do_restore(
    plan: Plan, *, restic_runner: backup.ResticRunner | None = None
) -> dr_verify.StepResult:
    """D0's ``restore`` into the live store path — the only restore path there is.

    ``force=True`` is passed unconditionally and deliberately.
    :func:`coord.backup.restore` refuses to write to ``live_db_path()`` without
    it *at all* — a correct default for the generic ``coord backup restore``
    caller, for whom writing the live store is always the mistake that destroys
    the thing the lane protects. For promotion it is the entire point, and the
    DR-appropriate gate has already run: :func:`build_plan` refuses a
    non-empty local store unless the operator passed ``--force``, and this code
    is unreachable until that refusal is satisfied.
    """
    started = time.monotonic()
    _log().info(
        "dr promote: restoring %s into %s",
        str(plan.snapshot_id)[:12],
        plan.live_db,
    )
    backup.restore(
        str(plan.snapshot_id),
        plan.live_db,
        runner=restic_runner,
        force=True,
    )
    took = time.monotonic() - started
    size = plan.live_db.stat().st_size if plan.live_db.exists() else 0
    return dr_verify.StepResult(
        STEP_RESTORE,
        True,
        f"restored {str(plan.snapshot_id)[:12]} into {plan.live_db} "
        f"({backup.format_bytes(size)}) in {took:.1f}s",
    )


def _write_role(coord_dir: Path | None) -> dr_verify.StepResult:
    """Declare this host the daemon, host-locally (#3128).

    ``deploy_manifest.resolve_role`` reads ``<coord_dir>/role`` with the board
    down — which is exactly the state this command runs in — so a promoted
    standby that never writes it keeps reporting itself a worker to
    ``unit_enablement`` forever after.

    Confirmed by re-reading through :func:`~coord.deploy_manifest.resolve_role`
    rather than by the write not raising.
    """
    from coord import db as _db  # noqa: PLC0415

    target = Path(coord_dir) if coord_dir else Path(_db.COORD_DIR)
    target.mkdir(parents=True, exist_ok=True)
    (target / "role").write_text(f"{deploy_manifest.ROLE_DAEMON}\n", encoding="utf-8")
    declared = deploy_manifest.resolve_role(target, env={})
    ok = declared.role == deploy_manifest.ROLE_DAEMON and declared.valid
    return dr_verify.StepResult(
        "role",
        ok,
        f"{target / 'role'} now reads {declared.role!r} (source={declared.source})"
        if ok
        else f"wrote {target / 'role'} but resolve_role still reports "
        f"{declared.role!r} from {declared.source}",
    )


def to_record(report: PromoteReport) -> dict[str, Any]:
    """A JSON-able summary of a run — the RTO number, kept rather than estimated."""
    return {
        "outcome": "ok" if report.ok else "failed",
        "dry_run": report.dry_run,
        "timestamp": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(report.started_at)
        ),
        "elapsed_seconds": round(report.elapsed_seconds, 3),
        "snapshot_id": report.plan.snapshot_id,
        "incumbent_alive": report.plan.incumbent.alive,
        "blockers": [b.render() for b in report.plan.blockers],
        "credentials": {c.name: c.verdict for c in report.plan.credentials},
        "units": {u.name: u.action for u in report.plan.units},
        "steps": [s.to_dict() for s in report.steps],
        "failure": report.failure,
        "remaining_manual_steps": list(REMAINING_MANUAL_STEPS),
    }


def write_record(report: PromoteReport, path: Path) -> Path:
    """Persist :func:`to_record` at *path*, scrubbed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _scrub(json.dumps(to_record(report), indent=2, sort_keys=True)) + "\n"
    path.write_text(body, encoding="utf-8")
    return path


__all__ = [
    "Blocker",
    "Credential",
    "Incumbent",
    "Plan",
    "PromoteError",
    "PromoteReport",
    "REMAINING_MANUAL_STEPS",
    "SettingsCheckout",
    "UnitPlan",
    "build_plan",
    "check_credentials",
    "check_settings_checkout",
    "plan_units",
    "probe_incumbent",
    "promote",
    "render_plan",
    "render_remaining_steps",
    "render_report",
    "start_units",
    "verify_board",
    "write_record",
]
