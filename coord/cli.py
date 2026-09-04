"""Click CLI entry point for the `coord` command.

#747: this module just builds the `main`/`agent`/`issue`/`context` groups and
registers the commands implemented in coord/commands/*.py — the actual
command bodies (~70 commands across ~12k lines pre-#747) now live in those
focused modules. Keep this file thin: new commands belong in
coord/commands/<area>.py, imported and attached here in one place.
"""

from __future__ import annotations

# Compatibility shims, not used directly by this file: a number of existing
# tests patch e.g. "coord.cli.subprocess.run" / "coord.cli.socket.gethostname"
# / "coord.cli.httpx.get" / "coord.cli.Path.home". Those patches work by
# replacing an attribute on the *shared* stdlib/third-party module or class
# object (the same object every coord.commands.* module below imports), so
# keeping these imports here — even though cli.py itself no longer calls
# them — keeps those existing test-patch targets resolving. noqa: F401
import os  # noqa: F401
import shutil  # noqa: F401
import socket  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
from pathlib import Path  # noqa: F401

import click
import httpx  # noqa: F401

from coord import __version__
from coord.dist_name import pkg_spec as _dist_pkg_spec

from coord.commands.acceptance import acceptance_group
from coord.commands.audit import audit
from coord.commands.gate_a import gate_a
from coord.commands.report import report_group
from coord.commands.scorecard import scorecard

# Re-exported for back-compat: some tests do `from coord.cli import
# _save_config_snapshot` / `_load_config` / etc. directly.
from coord.commands._common import (  # noqa: F401
    AGENT_PORT,
    SERVE_PORT,
    _apply_label_change,
    _CONFIG_OPTION,
    _load_config,
    _not_implemented,
    _save_config_snapshot,
)

from coord.commands.setup import (
    _ensure_coord_permissions,  # noqa: F401 — re-exported for tests
    _parse_github_remote,  # noqa: F401 — re-exported for tests
    config_cmd,
    init,
    install_skills,
    store_backend_cmd,
    version,
)
from coord.commands.agent_ops import agent, pause, quiet_hours, unpause
from coord.commands.gates import gates
from coord.commands.status import diagnose, doctor, show_plan, status, usage
from coord.commands.dispatch import (
    approve,
    assign,
    chat_continue,
    inject,
    plan,
    retry,
    stop,
)
from coord.commands.sessions import (
    _prune_dead_sessions,  # noqa: F401 — re-exported for tests
    log,
    pull_artifact,
    reattach,
    session,
    sessions_cmd,
    wait,
    watch,
)
from coord.commands.terminal import terminal_group
from coord.commands.tui import tui_group

# #1628: the health check engine ships its own CLI surface inside
# coord/health/ so the whole feature — registry, probes, renderer, command —
# stays in one package. Registered here like any other command.
from coord.health.cli import health
# #1632: same arrangement for the fleet notifier — predicate, baselines,
# transport seam and CLI all live in coord/notifier/.
from coord.notifier.cli import notifier_group
# #2179: coord-portal sync bridge client — coord/portal_bridge.py is the
# client, coord/commands/portal.py is the CLI over it.
# #3071: `journal` is top-level, not a `portal` subcommand — it answers a
# client's "what is happening with my project", not an operator's question
# about the bridge. Implemented alongside the bridge commands because it reads
# the same tables.
from coord.commands.portal import journal, portal_group
from coord.commands.merge import (
    backfill_review_cost,
    bounce,
    merge,
    post_pending_reviews,
    reconcile_merges,
    verify_merge,
)
from coord.commands.review import (
    _prompt_and_relay_review_verdict,  # noqa: F401 — re-exported for tests
    _prompt_and_relay_test_verdict,  # noqa: F401 — re-exported for tests
    fix_briefing_cmd,
    report_result,
    review_reaffirm,
    set_review_findings,
)
from coord.commands.test_gate import (
    _get_assignment_branch_head,  # noqa: F401 — re-exported for tests
    set_test_mode,
    test,
    test_plan_cmd,
    uat,
)
from coord.commands.chat import (
    new_issue_chat,
    ready,
    refine,
    refine_board,
    refine_chat,
    test_chat,
)
from coord.commands.issues import (
    backlog,
    context_group,
    issue_group,
    queue,
    sync,
    track,
    unqueue,
    untrack,
)
from coord.commands.drive import (
    decide,
    drive,
    drive_attach,
    drive_sessions,
    drive_stop,
    escalate_group,
)
from coord.commands.drive_queue import drive_queue_group
from coord.commands.lifecycle import done, housekeeping, notify, resume, serve, web
from coord.commands.codegen import codegen
from coord.commands.machine import machine_group
from coord.commands.milestone import milestone_group
from coord.commands.plans import plans_cmd
from coord.commands.release import release_group, release_preflight
# #2220: `coord repo add` / `coord repo doctor` — onboarding a repo, and
# verifying it actually happened across all five layers.
from coord.commands.repo import repo_group
from coord.commands.store_migrate import migrate_to_postgres
from coord.commands.plan_followup import (
    _dispatch_followup,  # noqa: F401 — re-exported for tests
    approve_plan,
    fix,
    reject_plan,
    resume_stuck,
    review,
    split,
)
# #2790: `coord pr` is now a group (`open`/`merge` — a branch with no board
# assignment) that still falls through to the legacy bare `coord pr
# <ASSIGNMENT_ID>` command above (dispatch a PR-opening worker for a
# completed assignment) — see coord/commands/pr.py's `_PrGroup`.
from coord.commands.pr import pr_group


