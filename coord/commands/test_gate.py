"""Test-gate commands: `test` (pull/record verdict), `test-plan`,
`set-test-mode`. Extracted from coord/cli.py (#747).

#615: the original ``@main.command("test", ...)`` "queue a smoke test"
command (`test_cmd`) was dead code even in the pre-#747 cli.py — the
`test` function below registers the same Click command name "test" and
always won (last registration in main.commands wins), so `test_cmd` could
never run. It also called `build_board()`/`load_board()`/`save_board()`
unconditionally, unlike every reachable board-mutating command, which are
now daemon-routed (see the #615 audit note via `coord context show
claude-coordinator 615`). Removed rather than migrated, since routing
dead code would be pure ceremony.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from coord.config import Config

from coord.commands._common import (
    _apply_label_change,
    _CONFIG_OPTION,
    _load_config,
)


def _get_assignment_branch_head(
    assignment_id: str,
    config: "Config",
    repo_path_fn: "Callable[[str, Config], Path | None]",
) -> str | None:
    """#349: Resolve the current HEAD SHA for an assignment's branch.

    Looks up the assignment's repo_name + branch from the DB, finds the local
    repo path via *repo_path_fn* (typically
    ``coord.test_orchestrator.find_local_repo_path``), then runs
    ``git rev-parse <branch>`` to get the SHA.

    Returns ``None`` when the assignment is not found, has no branch set, the
    local repo path can't be resolved, or git fails.  The caller treats ``None``
    as "HEAD unknown — skip staleness tracking".
    """
    import subprocess  # noqa: PLC0415 — lazy import keeps startup fast
    from coord import sql  # noqa: PLC0415
    from coord.db import get_connection  # noqa: PLC0415

    conn = get_connection()
    row = sql.execute(
        conn,
        "SELECT repo_name, branch FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if not row:
        return None
    repo_name: str = row["repo_name"] if hasattr(row, "keys") else row[0]
    branch: str = (row["branch"] if hasattr(row, "keys") else row[1]) or ""
    if not branch:
        return None
    local_path = repo_path_fn(repo_name, config)
    if not local_path or not local_path.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", branch],
            cwd=str(local_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _maybe_reconcile_branch(
    assignment, repo_dir, *, original_error: str, config,
):
    """When `git checkout <db_branch>` fails, try to learn the PR's actual
    head ref from GitHub and reconcile the DB.

    Returns the new branch name when reconciliation succeeded (DB
    updated + the reconciled branch verified on origin), or `None` when no
    PR is associated, the gh call failed, the head ref matches what we
    already had, or the reconciled ref is missing on origin.  The caller
    falls back to the original error in those cases.
    """
    from coord import sql
    from coord.db import get_connection

    # Need a PR number to look up the head ref.  Pull it from the
    # merge_queue entry for this assignment.
    aid = assignment.assignment_id
    if not aid:
        return None
    conn = get_connection()
    row = sql.execute(
        conn,
        "SELECT pr_number, repo_github FROM merge_queue "
        "WHERE assignment_id=?",
        (aid,),
    ).fetchone()
    if row is None:
        return None
    pr_number = row["pr_number"]
    repo_github = row["repo_github"]
    if pr_number is None or not repo_github:
        return None

    # Fetch the PR's actual head ref from GitHub.  Returns the real
    # branch name even when the DB has a stale slug.  Routed through the
    # github_ops seam (#1483) rather than a direct `gh` invocation here.
    from coord import github_ops  # noqa: PLC0415

    real_branch = github_ops.get_pr_head_ref(repo_github, pr_number) or ""
    if not real_branch:
        return None
    if real_branch == assignment.branch:
        # The PR DOES point at the DB-recorded branch; checkout failed
        # for some other reason (local-only clone, network, etc.).
        # Don't pretend we fixed it.
        return None

    # Validate the reconciled branch exists on origin before writing it to
    # the DB.  #561: this MUST be non-mutating — never `git checkout` in the
    # base checkout (it doubles as the live editable coordinator source).
    # `git fetch origin` already ran in the caller, so origin/<branch> is
    # current; a rev-parse verify confirms it without moving HEAD.
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{real_branch}"],
            cwd=str(repo_dir), check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        return None

    # Persist the reconciled branch on both tables so future runs of
    # coord test / coord merge / TUI etc. all see the right value.
    sql.execute(
        conn,
        "UPDATE assignments SET branch=? WHERE assignment_id=?",
        (real_branch, aid),
    )
    sql.execute(
        conn,
        "UPDATE merge_queue SET branch=? WHERE assignment_id=?",
        (real_branch, aid),
    )
    conn.commit()

    # Mute the unused 'original_error' / 'config' params — they're
    # there for future use (e.g. logging context, post-back to GitHub).
    _ = original_error
    _ = config
    return real_branch


@click.command(
    "set-test-mode",
    help=(
        "Set the per-issue test-mode policy for headless Work sessions.\n\n"
        "MODE must be 'smoke' (pause at the Test gate for a human-attended\n"
        "interactive smoke agent, the default) or 'auto' (run the smoke test\n"
        "headless and continue toward merge without stopping).\n\n"
        "The policy is persisted as a 'test-mode:smoke' / 'test-mode:auto'\n"
        "GitHub label.  The TUI reads this label when a headless Work session\n"
        "completes to decide whether to offer the interactive smoke agent or\n"
        "auto-dispatch smoke.py.  The label can be flipped at any time before\n"
        "the issue reaches the Test gate.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the GH\n"
        "issue number."
    ),
)


@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("mode", type=click.Choice(["smoke", "auto"]))
@_CONFIG_OPTION
def set_test_mode(repo: str, issue: int, mode: str, config_path: Path) -> None:
    """#685: TUI test-mode dialog and right-click flip fire this command."""
    from coord import github_ops  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
        sys.exit(1)
    slug = repo_entry.github

    # Ensure the test-mode:* labels exist in the repo before we try to add
    # them — `gh issue edit --add-label` fails with "label not found" when the
    # label has never been created.  `gh label create --force` is idempotent
    # (no-ops if the label already exists, creates it if absent).  Routed
    # through the github_ops seam (#1483) rather than a direct `gh` call here.
    _TEST_MODE_LABEL_COLOR = "0075ca"  # default blue; matches GitHub's "documentation" label
    for lbl in ("test-mode:smoke", "test-mode:auto"):
        try:
            github_ops.create_label(
                slug, lbl,
                color=_TEST_MODE_LABEL_COLOR,
                description="coord: per-issue test-mode policy",
            )
        except RuntimeError:
            pass  # best-effort — label creation failure is surfaced when add-label fails

    _apply_label_change(
        repo, issue, config_path,
        add={f"test-mode:{mode}"},
        remove_if_present={
            lbl for lbl in ("test-mode:smoke", "test-mode:auto")
            if lbl != f"test-mode:{mode}"
        },
        success_message=f"#{issue} ({slug}) test mode set to '{mode}'",
        no_op_message=f"#{issue} ({slug}) already has test-mode:{mode}",
    )


