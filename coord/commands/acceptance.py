"""``coord acceptance`` — the framework-agnostic oracle-loop runner (#944,
docs/ORACLE_LOOP.md).

Subcommands:

- ``coord acceptance run --repo R (--issue N | --all)`` — run the repo's
  declared driver **in-session** (the worker's own warm loop) and print a
  structured pass/fail verdict. Sealed: verdicts only, never test source.
- ``coord acceptance record --repo R --issue N --sha SHA`` — the
  coordinator's **external** trust gate: re-run the sealed slice against the
  pushed SHA in a throwaway worktree and write the verdict to the board (the
  Acceptance box). Routes the whole command through the daemon (mirrors
  ``coord merge`` / ``coord diagnose`` — the no-local-DB rule), never a bare
  ``save_board``.
- ``coord acceptance mock <repo> <tracking_issue>`` — Gate A (#930): dispatch
  an independent mock-author that renders a viewable mock + writes
  ``tests/acceptance/ms-NN/contract.md``.
  ``coord.milestone_dispatch.gate_a_status`` blocks the milestone's issue
  dispatch until that contract exists. ``--amend``/``--amend-file`` (#1315)
  instead dispatch a targeted correction to an already-merged contract — the
  properly-typed tool for that, replacing the ``type="work"`` fallback that
  caused #1314. **Requires no acceptance driver at all** — unlike ``run``/
  ``record`` above, mock-authoring only needs ``gh`` + a machine to dispatch
  to, so it works before ``acceptance.drivers`` has an entry for the repo
  (see this module's :func:`_resolve_driver`, the sibling of
  :func:`coord.acceptance.acceptance_capability_gap`, which ``run``/``record``
  call and ``mock`` does not).
  **Refuses outright from a thin client** (:func:`_refuse_if_thin_client_mock`,
  #2018) — mirrors ``coord.commands.portal._refuse_if_thin_client``: the
  ``gh`` calls this dispatch makes are plain local ``subprocess`` calls, not
  routed through the daemon, so running this from the operator's laptop (a
  thin client) used to either fail quietly or run with whatever ``gh``
  identity happened to be on PATH there.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from coord import github_ops
from coord.acceptance import (
    acceptance_capability_gap,
    acceptance_root_for_driver,
    apply_expected_red,
    build_verdict,
    dump_manifest_error_hint,
    expected_red_failure_summary,
    failure_summary,
    list_expected_red_via_api,
    load_expected_red,
    load_manifest,
    ms_dir_for_issue,
    search_roots_for_repo,
    test_ids_for_issue,
)
from coord.acceptance_drivers import DriverError, run_driver
from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.comments import format_needs_attention
from coord.dispatch import DispatchRefused
from coord.models import Repo


@click.group("acceptance")
def acceptance_group() -> None:
    """The oracle-loop acceptance runner.

    A thin, framework-agnostic front end over a per-repo driver adapter
    declared in ``coordinator.yml`` (``acceptance.drivers``). ``run`` is what
    a worker calls in its own warm session to check itself against the
    sealed suite; ``record`` is the coordinator's external re-run against a
    pushed SHA — the trust gate a headless worker can't fake.
    """


def _resolve_driver(cfg, repo: str, route_path: str | None = None):
    """Resolve *repo*'s acceptance driver, exit(1) with a clear message when
    none resolves.

    *route_path* (#1125, repo-root-relative — e.g. ``"coord/foo.py"``)
    selects a route when the repo's driver is routed
    (``acceptance.drivers.<repo>.routes``); it's ignored for a flat
    (unrouted) driver. When the repo IS routed but *route_path* doesn't
    resolve to a route (including ``None``), the error names the missing
    ``--for-path`` rather than the generic "not configured at all" message,
    since those are different operator mistakes.
    """
    driver_cfg = cfg.acceptance.driver_for(repo, route_path)
    if driver_cfg is None:
        if cfg.acceptance.has_driver(repo):
            click.echo(
                f"error: repo {repo!r} has a routed acceptance driver "
                "(acceptance.drivers routes) but no route matched — pass "
                "--for-path to select the subtree (e.g. 'coord/**')",
                err=True,
            )
        else:
            click.echo(
                f"error: no acceptance driver configured for repo {repo!r} "
                "(add it under acceptance.drivers in coordinator.yml)",
                err=True,
            )
        sys.exit(1)
    return driver_cfg


def _check_local_capability(driver_cfg, repo: str, cfg) -> None:
    """Fail loudly (#966) when this host is about to run *repo*'s acceptance
    driver but lacks the capability it declares, and some other configured
    machine has it. There's no remote-exec plumbing yet to actually route
    the run there — see :func:`coord.acceptance.acceptance_capability_gap` —
    so the best available behavior is a clear, actionable error instead of
    silently executing on hardware that may not support the driver.
    """
    gap = acceptance_capability_gap(driver_cfg.capability, repo, cfg)
    if gap is None:
        return
    click.echo(
        f"error: this host lacks the {driver_cfg.capability!r} capability "
        f"required by {repo!r}'s acceptance driver ({driver_cfg.kind}); "
        f"{gap.name!r} has it. Capability-matched remote routing isn't "
        "implemented yet (#966) — run this command on that machine directly.",
        err=True,
    )
    sys.exit(1)


def _refuse_if_thin_client_mock() -> None:
    """Refuse ``coord acceptance mock`` outright when run from a thin client
    (#2018, #2748 IL-2).

    ``dispatch_acceptance_mock`` (``coord/mock_author.py``) fetches the
    tracking issue and its milestone's open issues straight off ``gh`` —
    :func:`coord.github_ops.get_issue` / ``_fetch_milestone_issues`` are
    plain ``subprocess`` calls, run on whichever machine invokes this
    command, never routed through the daemon the way the board/config reads
    already are (:func:`coord.board_service.read_board`,
    :func:`coord.commands._common._load_config`'s ``fetch_remote_config``
    branch). The operator's laptop — the thin client from which the
    customer/oracle loop is actually driven (docs/ORACLE_LOOP.md) — is
    exactly the machine :mod:`coord.client`'s bootstrap contract documents
    as carrying "no Claude or gh credentials", so a ``gh`` call issued from
    there is either missing, unauthenticated, or (worse) silently talking
    to a DIFFERENT GitHub identity than the daemon uses for everything
    else. Refusing loudly here, before any of that runs, beats a Gate-A
    dispatch that quietly did nothing (#2018's report: "exit 0, no output,
    no dispatch") or one that ran with the wrong credentials and nobody
    noticed.

    Mirrors ``coord.commands.portal._refuse_if_thin_client`` — same
    detection (``board_service`` configured means this machine is a thin
    client per ``coord/client.py``'s bootstrap contract), same "name the
    daemon host, tell them to ssh there" remedy. Unlike ``coord portal
    link`` (#2751), this does NOT route the whole command through the
    daemon: that would need a new server endpoint (mirroring
    ``/acceptance-record``) that is out of scope for closing #2018 — the
    fix here is "fail loud instead of running wrong or not at all", not new
    remote-exec plumbing.
    """
    from coord.board_service import resolve  # noqa: PLC0415

    svc = resolve()
    if svc is None:
        return
    from urllib.parse import urlparse  # noqa: PLC0415

    host = urlparse(svc.url).hostname or svc.url
    raise click.ClickException(
        f"coord acceptance mock must run on the daemon host ({host}) — "
        "dispatching Gate A fetches the tracking issue and its milestone's "
        "open issues straight off `gh`, which this thin client carries no "
        "credentials for (board_service is configured in "
        "~/.coord/client.toml, making this machine a thin client). Run it "
        "over `ssh` on the daemon host instead. (#2018)"
    )


def _scoped_verdict(
    tests: list[dict],
    acceptance_root: Path,
    issue_number: int,
    *,
    entrypoint: str = "",
) -> dict:
    """Filter *tests* down to *issue_number*'s manifest slice, or exit(1) with
    a clear message when the manifest / slice doesn't exist yet.

    #1552: when the manifest's test-ids don't all show up in the driver's
    output, name the failure instead of leaving it as a bare
    ``total=0, green=false``. That combination has exactly one common cause
    on an entry-point-linked driver — the slice was authored but never
    registered in the driver's crate root, so cargo compiled it into
    nothing — and a verdict that only says "not green" sends the next round
    hunting for a test failure that never ran. The missing ids are recorded
    on the verdict as ``missing_ids`` and, when nothing at all ran, as a
    ``reason`` string.
    """
    manifest = load_manifest(acceptance_root)
    if not manifest:
        click.echo(f"error: {dump_manifest_error_hint(acceptance_root)}", err=True)
        sys.exit(1)
    ids = test_ids_for_issue(manifest, issue_number)
    if not ids:
        click.echo(
            f"error: issue #{issue_number} has no acceptance slice in the "
            "manifest yet.",
            err=True,
        )
        sys.exit(1)
    scoped = [t for t in tests if t["id"] in ids]
    verdict = build_verdict(scoped, scope="issue", issue_number=issue_number)

    missing = sorted(ids - {t["id"] for t in scoped})
    if missing:
        verdict["missing_ids"] = missing
        wiring_hint = (
            f" Register it in the driver's entry point `{entrypoint}` "
            "(acceptance.drivers.<repo>[.routes[]].entrypoint) — a slice that "
            "isn't registered there is compiled into nothing and reports no "
            "tests at all."
            if entrypoint else
            " The slice is not being discovered by the driver's run command."
        )
        if not scoped:
            reason = (
                f"issue #{issue_number}'s manifest lists {len(ids)} test-id(s) "
                f"({', '.join(sorted(ids))}), NONE of which appeared in the "
                "driver output — the slice is authored but never executed, so "
                "this is a wiring failure, not a test failure." + wiring_hint
            )
            verdict["reason"] = reason
            click.echo(f"error: {reason}", err=True)
        else:
            click.echo(
                f"warning: {len(missing)} of issue #{issue_number}'s "
                f"{len(ids)} manifest test-id(s) did not appear in the driver "
                f"output ({', '.join(missing)}) — they never ran." + wiring_hint,
                err=True,
            )
    return verdict


@acceptance_group.command("run")
@click.option("--repo", required=True, help="Local repo name (coordinator.yml repos[].name).")
@click.option(
    "--issue", "issue_number", type=int, default=None,
    help="Issue number to scope the verdict to (mutually exclusive with --all).",
)
@click.option(
    "--all", "run_all", is_flag=True,
    help="Run + report the full accumulated suite (Gate C) instead of one issue's slice.",
)
@click.option(
    "--path", "path_opt", type=click.Path(file_okay=False), default=None,
    help="Repo checkout to run the driver in (default: current directory).",
)
@click.option(
    "--for-path", "route_path", default=None,
    help=(
        "Repo-relative path (e.g. 'coord/foo.py') used to resolve a "
        "routed acceptance driver (acceptance.drivers.<repo>.routes) — "
        "required when the repo's driver is routed; unused/ignored for a "
        "flat (unrouted) driver. NOT the same as --path (the checkout dir)."
    ),
)
@click.option(
    "--ci", "ci_mode", is_flag=True,
    help=(
        "#2164: the CI wrapper. Only valid with --all. Honours each "
        "ms-NN/manifest.yml AND manifest.d/<issue>.yml fragment's (#2543) "
        "`expected_red:` registry — a listed test-id "
        "that FAILS does not fail the run (a sealed slice is red by design "
        "before its fix exists); one that PASSES is a hard, loud failure "
        "(the vacuous-assertion case #1965 cares about). Point your repo's "
        "ordinary CI test command at this instead of the raw driver command "
        "for the acceptance target to merge a red-by-design slice without "
        "`--force-merge` or reddening the default branch."
    ),
)
@_CONFIG_OPTION
def acceptance_run(
    repo: str,
    issue_number: int | None,
    run_all: bool,
    path_opt: str | None,
    route_path: str | None,
    ci_mode: bool,
    config_path: Path,
) -> None:
    """Run REPO's sealed acceptance suite and print a structured verdict.

    The in-session command a worker runs to check itself: ``coord acceptance
    run --issue N`` iterates against the sealed oracle without needing to see
    inside it — only pass/fail + failure messages are ever printed, never
    test source.
    """
    if not run_all and issue_number is None:
        click.echo("error: pass --issue N or --all", err=True)
        sys.exit(1)
    if run_all and issue_number is not None:
        click.echo("error: --issue and --all are mutually exclusive", err=True)
        sys.exit(1)
    if ci_mode and not run_all:
        # #2164: --ci only makes sense against the full accumulated suite.
        # Scoped to one issue it would let a worker's own in-progress fix
        # "pass" its assigned red tests without fixing anything — the
        # expected_red registry exists for OTHER runs (later PRs, the
        # default branch) to tolerate this issue's designed-in redness, not
        # for the issue's own oracle loop to stop converging.
        click.echo("error: --ci requires --all", err=True)
        sys.exit(1)

    cfg = _load_config(config_path)
    driver_cfg = _resolve_driver(cfg, repo, route_path)
    _check_local_capability(driver_cfg, repo, cfg)
    cwd = Path(path_opt).expanduser() if path_opt else Path.cwd()

    # #2038: name the commit this verdict is actually about, and warn (never
    # gate — the worker loop legitimately runs against uncommitted work) when
    # it's behind origin's default branch. Best-effort/non-fatal by design.
    repo_entry = cfg.repo(repo)
    default_branch = (getattr(repo_entry, "default_branch", None) or "main") if repo_entry else "main"
    for line in _checkout_freshness_lines(cwd, repo, default_branch):
        click.echo(line, err=True)

    # #2896: the manifests/contracts a resolved driver actually reads live
    # beside its own entrypoint when it has one (an entrypoint-linked driver
    # like tui-tuidriver `include!`s its slices from a sibling `acceptance/`
    # dir, relocated out of the repo-root tree so the crate is
    # self-contained) — NOT unconditionally under the shared repo-root
    # ACCEPTANCE_DIRNAME, which now only holds a directory-discovered
    # driver's (cli-pytest's) own slices.
    acceptance_root = acceptance_root_for_driver(cwd, driver_cfg.entrypoint)

    # #1125 review finding 2: resolve the `{ms}` template (e.g. a routed
    # `run: "pytest tests/acceptance/{ms}"`) from the issue's manifest-mapped
    # ms-NN dir *before* running, when scoped to one issue. Fails soft to
    # `ms=None` on any manifest read hiccup (malformed YAML, not authored
    # yet) — `_scoped_verdict` below still surfaces a clear error for that
    # case; this must not turn into a crash on its own.
    ms: str | None = None
    if issue_number is not None:
        try:
            ms = ms_dir_for_issue(acceptance_root, issue_number)
        except Exception:  # noqa: BLE001
            ms = None

    try:
        result = run_driver(
            driver_cfg.kind, driver_cfg.run, cwd=str(cwd), ms=ms,
            setup_command=driver_cfg.setup,
        )
    except DriverError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not result.tests and result.exit_code != 0:
        # Compile error / crash before any test emitted a verdict — surface
        # the driver's raw output so the worker can actually act on it,
        # rather than a bare "0 tests found".
        click.echo(result.raw_output, err=True)

    if run_all:
        verdict = build_verdict(result.tests, scope="all")
    else:
        verdict = _scoped_verdict(
            result.tests, acceptance_root, issue_number,
            entrypoint=driver_cfg.entrypoint,
        )

    if ci_mode:
        # #2339: scope the expected_red registry to milestones this run's
        # OWN driver could actually produce results for — a routed repo's
        # `--all --ci` used to merge every milestone's expected_red
        # regardless of which driver `--for-path` resolved, so every OTHER
        # driver's ids always came back `missing_expected_red_ids` (a hard
        # CI failure) the moment the repo had more than one driver kind in
        # use. See coord.acceptance.load_expected_red's docstring.
        expected_red = load_expected_red(
            acceptance_root, driver_kind=driver_cfg.kind,
        )
        verdict = apply_expected_red(verdict, set(expected_red))
        click.echo(json.dumps(verdict, indent=2))
        hard_failure = expected_red_failure_summary(verdict)
        if hard_failure:
            click.echo(f"\n{hard_failure}", err=True)
        if verdict["expected_red_still_red"]:
            click.echo(
                f"\n{len(verdict['expected_red_still_red'])} test(s) failed as "
                "expected (listed in `expected_red`) — not a CI failure: "
                f"{', '.join(verdict['expected_red_still_red'])}",
                err=True,
            )
        if verdict.get("missing_expected_red_ids"):
            click.echo(
                f"\nHARD FAILURE: {len(verdict['missing_expected_red_ids'])} "
                "test-id(s) listed in `expected_red` never appeared in the "
                "driver output at all: "
                f"{', '.join(verdict['missing_expected_red_ids'])}. Neither "
                "a pass nor a fail was observed — the entry point may be "
                "broken or the test deleted. This is NOT the same as an "
                "ordinary failure; investigate before assuming the slice "
                "is fine.",
                err=True,
            )
        if not verdict["ci_green"]:
            sys.exit(1)
        return

    click.echo(json.dumps(verdict, indent=2))
    if verdict["total"] == 0 or not verdict["green"]:
        sys.exit(1)


def _checkout_freshness_lines(cwd: Path, repo: str, default_branch: str) -> list[str]:
    """Best-effort ``acceptance: {repo} @ {sha} ({branch})`` header plus a
    dirty-tree / behind-``origin/{default_branch}`` warning line for each
    condition that applies (#2038).

    ``coord acceptance run`` answers "is the sealed suite green" for
    *whatever is in the current checkout*, with no indication of which
    commit that is — exactly right for a worker's own warm loop (it
    legitimately runs against uncommitted work), but a false-signal
    generator for a coordinator's red-first / sanity re-run against a
    checkout that quietly fell behind ``origin``: every test 404s or fails
    and nothing in the output names a commit, so it reads as a broken merge
    rather than a stale tree. This stays informational (a warning, never a
    gate) — failing closed here would break the worker loop.

    Silently returns ``[]`` (no header at all) when *cwd* isn't a git
    checkout, or any git call errors/times out — this must never turn an
    otherwise-successful acceptance run into a crash, and a worker running
    in a network-isolated sandbox shouldn't see a scary "git fetch failed"
    on every single invocation.
    """
    import subprocess  # noqa: PLC0415 — mirrors the rest of this module's lazy subprocess imports

    def _git(*args: str, timeout: float = 10.0) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return None

    head = _git("rev-parse", "--short=9", "HEAD")
    if head is None or head.returncode != 0:
        return []
    sha = head.stdout.strip()

    branch_res = _git("branch", "--show-current")
    branch = (branch_res.stdout.strip() if branch_res and branch_res.returncode == 0 else "") \
        or "HEAD (detached)"

    lines = [f"acceptance: {repo} @ {sha} ({branch})"]

    status_res = _git("status", "--porcelain")
    if status_res is not None and status_res.returncode == 0 and status_res.stdout.strip():
        lines.append("  ⚠ working tree has uncommitted changes")

    # Single-branch fetch (never touches the working tree or local branch
    # refs) so the behind-check reflects the ACTUAL remote, not whatever
    # `origin/<default_branch>` happened to be cached from a previous
    # fetch elsewhere — that staleness is exactly what bit #2038.
    fetch_res = _git("fetch", "origin", default_branch, "--quiet", timeout=20.0)
    if fetch_res is not None and fetch_res.returncode == 0:
        count_res = _git("rev-list", "--count", "HEAD..FETCH_HEAD")
        if count_res is not None and count_res.returncode == 0:
            raw = count_res.stdout.strip()
            if raw.isdigit() and int(raw) > 0:
                n = int(raw)
                lines.append(
                    f"  ⚠ checkout is {n} commit{'s' if n != 1 else ''} behind "
                    f"origin/{default_branch} — `git pull` before trusting a "
                    "red/green verdict"
                )
    return lines


def _acceptance_worktree_path(repo_name: str, issue_number: int) -> Path:
    """Throwaway worktree path for ``coord acceptance record``'s external
    re-run.  Lives under ``~/.coord/acceptance-worktrees/`` — OUTSIDE the base
    checkout, same rationale as ``coord test``'s ``_test_worktree_path``
    (#561): a Build/record must never move the base checkout's branch (it
    doubles as the live editable coordinator source on some machines)."""
    from coord.state import COORD_DIR

    return COORD_DIR / "acceptance-worktrees" / f"{repo_name}-{issue_number}"


def _acceptance_worktree_lock_path(repo_name: str, issue_number: int) -> Path:
    """The ``flock`` guarding :func:`_acceptance_worktree_path` (#2352).

    That path is deliberately ONE per ``(repo, issue)``, reused across every
    SHA/round for that issue (same incremental-build rationale as `coord
    test`'s #561) — but nothing serialised *access* to it. Two concurrent
    ``coord acceptance record`` calls for the same issue (an orphaned drive
    process — #1660 — racing a fresh queue relaunch, or a by-hand re-run
    overlapping an in-flight one) each did an unconditional ``git worktree
    remove --force`` + ``add --force`` on the SAME directory: the second
    invocation's remove ripped the tree out from under the first's
    in-flight build/test, mid-compile. The corrupted build then printed
    ordinary-looking compiler errors and exited non-zero having run zero
    tests — indistinguishable from a genuinely red suite to
    ``_scoped_verdict``, so it landed on the board as a real
    ``acceptance_state=failed`` and burned a drive's fix-round budget on a
    PR that was never actually broken.

    A sibling ``.lock`` file next to the worktree directory itself (not one
    global lock for the whole ``acceptance-worktrees/`` tree) so unrelated
    ``(repo, issue)`` pairs never wait on each other.

    Derived from :func:`_acceptance_worktree_path` (module-level lookup, so
    a test monkeypatching that name — e.g. to redirect it under
    ``tmp_path`` — redirects this lock alongside it) rather than
    re-deriving ``COORD_DIR`` independently, which would silently drift the
    two apart the moment either one's path scheme changes."""
    wt_path = _acceptance_worktree_path(repo_name, issue_number)
    return wt_path.with_name(wt_path.name + ".lock")


def _remove_acceptance_worktree(repo_dir: Path, wt_path: Path) -> None:
    if not wt_path.exists():
        return
    for args in (
        ["git", "worktree", "remove", "--force", str(wt_path)],
        ["git", "worktree", "prune"],
    ):
        try:
            subprocess.run(
                args, cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
            )
        except (subprocess.SubprocessError, OSError):
            pass


def _acceptance_record_via_daemon(svc, params: dict) -> None:
    """Run ``coord acceptance record`` on the daemon host (where the
    canonical board + the repo checkouts live) and relay its output.
    Mirrors ``_diagnose_via_daemon`` / ``_reconcile_via_daemon``."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/acceptance-record", params, timeout=900.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: acceptance record via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


@acceptance_group.command(
    "author",
    help=(
        "Dispatch an independent `type=\"test-author\"` session (#931, "
        "docs/ORACLE_LOOP.md) that authors — or, with --issue, extends — the "
        "sealed feature-level acceptance suite for a milestone from its "
        "Gate-A contract. TRACKING_ISSUE is the milestone's tracking issue "
        "number (same argument `coord milestone order`/`gate-c` take); the "
        "milestone number is resolved from it. Requires "
        "`tests/acceptance/ms-NN/contract.md` to already exist in the repo "
        "(hand-authored, or produced by the mock-author, #930) — the "
        "test-author reads it from its own checkout, it is not dispatched "
        "with the contract text embedded."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--issue", "issue_number", type=int, default=None,
    help=(
        "Scope to one issue's just-in-time slice instead of the whole "
        "milestone (must be a member of TRACKING_ISSUE's work order)."
    ),
)
@click.option(
    "--machine", "machine_override", default=None,
    help="Force a specific machine instead of auto-picking one.",
)
@click.option(
    "--for-path", "route_path", default=None,
    help=(
        "Repo-relative path (e.g. 'coord/foo.py') used to resolve a "
        "routed acceptance driver (acceptance.drivers.<repo>.routes) — "
        "required when the repo's driver is routed; unused/ignored for a "
        "flat (unrouted) driver."
    ),
)
@click.option(
    "--interactive", is_flag=True,
    help=(
        "#1173: run the test-authoring session as a HUMAN-ATTENDED "
        "`claude` (provider `claude-pty`) instead of dispatching a "
        "headless `claude -p` worker — same shape as `coord assign "
        "--interactive`'s --smoke-of/--merge-of/etc flavours. The "
        "independence contract (zero shared context with the "
        "implementation, contract-only) is unchanged; only who "
        "supervises the authoring changes."
    ),
)
@click.option(
    "--dry-run", "dry_run", is_flag=True,
    help="With --interactive: resolve everything and print what would run, but don't launch.",
)
@_CONFIG_OPTION
def acceptance_author(
    repo: str,
    tracking_issue: int,
    issue_number: int | None,
    machine_override: str | None,
    route_path: str | None,
    interactive: bool,
    dry_run: bool,
    config_path: Path,
) -> None:
    """Dispatch the independent test-author for REPO's milestone."""
    cfg = _load_config(config_path)

    if interactive:
        from coord.test_author import dispatch_test_author_interactive

        try:
            exit_code = dispatch_test_author_interactive(
                repo,
                tracking_issue,
                cfg,
                issue_number=issue_number,
                machine_override=machine_override,
                path=route_path,
                dry_run=dry_run,
            )
        except DispatchRefused as e:
            # #2063: the Gate-A sign-off refusal is deterministic and
            # operator-fixable (`coord gate-a --approved`), not a crash —
            # exit EXIT_DISPATCH_REFUSED (not the generic 1 below) so
            # `coord drive`'s subprocess boundary and `coord drive-queue`'s
            # tick can tell it apart and PARK the entry (#1891/#1892)
            # instead of burning attempts toward terminal `blocked` (#2040).
            # Mirrors `coord fix`'s/`coord assign`'s existing
            # `except DispatchRefused` handling.
            from coord.drive import EXIT_DISPATCH_REFUSED  # noqa: PLC0415

            click.echo(f"error: {e}", err=True)
            sys.exit(EXIT_DISPATCH_REFUSED)
        except RuntimeError as e:
            click.echo(f"error: {e}", err=True)
            sys.exit(1)
        sys.exit(exit_code)

    if dry_run:
        click.echo("error: --dry-run requires --interactive", err=True)
        sys.exit(2)

    from coord.test_author import dispatch_test_author

    try:
        assignment_id, machine_name = dispatch_test_author(
            repo,
            tracking_issue,
            cfg,
            issue_number=issue_number,
            machine_override=machine_override,
            path=route_path,
        )
    except DispatchRefused as e:
        # #2063: see the matching --interactive branch above — same
        # deterministic-refusal exit code so the headless dispatch path
        # parks instead of blocking too.
        from coord.drive import EXIT_DISPATCH_REFUSED  # noqa: PLC0415

        click.echo(f"error: {e}", err=True)
        sys.exit(EXIT_DISPATCH_REFUSED)
    except RuntimeError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    scope = f"issue #{issue_number} slice" if issue_number is not None else "full milestone"
    click.echo(
        f"Dispatched test-author {assignment_id} to {machine_name} for "
        f"{repo} (tracking issue #{tracking_issue}, {scope})."
    )


@acceptance_group.command("record")
@click.option("--repo", required=True, help="Local repo name (coordinator.yml repos[].name).")
@click.option("--issue", "issue_number", type=int, required=True, help="Issue number.")
@click.option(
    "--sha", "sha", required=True,
    help="Commit SHA to check out and re-run the sealed suite against — the trust gate.",
)
@click.option(
    "--for-path", "route_path", default=None,
    help=(
        "Repo-relative path (e.g. 'coord/foo.py') used to resolve a "
        "routed acceptance driver (acceptance.drivers.<repo>.routes) — "
        "required when the repo's driver is routed; unused/ignored for a "
        "flat (unrouted) driver."
    ),
)
@_CONFIG_OPTION
def acceptance_record(
    repo: str,
    issue_number: int,
    sha: str,
    route_path: str | None,
    config_path: Path,
) -> None:
    """Re-run REPO's issue-N acceptance slice externally against SHA and
    write the verdict to the board (the Acceptance box).

    A headless worker can lie about "green" in its own session; it can't
    fake the coordinator re-running the sealed suite itself, against the
    exact SHA it pushed, in a throwaway worktree the worker never touches.
    """
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    # #944: the canonical board + the repo checkouts live on the daemon host,
    # so a thin client routes the ENTIRE record run there (mirrors `coord
    # merge` / `coord diagnose` — never a bare save_board from a thin
    # client's empty local DB). COORD_ACCEPTANCE_ON_DAEMON guards the daemon
    # against re-routing to itself (set by the /acceptance-record server
    # route before it calls this callback directly).
    svc = daemon_reroute_target("COORD_ACCEPTANCE_ON_DAEMON")
    if svc is not None:
        _acceptance_record_via_daemon(
            svc,
            {
                "repo": repo, "issue": issue_number, "sha": sha,
                "for_path": route_path,
            },
        )
        return

    _acceptance_record_local(repo, issue_number, sha, config_path, route_path)


@acceptance_group.command(
    "expected-red",
    help=(
        "#2164 acceptance criterion 4: list every `expected_red:` entry "
        "currently live on REPO's default branch, via the GitHub API — no "
        "local checkout required. Flags an issue whose GitHub state is "
        "already CLOSED but still carries entries as STUCK: the clearing "
        "PR (coord.acceptance.clear_expected_red_via_pr, fired from `coord "
        "merge` right after that issue's fix merges) may have failed, or "
        "the fix landed by some other path. A long-lived expected_red "
        "entry is exactly the invisible debt this command exists to "
        "surface. #2266: `--clear` acts on it — for every STUCK entry this "
        "listing finds, invoke the same `clear_expected_red_via_pr` PR "
        "path `coord merge` uses, so a registry stuck because the merge-"
        "time trust-gate guards never fired (acceptance never recorded, or "
        "the PR merged out of band) has a re-fire path instead of only a "
        "detector. Never touches an entry whose issue is still open — "
        "those are read as legitimately red, not stuck."
    ),
)
@click.argument("repo")
@_CONFIG_OPTION
@click.option(
    "--clear", "do_clear", is_flag=True, default=False,
    help=(
        "Open+merge the expected_red-clearing PR for every STUCK entry "
        "this listing finds (issue closed, entries still live). Skips — "
        "never clears — any entry whose issue is still open."
    ),
)
@click.option(
    "--issue", "only_issue", type=int, default=None,
    help="With --clear, restrict clearing to this issue number.",
)
def acceptance_expected_red(
    repo: str, config_path: Path, do_clear: bool, only_issue: int | None,  # noqa: FBT001
) -> None:
    if only_issue is not None and not do_clear:
        # #2266 review nit: `--issue` only narrows what `--clear` acts on
        # (per its own help text) — silently accepting it without `--clear`
        # looks like it did something when it didn't.
        click.echo(
            "warning: --issue has no effect without --clear; the listing "
            "below covers every issue.",
            err=True,
        )

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    # #2896 review: sweep EVERY acceptance search root this repo declares,
    # not just the shared repo-root tree — a relocated (entrypoint-linked)
    # milestone's slices live beside their driver's entrypoint (e.g.
    # `tui/tests/acceptance/ms-65/`), and omitting those roots is exactly
    # the "long-lived expected_red entry is invisible debt" failure this
    # command exists to prevent (#2164 acceptance criterion 4).
    search_roots = search_roots_for_repo(cfg, repo)
    by_ms = list_expected_red_via_api(
        repo_entry.github, repo_entry.default_branch, search_roots=search_roots,
    )
    if not by_ms:
        click.echo(f"no expected_red entries on {repo}@{repo_entry.default_branch}.")
        return

    total = 0
    # #2266: STUCK entries found while rendering the listing above — the
    # exact set `--clear` acts on, so the remedy can never drift from the
    # detector that names the debt.
    stuck: list[tuple[str, int, frozenset[str]]] = []
    for ms, by_issue in sorted(by_ms.items()):
        click.echo(f"{ms}:")
        for issue_number, ids in sorted(by_issue.items()):
            total += len(ids)
            closed_note = ""
            is_stuck = False
            try:
                issue_data = github_ops.get_issue(repo_entry.github, issue_number)
                if str((issue_data or {}).get("state", "")).lower() == "closed":
                    closed_note = "  [STUCK: issue is closed but entries remain]"
                    is_stuck = True
            except Exception:  # noqa: BLE001 — a lookup hiccup shouldn't hide the entry itself
                pass
            click.echo(
                f"  #{issue_number}: {', '.join(sorted(ids))}{closed_note}"
            )
            if is_stuck:
                stuck.append((ms, issue_number, ids))
    click.echo(f"\n{total} expected_red test-id(s) across "
               f"{sum(len(v) for v in by_ms.values())} issue(s).")

    if not do_clear:
        return

    _clear_stuck_expected_red(
        repo, repo_entry, stuck, only_issue, search_roots=search_roots,
    )


def _clear_stuck_expected_red(
    repo: str, repo_entry: Repo, stuck: list[tuple[str, int, frozenset[str]]],
    only_issue: int | None,
    *,
    search_roots: list[str] | None = None,
) -> None:
    """#2266: the remedy half of `coord acceptance expected-red --clear`.

    Acts ONLY on *stuck* — the STUCK (issue closed, entries still live) set
    the listing above already computed — never on an issue whose entries
    are legitimately still red (still open). `--issue` narrows to one
    issue but does not widen scope: naming an open issue's number here
    finds nothing to clear, same as naming one that isn't in *stuck* at
    all.

    Exits non-zero when at least one entry hard-fails to clear (#2266
    review, non-blocking finding: without this, a caller scripting this
    command in CI/cron could only detect a fully-failed run by reading
    stdout or separately querying the audit log).

    *search_roots* (#2896 review): the same roots the listing above swept,
    forwarded so the clear resolves a relocated (entrypoint-linked)
    milestone's manifest. Passing the caller's roots rather than
    re-deriving them here keeps detector and remedy on identical scope —
    a `--clear` that could not find what the listing just named would be
    the split-brain #2266 built the shared classifier to avoid.
    """
    from coord.acceptance import (  # noqa: PLC0415
        classify_expected_red_clear_result,
        clear_expected_red_via_pr,
    )
    from coord.audit import record_audit  # noqa: PLC0415

    if only_issue is not None:
        stuck = [s for s in stuck if s[1] == only_issue]
        if not stuck:
            click.echo(
                f"\n--issue {only_issue}: not STUCK (open, or no "
                "expected_red entries recorded for it) — nothing to clear."
            )
            return

    if not stuck:
        click.echo("\nno STUCK entries to clear.")
        return

    click.echo(f"\nclearing {len(stuck)} STUCK issue(s)...")
    # #2266 review (blocking finding 2): classify through the same shared
    # helper `coord.merge_queue._maybe_clear_expected_red` uses, instead of
    # each surface independently re-deriving "did this succeed?" from the
    # message text — a split-brain waiting to happen if the message
    # wording ever changes (epic #2096's "one question, one answer").
    any_failed = False
    for ms, issue_number, ids in stuck:
        msg = clear_expected_red_via_pr(
            repo_entry.github, repo, repo_entry.default_branch, issue_number,
            gh_ops=github_ops, search_roots=search_roots,
        )
        click.echo(f"  #{issue_number}: {msg}")
        status = classify_expected_red_clear_result(msg)
        if status == "no_op":
            # These entries were just listed as STUCK, so this should be
            # rare (e.g. a race — another process already cleared them) —
            # but it's still not a failure worth a durable audit row.
            continue
        if status == "failed":
            any_failed = True
        event_type = {
            "cleared": "expected_red_clear",
            "pending_retry": "expected_red_clear_pending",
            "failed": "expected_red_clear_failed",
        }[status]
        # #2266 scope 2: a failed clear must land somewhere durable — the
        # audit log — so the next pass over this repo can say "this
        # registry is still stuck" without anyone re-reading this output.
        record_audit(
            tier="business",
            category="acceptance",
            event_type=event_type,
            actor="user",
            summary=f"coord acceptance expected-red --clear {repo} #{issue_number}: {msg}",
            repo=repo,
            issue=issue_number,
            details={"ms": ms, "test_ids": sorted(ids), "result": msg},
        )

    if any_failed:
        sys.exit(1)


def _acceptance_record_local(
    repo: str,
    issue_number: int,
    sha: str,
    config_path: Path,
    route_path: str | None = None,
) -> None:
    from coord.filelock import FileLock  # noqa: PLC0415
    from coord.test_orchestrator import find_local_repo_path  # noqa: PLC0415

    cfg = _load_config(config_path)
    driver_cfg = _resolve_driver(cfg, repo, route_path)
    _check_local_capability(driver_cfg, repo, cfg)

    repo_dir = find_local_repo_path(repo, cfg)
    if repo_dir is None or not repo_dir.exists():
        click.echo(
            f"error: no local repo checkout found for {repo!r} "
            "(repo_paths in coordinator.yml)",
            err=True,
        )
        sys.exit(1)

    wt_path = _acceptance_worktree_path(repo, issue_number)

    # #2352: everything from here down touches the ONE worktree this
    # (repo, issue) reuses across every SHA/round — remove+add it, build in
    # it, read its results. A second concurrent `coord acceptance record`
    # for the same issue (an orphaned drive racing a fresh queue relaunch,
    # or a by-hand re-run overlapping an in-flight one — see
    # _acceptance_worktree_lock_path's docstring) must never interleave
    # with this, or its `git worktree remove --force` clobbers whatever the
    # first invocation is mid-build on. Blocking (not fail-fast) is
    # deliberate: `flock` is released by the kernel even if the holder is
    # killed, so there is no stale-lock deadlock risk to trade away, and
    # blocking needs no change to `_decide_acceptance_gate`'s error
    # handling — the second invocation just waits its turn and then records
    # its own (correct) verdict, same as if it had been dispatched later.
    with FileLock(_acceptance_worktree_lock_path(repo, issue_number)):
        click.echo(
            f"Fetching origin and preparing acceptance worktree at {sha!r} "
            f"(base checkout {repo_dir} stays untouched)..."
        )
        try:
            subprocess.run(
                ["git", "fetch", "origin", "--prune"], cwd=str(repo_dir),
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            click.echo(f"error: git fetch failed: {e.stderr.strip()}", err=True)
            sys.exit(1)

        _remove_acceptance_worktree(repo_dir, wt_path)
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            ["git", "worktree", "add", "--force", "--detach", str(wt_path), sha],
            cwd=str(repo_dir), capture_output=True, text=True,
        )
        if res.returncode != 0:
            click.echo(
                f"error: could not create acceptance worktree at {sha!r}: "
                f"{res.stderr.strip()}",
                err=True,
            )
            sys.exit(1)

        # #2896: same per-driver root resolution as `acceptance_run` above —
        # an entrypoint-linked driver's manifests live beside its entrypoint,
        # not unconditionally under the shared repo-root ACCEPTANCE_DIRNAME.
        acceptance_root = acceptance_root_for_driver(wt_path, driver_cfg.entrypoint)

        # #1125 review finding 2: resolve `{ms}` from the issue's
        # manifest-mapped ms-NN dir (the worktree just checked out at
        # `sha`) before running — same fail-soft-to-None rationale as
        # `acceptance_run` above.
        ms: str | None = None
        try:
            ms = ms_dir_for_issue(acceptance_root, issue_number)
        except Exception:  # noqa: BLE001
            ms = None

        try:
            result = run_driver(
                driver_cfg.kind, driver_cfg.run, cwd=str(wt_path), ms=ms,
                setup_command=driver_cfg.setup,
            )
        except DriverError as e:
            click.echo(f"error: {e}", err=True)
            _remove_acceptance_worktree(repo_dir, wt_path)
            sys.exit(1)

        # #944 review: _scoped_verdict exits(1) internally for a manifest
        # that hasn't been authored yet / has no slice for this issue — a
        # configuration error, not a real (kept-for-inspection) test
        # failure, so the throwaway worktree must still be cleaned up on
        # the way out.
        try:
            verdict = _scoped_verdict(
                result.tests, acceptance_root, issue_number,
                entrypoint=driver_cfg.entrypoint,
            )
        except SystemExit:
            _remove_acceptance_worktree(repo_dir, wt_path)
            raise

        from coord.board_service import read_board  # noqa: PLC0415
        from coord.diagnose import stage_assignments  # noqa: PLC0415

        board = read_board()
        work_rows = stage_assignments(board, repo, issue_number, "work")
        if not work_rows:
            click.echo(
                f"error: no work assignment found for {repo} #{issue_number}; "
                "cannot record verdict",
                err=True,
            )
            # Same rationale: a lookup error, not a failing-verdict "kept
            # for inspection" case — don't leak the worktree.
            _remove_acceptance_worktree(repo_dir, wt_path)
            sys.exit(1)
        assignment_id = work_rows[0].assignment_id

        from coord.state import record_acceptance_verdict  # noqa: PLC0415

        acceptance_state = "passed" if verdict["green"] else "failed"
        reason = failure_summary(verdict) or None
        record_acceptance_verdict(
            assignment_id=assignment_id,
            acceptance_state=acceptance_state,
            acceptance_reason=reason,
            acceptance_sha=sha,
            # #932: per-test counts so the Acceptance box can show partial
            # progress ("3/7 acceptance green") instead of a bare verdict.
            acceptance_total=verdict["total"],
            acceptance_passed=verdict["passed"],
        )

        click.echo(json.dumps(verdict, indent=2))
        click.echo(
            f"\nAcceptance {acceptance_state.upper()} for {repo} #{issue_number} @ {sha}"
        )

        if acceptance_state == "passed":
            # #2164 review: clearing expected_red here (before
            # Test/Review/the actual merge to the default branch have
            # happened) was the bug — it could reopen "red default branch"
            # for reasons unrelated to whatever CI run next observed this
            # issue's still-unmerged fix. The clear now happens in
            # `coord.merge_queue.process`, right after this SHA's PR
            # actually merges — see
            # `coord.acceptance.clear_expected_red_via_pr`'s docstring.
            # Nothing to do here beyond pointing the operator at how to
            # check.
            if ms is not None:
                click.echo(
                    f"  note: any expected_red entries for #{issue_number} clear "
                    f"automatically once its fix merges — see `coord acceptance "
                    f"expected-red {repo}` to check current state."
                )
            _remove_acceptance_worktree(repo_dir, wt_path)
        else:
            click.echo(f"  worktree kept for inspection: {wt_path}")
            sys.exit(1)


def _stall_push_wip_snapshot(cwd: Path) -> str:
    """Best-effort WIP snapshot push (#846 worker self-report).

    Not the coordinator's remote-exec finalize path
    (``coord.interactive.finalize_remote_interactive_exit`` — that's for a
    *remote* interactive fix session over ssh, the wrong shape here since
    this runs inside the worker's own local checkout) — just a plain
    ``git push`` of whatever is on the current branch, so nothing is lost if
    the coordinator takes over. Never raises: a worker calling ``stall`` is
    already stuck, and a push failure shouldn't block the rest of the
    report.
    """
    try:
        branch_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd), capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"WIP push skipped: could not resolve branch ({exc})."
    branch = branch_res.stdout.strip()
    if branch_res.returncode != 0 or not branch or branch == "HEAD":
        return "WIP push skipped: not on a branch (detached HEAD)."
    try:
        push_res = subprocess.run(
            ["git", "push", "origin", f"HEAD:{branch}"],
            cwd=str(cwd), capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"WIP push failed (branch `{branch}`): {exc}"
    if push_res.returncode == 0:
        return f"WIP snapshot pushed to `{branch}`."
    return f"WIP push failed (branch `{branch}`): {push_res.stderr.strip()[:200]}"


@acceptance_group.command(
    "stall",
    help=(
        "Worker self-report (#846, preferred over a coordinator wall-clock "
        "backstop): call this when your acceptance slice for REPO #ISSUE "
        "isn't converging — the failing-set churns rather than shrinks "
        "across >=2 rounds. Records a pinned #603 context note, "
        "best-effort pushes a WIP snapshot of the current branch, and "
        "posts the same one-shot 'needs attention' GitHub comment the "
        "coordinator's backstop (coord.notify.detect_needs_attention) "
        "would otherwise post later. This is the 'stop grinding and "
        "report it' step the oracle-loop contract "
        "(coord.acceptance.oracle_loop_contract_block) points workers at."
    ),
)
@click.option("--repo", required=True, help="Local repo name (coordinator.yml repos[].name).")
@click.option("--issue", "issue_number", type=int, required=True, help="Issue number.")
@click.option(
    "--tried", required=True,
    help="What you tried across the churning rounds (one or two sentences).",
)
@click.option(
    "--stuck", required=True,
    help="Which test id(s)/behavior are still failing and why, as best understood.",
)
@click.option(
    "--path", "path_opt", type=click.Path(file_okay=False), default=None,
    help="Repo checkout to push the WIP snapshot from (default: current directory).",
)
@_CONFIG_OPTION
def acceptance_stall(
    repo: str,
    issue_number: int,
    tried: str,
    stuck: str,
    path_opt: str | None,
    config_path: Path,
) -> None:
    """Report that REPO #ISSUE's acceptance slice isn't converging."""
    from coord.board_service import read_board  # noqa: PLC0415
    from coord.diagnose import stage_assignments  # noqa: PLC0415
    from coord.state import add_issue_context_entry, mark_needs_attention_notified  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    cwd = Path(path_opt).expanduser() if path_opt else Path.cwd()
    push_note = _stall_push_wip_snapshot(cwd)

    tried = tried.strip()
    stuck = stuck.strip()
    note = f"Acceptance stall reported. Tried: {tried} Stuck: {stuck} {push_note}".strip()
    add_issue_context_entry(repo, issue_number, note, pinned=True, source="acceptance-stall")

    board = read_board()
    work_rows = stage_assignments(board, repo, issue_number, "work")
    work = work_rows[0] if work_rows else None

    body = format_needs_attention(
        assignment_id=(work.assignment_id if work else None) or "",
        machine_name=(work.machine_name if work else None) or "(self-reported)",
        repo_name=repo,
        issue_number=issue_number,
        reason="non_convergence",
        detail=(
            "Acceptance slice not converging (worker self-report).\n\n"
            f"**Tried:** {tried}\n\n**Stuck:** {stuck}\n\n{push_note}"
        ),
    )
    try:
        github_ops.post_issue_comment(repo_entry.github, issue_number, body)
    except Exception as exc:  # noqa: BLE001 — the context note above already
        # landed; a comment-post failure shouldn't turn this into a hard error.
        click.echo(f"warning: could not post needs-attention comment: {exc}", err=True)
    else:
        # #846 review: share the notified-ledger with the coordinator's
        # wall-clock backstop (coord.notify.detect_needs_attention) so this
        # self-report is a true one-shot — otherwise the same assignment
        # stays eligible and can get a second "needs attention" comment
        # later. Skip when no work assignment id was resolved (matches the
        # existing blank-assignment-id test case).
        if work is not None:
            mark_needs_attention_notified(work.assignment_id)

    click.echo(f"Recorded acceptance stall for {repo} #{issue_number}.")
    click.echo(f"  {push_note}")


@acceptance_group.command(
    "mock",
    help=(
        "Gate A (#930, docs/ORACLE_LOOP.md): dispatch an independent "
        "mock-author agent that renders a viewable mock of the milestone's "
        "user-facing surface and writes tests/acceptance/ms-NN/contract.md "
        "— the black-box contract the milestone's workers and the "
        "independent test-author (#931) implement/test to. REPO is the "
        "local repo name from coordinator.yml; TRACKING_ISSUE is the GH "
        "issue number of the milestone's tracking issue (must carry a "
        "milestone). `coord milestone dispatch` refuses this milestone's "
        "issues until the contract this produces exists. Pass --amend (or "
        "--amend-file) to instead dispatch a targeted correction to an "
        "ALREADY-MERGED contract — the properly-typed tool for that #1315 "
        "adds, replacing the type=\"work\" fallback that caused #1314."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first idle machine that lists the repo).",
)
@click.option(
    "--for-path", "route_path", default=None,
    help=(
        "Repo-relative path (e.g. 'coord/foo.py') used to resolve a "
        "routed acceptance driver (acceptance.drivers.<repo>.routes) — "
        "required when the repo's driver is routed; unused/ignored for a "
        "flat (unrouted) driver."
    ),
)
@click.option(
    "--amend", "amend_text", default=None,
    help=(
        "#1315: targeted amendment mode — dispatch a narrow mock-author "
        "session that corrects the ALREADY-MERGED contract.md/mocks under "
        "tests/acceptance/ms-NN/, using this exact text as the correction "
        "to make, instead of doing a full fresh render from the "
        "milestone's open issues. This is the properly-typed replacement "
        "for falling back to a plain `coord assign` (type=\"work\") to fix "
        "a small contract mistake (#1314's root cause). Mutually "
        "exclusive with --amend-file."
    ),
)
@click.option(
    "--amend-file", "amend_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read the --amend correction text from a file instead of the command line.",
)
@_CONFIG_OPTION
def acceptance_mock_cmd(
    repo: str,
    tracking_issue: int,
    machine: str | None,
    route_path: str | None,
    amend_text: str | None,
    amend_file: Path | None,
    config_path: Path,
) -> None:
    _refuse_if_thin_client_mock()

    if amend_text is not None and amend_file is not None:
        click.echo("error: --amend and --amend-file are mutually exclusive", err=True)
        sys.exit(2)
    if amend_file is not None:
        amend_text = amend_file.read_text()

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord.mock_author import dispatch_acceptance_mock

    try:
        assignment_id, picked_machine = dispatch_acceptance_mock(
            repo,
            tracking_issue,
            cfg,
            machine_override=machine,
            path=route_path,
            amend_briefing=amend_text,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    verb = "amend" if amend_text is not None else "mock-author"
    click.echo(f"Dispatched {verb} for #{tracking_issue} -> {picked_machine}")
    click.echo(assignment_id)