# #1182: thresholds past which a stale non-editable install escalates from the
# mild "edits won't reach the CLI" note to a loud STALE INSTALL banner.
_STALE_COMMITS_THRESHOLD = 3
_STALE_DAYS_THRESHOLD = 2.0


def _compute_install_staleness(repo_root: Path, installed_version: str) -> dict | None:
    """Best-effort: how far *installed_version* trails ``repo_root``'s HEAD.

    Returns ``{"commits_behind": int, "days_behind": float | None, "tag": str}``
    when a ``v{installed_version}`` tag exists in the checkout and HEAD has
    moved past it, or ``None`` when there's no matching tag, the checkout
    isn't a git repo, HEAD hasn't moved, or anything else goes wrong (never
    raises — this is a nice-to-have signal, not something that should ever
    break the CLI, per #1182).
    """
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    if not installed_version:
        return None
    try:
        if not (repo_root / ".git").exists():
            return None
        tag = f"v{installed_version}"

        def _git(*args: str) -> str | None:
            result = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
            return result.stdout.strip()

        if _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}") is None:
            return None  # no matching tag — can't quantify drift
        count_raw = _git("rev-list", "--count", f"{tag}..HEAD")
        if count_raw is None or not count_raw.isdigit():
            return None
        commits_behind = int(count_raw)
        if commits_behind <= 0:
            return None  # installed version is at least as new as HEAD

        days_behind = None
        ts_raw = _git("log", "-1", "--format=%ct", tag)
        if ts_raw and ts_raw.isdigit():
            days_behind = (time.time() - int(ts_raw)) / 86400.0
        return {"commits_behind": commits_behind, "days_behind": days_behind, "tag": tag}
    except Exception:  # noqa: BLE001 — best-effort, never break the CLI
        return None


def _warn_if_source_install_drift() -> None:
    """Warn when the CLI is running from a non-editable install of a package
    whose source checkout is the current working directory.

    Root cause of #222: ``pip install .`` (without ``-e``) copies a snapshot
    into site-packages. Subsequent edits in the source tree don't reach the
    CLI, while ``python -c "from coord.... import ..."`` from the source dir
    DOES pick them up (cwd shadows site-packages on import). Result: the same
    workflow gives different answers depending on entry path.

    Heuristic: ``coord.__file__`` lives in ``site-packages`` AND the cwd has
    a sibling ``coord/`` package — that's exactly the drift case.

    #1182: that plain warning shipped in #222 turned out to read as
    boilerplate noise — it fired identically whether the install was a day
    stale or, as happened on elitebook, many releases behind `main` (silently
    evaluating retired logic and causing a false merge-gate block). When the
    checkout has a tag matching the installed version, we now also quantify
    how many commits/days HEAD has moved past it and escalate to a much
    louder banner past ``_STALE_COMMITS_THRESHOLD`` / ``_STALE_DAYS_THRESHOLD``.
    """
    import os  # noqa: PLC0415

    try:
        import coord as _coord  # noqa: PLC0415

        coord_file = _coord.__file__ or ""
        if "site-packages" not in coord_file:
            return  # Editable install — source IS the import path, no drift.
        local_init = Path(os.getcwd()) / "coord" / "__init__.py"
        if not local_init.exists():
            return  # Not running from a source checkout.

        staleness = _compute_install_staleness(Path(os.getcwd()), _coord.__version__ or "")
        if staleness is not None and (
            staleness["commits_behind"] >= _STALE_COMMITS_THRESHOLD
            or (
                staleness["days_behind"] is not None
                and staleness["days_behind"] >= _STALE_DAYS_THRESHOLD
            )
        ):
            days_note = (
                f", ~{staleness['days_behind']:.1f} day(s)"
                if staleness["days_behind"] is not None
                else ""
            )
            # #2103/#2106: suggest an upgrade of the installed distribution
            # name (`code-coordinator`), resolved via `coord.dist_name`
            # rather than hardcoded. `_coord.__file__` is already confirmed
            # above to be a real site-packages install, so resolution is
            # guaranteed to succeed here; the literal fallback only guards a
            # resolution race between that check and this one — the only
            # way to reach it is "the distribution vanished between the two
            # checks". `_dist_pkg_spec` is the module-level `coord.dist_name`
            # import above (real module, independent of `_coord` here
            # potentially being a test stand-in), so this doesn't depend on
            # `coord.dist_name` already being cached.
            try:
                install_target = _dist_pkg_spec(extra="server")
            except Exception:  # noqa: BLE001 — best-effort, never break the CLI
                install_target = "code-coordinator[server]"
            banner = "⚠" * 24
            click.echo(
                f"{banner}\n"
                f"⚠⚠⚠  STALE INSTALL: coord CLI is {staleness['commits_behind']} "
                f"commit(s){days_note} behind the checkout's HEAD  ⚠⚠⚠\n"
                f"Installed: {_coord.__version__} (tag {staleness['tag']}) — the source "
                f"checkout at {local_init.parent} has moved past that release.\n"
                "This install may be silently evaluating RETIRED logic (#1182 — this is "
                "how a false merge-gate block slipped through).  Fix:\n"
                f"  pip install --upgrade '{install_target}'   "
                "(if a release covers those commits)\n"
                "  pip install -e .                           (to run live source instead)\n"
                f"{banner}",
                err=True,
            )
            return

        # Inside a source checkout but CLI uses snapshot copy → drift possible.
        click.echo(
            "warning: coord CLI is running from a non-editable install "
            "(site-packages snapshot) but a source checkout exists at "
            f"{local_init.parent}.\n"
            "         Edits to the source tree will NOT reach the CLI.  "
            "Fix:  pip install -e .",
            err=True,
        )
    except Exception:  # noqa: BLE001 — best-effort, never break the CLI
        pass