def _test_worktree_path(assignment_id: str, repo_name: str) -> Path:
    """#561: throwaway worktree path for `coord test`'s build (per assignment).

    Lives under ``~/.coord/test-worktrees/`` — OUTSIDE the base checkout — so a
    Build never moves the base checkout's branch (which doubles as the live
    editable coordinator source).
    """
    from coord.state import COORD_DIR  # noqa: PLC0415

    return COORD_DIR / "test-worktrees" / f"{repo_name}-{assignment_id}"


def _remove_test_worktree(repo_dir: Path, wt_path: Path) -> None:
    """Best-effort removal of a `coord test` worktree (+ prune admin refs)."""
    import subprocess  # noqa: PLC0415

    if not wt_path.exists():
        return
    for args in (
        ["git", "worktree", "remove", "--force", str(wt_path)],
        ["git", "worktree", "prune"],
    ):
        try:
            subprocess.run(
                args, cwd=str(repo_dir), capture_output=True, text=True, timeout=30
            )
        except (subprocess.SubprocessError, OSError):
            pass


def _stash_test_artifacts(repo, assignment, wt_path: Path) -> None:
    """#1249: stash the built artifact right after `coord test`'s build
    succeeds — the ONE stage that reliably runs `build_command` and produces
    the exact binary the Test-stage `kind: "pull"` step (and the next
    `--fix-of` re-test on this branch) wants to pull instead of rebuilding.

    The Work-completion stash (`AgentServer._stash_artifacts`) fires right
    before that worktree is removed, but a Work session isn't required to
    run `build_command` — only `coord test` (and the interactive
    `--smoke-of` path, already covered by `finalize_interactive_exit`) is.
    Delegates to the same `stash_artifacts_for_branch` helper both of those
    call, so one filter/copy/GC implementation serves all three call sites.

    Best-effort and silent on the "nothing to do" cases (no configured
    `artifact_paths`, no branch, no worktree on disk) — mirrors the guard
    clauses in `AgentServer._stash_artifacts`.
    """
    if repo is None or not repo.artifact_paths:
        return
    if not assignment.branch:
        return
    if not wt_path.exists():
        return

    from coord.agent import stash_artifacts_for_branch  # noqa: PLC0415
    from coord.state import COORD_DIR  # noqa: PLC0415

    patterns = list(repo.artifact_paths)
    copied = stash_artifacts_for_branch(
        worktree_path=wt_path,
        branch=assignment.branch,
        repo_name=assignment.repo_name,
        patterns=patterns,
        state_dir=COORD_DIR,
        assignment_id=assignment.assignment_id,
        log_path=None,
    )
    if copied > 0:
        click.echo(f"  stashed {copied} artifact(s) for {assignment.branch!r}.")
    else:
        # #1249 review finding #2: the other two call sites
        # (AgentServer._stash_artifacts, finalize_interactive_exit) pass a
        # real log_path, so stash_artifacts_for_branch's #1248 "loud 0-copy"
        # warning lands in the assignment log. This call site has no log
        # file — `coord test` runs directly in the operator's terminal —
        # so log_path=None above means that warning is silently swallowed.
        # Echo it here instead, mirroring the same message.
        click.echo(
            f"  warning: 0 artifact(s) matched {patterns!r} in {wt_path} — "
            "check artifact_paths config and that the build actually "
            "produced the expected outputs.",
            err=True,
        )