def _editable_checkout_drift() -> tuple[Path, str] | None:
    """Pure query behind both #561/#601's CLI warning and #2314's
    drive-queue tick refusal (:mod:`coord.drive_queue`): is THIS process an
    EDITABLE checkout of coord, and if so, is it on a branch other than the
    default one?

    A Build/`coord test`/smoke that git-checkout'd the base — or an
    interactive agent inspecting a branch in the live checkout — silently
    puts the running coordinator on that branch's code until restored (#561
    incident: disabled guards; #601 incident: old code + retired local DB).

    Returns ``(repo_root, shown)`` when drifted — ``shown`` is the branch
    name formatted the way both callers print it (quoted, or
    ``"(detached HEAD)"``) — or ``None`` when this is a non-editable/
    site-packages install, isn't a git checkout at all, is cleanly on
    ``main``/``master``, or the check itself couldn't be completed (git not
    on PATH, no ``.git``, a timeout). Every failure mode here is read-only
    and best-effort, so it degrades to "no drift detected" rather than
    raising — a flaky read must never itself block a tick or crash a
    command.
    """
    import subprocess  # noqa: PLC0415

    try:
        import coord as _coord  # noqa: PLC0415

        coord_file = _coord.__file__ or ""
        if "site-packages" in coord_file:
            return None  # PyPI/snapshot install — moving a checkout can't affect it.
        repo_root = Path(coord_file).resolve().parents[1]
        if not (repo_root / ".git").exists():
            return None
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=3,
        )
        if head.returncode != 0:
            return None
        branch = head.stdout.strip()
        if branch in ("main", "master"):
            return None
        shown = "(detached HEAD)" if branch == "HEAD" else f"'{branch}'"
        return (repo_root, shown)
    except Exception:  # noqa: BLE001 — best-effort, never break the caller
        return None


def _warn_if_editable_checkout_moved() -> None:
    """#561/#601 backstop: when running from an EDITABLE checkout, warn loudly if
    its branch was moved off the default.

    This makes that state visible on every command instead of waiting for a
    verdict or manual restore. #2314 escalated the SAME underlying reading
    (:func:`_editable_checkout_drift`) from advisory-only here to a hard
    refusal inside the drive-queue tick gate — see
    ``coord.drive_queue.plan_tick``'s ``editable_drift`` parameter — because
    a warning nobody is watching an unattended tick's log for is not a gate.
    This function stays as the interactive/human-facing half of that fix.
    """
    import sys as _sys  # noqa: PLC0415

    if "pytest" in _sys.modules:
        return  # don't add startup noise to the test suite
    drift = _editable_checkout_drift()
    if drift is None:
        return
    repo_root, shown = drift
    click.echo(
        f"⚠ coord: editable checkout {repo_root} is on {shown}, not the "
        "default branch — the running coordinator is on THAT code. A "
        "Build/smoke/test may have checked it out. Restore with:  "
        f"git -C {repo_root} checkout main",
        err=True,
    )