def _cleanup_test_worktree(cfg, assignment) -> None:
    """Remove the test worktree for *assignment* (called on a pass/skip verdict).

    Resolves the base checkout the same way the build path does; a no-op when no
    worktree exists (e.g. a verdict recorded without a prior Build).
    """
    if not assignment.assignment_id:
        return
    repo_dir = _local_repo_dir(cfg, assignment.repo_name)
    if repo_dir is None:
        return
    _remove_test_worktree(
        repo_dir, _test_worktree_path(assignment.assignment_id, assignment.repo_name)
    )


def _local_repo_dir(cfg, repo_name: str) -> Path | None:
    """Resolve the base checkout for *repo_name* (local machine first, then any
    machine that knows it).  Returns an expanded ``Path`` or ``None``."""
    import socket  # noqa: PLC0415

    hostname = socket.gethostname().split(".")[0]
    local_machine = next(
        (m for m in cfg.machines if m.name == hostname or m.host.split(".")[0] == hostname),
        None,
    )
    repo_path = None
    if local_machine:
        repo_path = local_machine.repo_path(repo_name)
    if repo_path is None:
        for m in cfg.machines:
            repo_path = m.repo_path(repo_name)
            if repo_path:
                break
    return Path(repo_path).expanduser() if repo_path else None


def _restore_default_branch_after_test(cfg, assignment) -> None:
    """#271 part 1: switch the local checkout back to the repo's
    `default_branch` after a pass/skip verdict.

    Resolves the repo path the same way `coord test`'s checkout step
    does (local machine's `repo_paths` first, then any machine that
    knows the repo).  Best-effort: a failed `git checkout` is surfaced
    as a warning but doesn't fail the verdict recording.
    """
    import socket  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    if not assignment.branch:
        # No branch was ever checked out — nothing to restore.
        return

    repo = cfg.repo(assignment.repo_name)
    if repo is None or not repo.default_branch:
        return

    hostname = socket.gethostname().split(".")[0]
    local_machine = next(
        (m for m in cfg.machines if m.name == hostname or m.host.split(".")[0] == hostname),
        None,
    )
    repo_path = None
    if local_machine:
        repo_path = local_machine.repo_path(assignment.repo_name)
    if repo_path is None:
        for m in cfg.machines:
            repo_path = m.repo_path(assignment.repo_name)
            if repo_path:
                break
    if repo_path is None:
        return

    repo_dir = _Path(repo_path).expanduser()
    if not repo_dir.exists():
        return

    # Quick early-out: if the user is already on the default branch
    # (e.g. they switched manually after running `coord test`), there's
    # nothing to do and no need to announce a no-op.
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if head.returncode == 0 and head.stdout.strip() == repo.default_branch:
            return
    except (subprocess.TimeoutExpired, OSError):
        # If we can't even check the current branch, don't try to switch.
        return

    try:
        result = subprocess.run(
            ["git", "checkout", repo.default_branch],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        click.echo(f"  warning: could not restore default branch: {e}", err=True)
        return
    if result.returncode != 0:
        # Most common cause: dirty working tree from manual edits during
        # testing.  Surface it so the user can stash + retry manually.
        click.echo(
            f"  warning: could not switch back to {repo.default_branch!r}: "
            f"{result.stderr.strip()}",
            err=True,
        )
        return
    click.echo(f"  restored: {repo.default_branch} in {repo_dir}")


@click.command(help="Pull a worker's branch locally for testing, or record a Test gate verdict.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--passed", "verdict", flag_value="pass", help="Mark Test gate as passed.")
@click.option("--fail", "verdict", flag_value="fail", help="Mark Test gate as failed.")
@click.option("--skipped", "verdict", flag_value="skip", help="Mark Test gate as skipped (trivial change).")
@click.option(
    "--running", "verdict", flag_value="running",
    help=(
        "Mark the Test gate as in-progress (#1395). A transient, non-verdict "
        "marker for a driver that runs the suite locally instead of through "
        "dispatch_smoke — set right before the run starts so the TUI/board "
        "reads the Test box Active instead of Pending for the run's "
        "duration. Overwrite it with --passed/--fail/--skipped when the run "
        "concludes; every gate treats it as 'no verdict yet'."
    ),
)
@click.option("--reason", default="", help="Reason for failure or skip (used with --fail/--skipped).")
@click.option("--output", "output_file", type=click.Path(), default=None,
              help="File with test output to store (used with --fail).")


def test(assignment_id: str, config_path: Path, verdict: str | None, reason: str, output_file: str | None) -> None:
    from coord.board_service import read_board
    from coord.state import record_test_verdict

    cfg = _load_config(config_path)
    # #590 Phase 2 / #1337: a thin client reads the board from the daemon (its
    # local DB is empty) so the assignment resolves; the verdict is recorded
    # via the single-row record_test_verdict on BOTH paths (it self-routes:
    # daemon when board_service is set, direct local UPDATE otherwise).
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)

    # ── Record verdict ──────────────────────────────────────────────────
    if verdict:
        # Map CLI verdict flags to the canonical test_state values used by the
        # TUI's Test stage and the reconcile review-gating logic. "running"
        # (#1395) is NOT a verdict — it's a transient in-progress marker a
        # driver sets around a locally-run suite, overwritten by one of the
        # other three flags when the run concludes.
        test_state_map = {"pass": "passed", "fail": "failed", "skip": "skipped", "running": "running"}
        assignment.test_state = test_state_map[verdict]
        # #1213: a --skipped verdict carries a reason too (e.g. "trivial dep
        # bump, covered by regression test in the same PR") — that reason IS
        # the audit trail for why the human test was bypassed, so it must
        # not be discarded the way --passed's (never has one) is. Only a
        # bare --passed clears any stale reason from a prior verdict.
        assignment.test_reason = reason if verdict in ("fail", "skip") else None
        # Mirror to legacy smoke_test for the existing smoke-stage scoring in
        # pipeline.py (which predates the human Test gate).
        if verdict in ("pass", "fail"):
            assignment.smoke_test = verdict
            assignment.smoke_test_reason = reason if verdict == "fail" else None

        # Store test output when --fail --output is provided
        if verdict == "fail" and output_file:
            output_path = Path(output_file)
            if output_path.exists():
                from coord.state import COORD_DIR

                test_output_dir = COORD_DIR / "test_output"
                test_output_dir.mkdir(parents=True, exist_ok=True)
                stored = test_output_dir / f"{assignment_id}.txt"
                stored.write_text(output_path.read_text())
                # Record the stored path so coord fix can find it
                reason_with_output = (
                    f"{reason} [output: {stored}]" if reason else f"[output: {stored}]"
                )
                assignment.test_reason = reason_with_output
                assignment.smoke_test_reason = reason_with_output
                click.echo(f"  test output stored: {stored}")
            else:
                click.echo(f"  warning: output file not found: {output_file}", err=True)

        # #1629 (H-2): best-effort — which toolchain, on THIS machine, ran the
        # suite that produced a pass/fail verdict. "skip"/"running" carry no
        # test run, so there's nothing to attribute. Never blocks the verdict
        # write: any failure here (repo path unresolved, no known toolchain,
        # the binary not on PATH) just leaves it None, same as every
        # pre-#1629 verdict.
        test_toolchain = None
        if verdict in ("pass", "fail"):
            try:
                from coord.health.checks.toolchain import local_toolchain_label

                repo_dir = _local_repo_dir(cfg, assignment.repo_name)
                if repo_dir is not None:
                    test_toolchain = local_toolchain_label(repo_dir)
            except Exception:  # noqa: BLE001 — annotation only, never fatal
                test_toolchain = None

        # #1337: single-row verdict write on BOTH paths (record_test_verdict
        # self-routes: daemon when board_service is set, direct UPDATE
        # locally).  The old local-path save_board() relied on the whole-board
        # upsert carrying test_reason/smoke_test_reason — those free-text
        # columns are now excluded from that upsert so a bounded /board
        # preview can never round-trip over the full stored text.
        record_test_verdict(
            assignment_id=assignment_id,
            test_state=assignment.test_state,
            test_reason=assignment.test_reason,
            smoke_test=assignment.smoke_test,
            smoke_test_reason=assignment.smoke_test_reason,
            test_toolchain=test_toolchain,
        )
        verdict_word = {
            "pass": "PASSED", "fail": "FAILED", "skip": "SKIPPED", "running": "RUNNING",
        }[verdict]
        click.echo(f"Test gate {verdict_word} for {assignment.repo_name} #{assignment.issue_number}")
        if verdict in ("fail", "skip") and reason:
            click.echo(f"  reason: {reason}")
        elif verdict == "pass":
            click.echo("  Run: coord merge to proceed")
        elif verdict == "running":
            click.echo("  (transient — record --passed/--fail/--skipped when the run concludes)")

        # #271 part 1: restore the local checkout to `default_branch` after a
        # pass/skip verdict (legacy safety — #561 means a Build no longer moves
        # the base, so this is a no-op on fresh checkouts), and #561: remove the
        # throwaway test worktree now that testing concluded.  `--fail` leaves
        # the worktree so the user can dig into the failure. `running` is
        # neither — the driver manages its own scratch worktree separately, so
        # this command never touched one in the first place.
        if verdict in ("pass", "skip"):
            _restore_default_branch_after_test(cfg, assignment)
            _cleanup_test_worktree(cfg, assignment)
        return

    # ── Checkout and build (in a throwaway worktree — #561) ──────────────
    if not assignment.branch:
        click.echo(
            f"error: assignment {assignment_id} has no branch recorded. "
            f"The worker may not have pushed yet, or the branch wasn't captured during reconciliation.",
            err=True,
        )
        sys.exit(1)

    import subprocess

    repo_dir = _local_repo_dir(cfg, assignment.repo_name)
    if repo_dir is None:
        click.echo(
            f"error: no repo_path configured for {assignment.repo_name!r}. "
            f"Add it to coordinator.yml under machines[].repo_paths.",
            err=True,
        )
        sys.exit(1)
    if not repo_dir.exists():
        click.echo(f"error: repo path does not exist: {repo_dir}", err=True)
        sys.exit(1)

    # #561: build/test in a throwaway worktree fetched fresh from origin —
    # NEVER `git checkout` in the base checkout. The base doubles as the live
    # editable coordinator source, so moving its branch silently downgrades the
    # running coord (disabled guards, reintroduced bugs) until restored. A
    # `git fetch` is safe (it doesn't move HEAD); the worktree gets its own.
    wt_path = _test_worktree_path(assignment_id, assignment.repo_name)
    click.echo(
        f"Fetching origin and preparing test worktree for {assignment.branch!r} "
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

    # Clear any stale worktree from a prior Build of this assignment.
    _remove_test_worktree(repo_dir, wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    def _add_worktree(branch: str):
        # --detach: we only read the tree to build/test; no local branch needed.
        return subprocess.run(
            ["git", "worktree", "add", "--force", "--detach",
             str(wt_path), f"origin/{branch}"],
            cwd=str(repo_dir), capture_output=True, text=True,
        )

    res = _add_worktree(assignment.branch)
    if res.returncode != 0:
        # Branch drift (auto-loop orphan branches; slugifier max_len changes
        # across releases; manual `git branch -m` on origin). When the worktree
        # add fails AND the issue has a PR, resolve the PR's actual headRefName
        # (non-mutating), update the DB, and retry.
        reconciled = _maybe_reconcile_branch(
            assignment, repo_dir, original_error=res.stderr.strip(), config=cfg,
        )
        if reconciled is None:
            click.echo(
                f"error: could not create test worktree: {res.stderr.strip()}",
                err=True,
            )
            sys.exit(1)
        assignment.branch = reconciled
        click.echo(
            f"  branch drift reconciled: using the PR's actual head ref "
            f"{assignment.branch!r}"
        )
        res = _add_worktree(assignment.branch)
        if res.returncode != 0:
            click.echo(
                f"error: could not create test worktree: {res.stderr.strip()}",
                err=True,
            )
            sys.exit(1)

    click.echo(f"Test worktree ready at {wt_path} (branch {assignment.branch!r}).")

    if repo and repo.build_command:
        click.echo(f"Running build: {repo.build_command}")
        result = subprocess.run(repo.build_command, shell=True, cwd=str(wt_path))
        if result.returncode != 0:
            click.echo(f"Build failed (exit {result.returncode})", err=True)
            click.echo(f"  worktree kept for inspection: {wt_path}")
            sys.exit(1)
        click.echo("Build succeeded.")
        # #1249: stash immediately after a successful build — regardless of
        # whether the test step below then passes or fails — so a later
        # `--fix-of` re-test on this branch can pull instead of rebuilding.
        _stash_test_artifacts(repo, assignment, wt_path)

    if repo and repo.test_command:
        click.echo(f"Running tests: {repo.test_command}")
        # #2170: capture (stdout+stderr merged, same as an unpiped terminal
        # would show) so a failure can be checked for the runner's `RESULT:
        # BASELINE-RED` marker below — a failure the runner itself has
        # already confirmed also reproduces on the merge-base is a
        # statement about THIS MACHINE, not this branch, and must not be
        # nudged towards `coord test --fail`. Relayed to the terminal after
        # the run finishes rather than streamed live — the trade-off that
        # buys the marker check `subprocess.run` (not `Popen`) keeps this
        # call mockable exactly like every other one in this function.
        result = subprocess.run(
            repo.test_command, shell=True, cwd=str(wt_path),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.stdout:
            click.echo(result.stdout, nl=False)
        if result.returncode != 0:
            from coord.revalidate import is_baseline_red_failure  # noqa: PLC0415

            if is_baseline_red_failure(result.returncode, result.stdout or ""):
                click.echo(
                    f"\nTests failed (exit {result.returncode}) — but the "
                    "runner already confirmed every failure reproduces "
                    "identically on the merge-base (#2170): this machine's "
                    "BASELINE is red, not this branch.",
                    err=True,
                )
                click.echo(f"  worktree kept for inspection: {wt_path}")
                click.echo(
                    "  run: coord test --skipped "
                    f"{assignment_id} --reason \"baseline-red (#2170): "
                    "pre-existing failures on this machine\"   "
                    "# NOT --fail — the branch made nothing worse"
                )
                sys.exit(1)
            click.echo(f"Tests failed (exit {result.returncode})", err=True)
            click.echo(f"  worktree kept for inspection: {wt_path}")
            sys.exit(1)
        click.echo("Tests passed.")

    click.echo(
        f"\nReady for smoke test (worktree: {wt_path}). Run:\n"
        f"  coord test --passed {assignment_id}   # if it looks good (removes the worktree)\n"
        f"  coord test --fail {assignment_id} --reason \"description\"   # keeps the worktree to dig in"
    )


@click.command(
    help=(
        "Record a pre-merge UAT (User Acceptance Test) verdict (#2687).\n\n"
        "For repos that opt in via `uat_preview` or `uat_live_preview` in "
        "coordinator.yml, `coord merge` refuses to merge ASSIGNMENT_ID's PR "
        "until this records a "
        "--passed verdict — the gate exists so a human looks at the PR's "
        "deployed preview (the URL coord.merge_queue.evaluate_uat_verdict "
        "prints alongside the block) before a customer does. Modelled "
        "directly on `coord test --passed|--fail`."
    )
)
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--passed", "verdict", flag_value="passed", help="Mark the UAT gate as passed.")
@click.option("--failed", "verdict", flag_value="failed", help="Mark the UAT gate as failed.")
@click.option(
    "--note", default="",
    help=(
        "Note describing the verdict — e.g. why it failed. Carried into "
        "the failure the same way `coord test --fail --reason` is: recorded "
        "on the board and added to the issue's context digest so the next "
        "worker sees it without re-fetching the PR."
    ),
)
def uat(assignment_id: str, config_path: Path, verdict: str | None, note: str) -> None:
    from coord.board_service import read_board
    from coord.state import record_uat_verdict

    if verdict is None:
        click.echo("error: specify --passed or --failed", err=True)
        sys.exit(1)

    cfg = _load_config(config_path)
    # #590 Phase 2 / #1337: same daemon-or-local self-routing as `coord test`
    # — read_board() resolves the daemon's board on a thin client, and
    # record_uat_verdict routes its write the same way.
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)
    # #2948: "opted in" is uat_preview OR uat_live_preview — same two-part
    # question coord.merge_queue.requires_uat answers. Checking uat_preview
    # alone here made this warning lie for a repo that opted in via
    # uat_live_preview only (the shape this PR's own docs recommend for a
    # project with no templatable preview host): coord merge DOES enforce
    # the gate for that repo, so claiming otherwise is the exact silent-
    # trust-erosion bug #2948 was filed to close, reintroduced on the CLI
    # surface that records the operator's verdict.
    uat_opted_in = bool(
        repo is not None
        and (repo.uat_preview or getattr(repo, "uat_live_preview", False))
    )
    if not uat_opted_in:
        # Not a hard error: an operator may still want a manual verdict on
        # record for a repo that hasn't (yet) opted in — but the merge gate
        # (coord.merge_queue.requires_uat) won't enforce it either way, so
        # say so rather than implying this verdict blocks anything.
        click.echo(
            f"warning: {assignment.repo_name!r} has no uat_preview or "
            "uat_live_preview configured in coordinator.yml — recording "
            "the verdict, but coord merge will not enforce this gate for "
            "this repo.",
            err=True,
        )
    elif repo.uat_preview:
        # Template-only, best-effort — mirrors coord.stage_projection.
        # uat_preview_url_for: this command has no gh_ops wired up to run
        # the live GitHub-Deployment lookup uat_live_preview opts into, so
        # a uat_live_preview-only repo prints no preview line here rather
        # than a guessed/dead one (the #2948 bug this PR fixes). The full
        # live resolution still runs server-side in
        # coord.merge_queue.evaluate_uat_verdict at merge-gate time.
        pr_number = None
        try:
            from coord import sql  # noqa: PLC0415
            from coord.db import get_connection  # noqa: PLC0415

            row = sql.execute(
                get_connection(),
                "SELECT pr_number FROM merge_queue WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            pr_number = row["pr_number"] if row else None
        except Exception:  # noqa: BLE001 — best-effort; annotation only
            pr_number = None
        preview_url = repo.resolve_uat_preview_url(
            branch=assignment.branch,
            issue_number=assignment.issue_number,
            pr_number=pr_number,
        )
        if preview_url:
            click.echo(f"  preview: {preview_url}")

    record_uat_verdict(
        assignment_id=assignment_id,
        uat_state=verdict,
        uat_reason=(note.strip() or None) if verdict == "failed" else None,
    )
    click.echo(
        f"UAT gate {verdict.upper()} for {assignment.repo_name} #{assignment.issue_number}"
    )
    if verdict == "failed" and note.strip():
        click.echo(f"  note: {note.strip()}")
    elif verdict == "passed":
        click.echo("  Run: coord merge to proceed")


def _test_plan_via_daemon(svc, params: dict) -> None:
    """#851: run ``coord test-plan`` on the daemon host (the assignment + its
    cached ``test_plan`` live in the daemon's canonical DB, not a thin
    client's empty local one) and relay its output.  Mirrors
    ``_diagnose_via_daemon`` in ``coord/commands/status.py``."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/test-plan", params, timeout=120.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: test-plan via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


@click.command(
    "test-plan",
    help=(
        "Generate (or display) a smoke test plan for a completed assignment.\n\n"
        "On first call the plan is generated by calling claude -p (Haiku by default) "
        "with the PR diff, CLAUDE.md, artifact manifest, and issue body.  The result "
        "is cached in the database.  Subsequent calls return the cached plan instantly "
        "without invoking Claude.\n\n"
        "Use --refresh to regenerate and overwrite the cached plan."
    ),
)


@click.argument("assignment_id")
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Regenerate the plan even if a cached one exists.",
)


@click.option(
    "--model",
    default="haiku",
    show_default=True,
    help="Claude model alias to use for plan generation.",
)


@_CONFIG_OPTION
def test_plan_cmd(
    assignment_id: str,
    refresh: bool,
    model: str,
    config_path: Path,
) -> None:
    """Generate or display the smoke test plan for ASSIGNMENT_ID."""
    # #851: the assignment row (and its cached test_plan) live in the
    # daemon's canonical DB. `generate_plan` queries the local DB directly,
    # so on a thin client (empty local DB) it always reports "not found"
    # even for a perfectly valid id — the same #584-class gap `diagnose` /
    # `merge` / `reconcile-merges` already had. Route the whole command to
    # the daemon, mirroring that pattern. COORD_TEST_PLAN_ON_DAEMON guards
    # the daemon against re-routing to itself.
    from coord.client import resolve_board_service  # noqa: PLC0415

    svc = resolve_board_service()
    if svc is not None and not os.environ.get("COORD_TEST_PLAN_ON_DAEMON"):
        _test_plan_via_daemon(
            svc,
            {"assignment_id": assignment_id, "refresh": refresh, "model": model},
        )
        return

    from coord.state import get_test_plan, set_test_plan
    from coord.test_orchestrator import find_local_repo_path, generate_plan

    cfg = _load_config(config_path)

    # ── Cache hit path ────────────────────────────────────────────────────
    if not refresh:
        cached = get_test_plan(assignment_id)
        if cached is not None:
            click.echo(json.dumps(cached, indent=2))
            return

    # ── Generate ──────────────────────────────────────────────────────────
    click.echo(
        f"Generating smoke test plan for assignment {assignment_id!r} "
        f"(model: {model})...",
        err=True,
    )
    plan = generate_plan(assignment_id, cfg, model=model)

    # ── Capture branch HEAD SHA for staleness detection ────────────────────
    # Read the assignment's branch from the DB, then resolve the HEAD SHA on
    # the local machine so the TUI can detect when the branch has advanced
    # since this plan was generated and trigger a refresh automatically.
    branch_head = _get_assignment_branch_head(assignment_id, cfg, find_local_repo_path)

    # Persist (always, even the fallback — so a subsequent call without
    # --refresh shows the cached result rather than hitting Claude again).
    # Always write branch_head (even when None) so stale SHAs from a prior
    # run are cleared.
    set_test_plan(assignment_id, plan, branch_head=branch_head)

    click.echo(json.dumps(plan, indent=2))