@click.group(help="Multi-agent coordinator for Claude Code workers.")
@click.version_option(__version__, prog_name="coord")
def main() -> None:
    """coord — coordinate Claude Code workers across machines and repos."""
    _warn_if_source_install_drift()
    _warn_if_editable_checkout_moved()


# Registration order below matches the historical decoration order in the
# pre-#747 cli.py exactly.
main.add_command(version)
main.add_command(config_cmd)
main.add_command(store_backend_cmd)
main.add_command(init)
main.add_command(agent)
main.add_command(status)
main.add_command(plan)
main.add_command(approve)
main.add_command(assign)
main.add_command(log)
main.add_command(show_plan)
main.add_command(inject)
main.add_command(chat_continue)
main.add_command(stop)
main.add_command(report_result)
main.add_command(verify_merge)
main.add_command(set_review_findings)
main.add_command(review_reaffirm)
main.add_command(retry)
main.add_command(pull_artifact)
main.add_command(bounce)
main.add_command(sync)
main.add_command(pause)
main.add_command(unpause)
# #2146: set a machine's quiet-hours window without a coordinator session.
main.add_command(quiet_hours)
main.add_command(refine_chat)
main.add_command(test_chat)
main.add_command(new_issue_chat)
main.add_command(refine_board)
main.add_command(ready)
main.add_command(refine)
main.add_command(reconcile_merges)
main.add_command(housekeeping)
main.add_command(diagnose)
main.add_command(gates)
main.add_command(doctor)
main.add_command(health)
main.add_command(notifier_group)
main.add_command(portal_group)
main.add_command(journal)
main.add_command(issue_group)
main.add_command(context_group)
main.add_command(audit)
main.add_command(report_group)
main.add_command(scorecard)
main.add_command(milestone_group)
main.add_command(plans_cmd)
main.add_command(fix_briefing_cmd)
main.add_command(track)
main.add_command(untrack)
main.add_command(backlog)
main.add_command(queue)
main.add_command(unqueue)
main.add_command(set_test_mode)
main.add_command(notify)
main.add_command(post_pending_reviews)
main.add_command(backfill_review_cost)
main.add_command(merge)
main.add_command(resume)
main.add_command(test)
main.add_command(test_plan_cmd)
main.add_command(uat)
main.add_command(split)
main.add_command(done)
main.add_command(session)
main.add_command(sessions_cmd)
main.add_command(reattach)
main.add_command(terminal_group)
main.add_command(usage)
main.add_command(web)
main.add_command(serve)
main.add_command(wait)
main.add_command(watch)
main.add_command(drive)
main.add_command(drive_sessions)
main.add_command(drive_attach)
main.add_command(drive_stop)
main.add_command(drive_queue_group)
main.add_command(escalate_group)
main.add_command(decide)
main.add_command(pr_group)
main.add_command(fix)
main.add_command(review)
main.add_command(approve_plan)
main.add_command(reject_plan)
main.add_command(resume_stuck)
main.add_command(install_skills)
main.add_command(acceptance_group)
# #2063: the Gate-A human sign-off verdict, sibling to `coord test
# --passed|--fail`. Flat (not under `acceptance`) because it is an operator
# gesture on a milestone, not part of the acceptance runner.
main.add_command(gate_a)
main.add_command(release_preflight)
# #1834: `coord release verify` (and `coord release preflight` as an alias of
# the flat command above), grouped so the pre-tag and post-release halves of
# the release story are discoverable together.
main.add_command(release_group)
# #2220: repo onboarding + its verifier.
main.add_command(repo_group)
# #2915: the machine-side analogue — machine onboarding + its verifier.
main.add_command(machine_group)
main.add_command(migrate_to_postgres)
main.add_command(tui_group)
# #3045: reach coord.codegen (formerly scripts/codegen.py, never shipped)
# without a checkout of this repo.
main.add_command(codegen)


# #1809: without this guard, `python -m coord.cli <args>` just IMPORTS the
# module — building every `click.group`/`add_command` above, then returning —
# and exits 0 having run nothing and printed nothing. That import-only path is
# exactly what `coord.drive.coord_argv()` falls back to whenever `coord` isn't
# on PATH (a bare venv, a systemd user unit, a non-interactive ssh session —
# see #402), so every subprocess the driver or the drive queue spawned on such
# a host was a silent no-op that reported success. `python -m coord.cli
# --version` and the fallback argv both need `main()` to actually run.
if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    main()
