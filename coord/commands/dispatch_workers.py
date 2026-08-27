"""`assign --interactive` mode implementations: review/smoke/troubleshoot/
chat/fix/rework/merge/plain-interactive/headless dispatch. Split out of
dispatch.py to keep either file from becoming a new god-file.
Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click
import httpx

from coord import github_ops
from coord.config import Config


from coord.commands.review import (
    _prompt_and_relay_review_verdict,
    _prompt_and_relay_test_verdict,
)


def _echo_artifact_stash(fr: object) -> None:
    """Echo the artifact-stash outcome from a remote interactive finalize (#1295).

    ``fr.artifacts_stashed`` is ``None`` when no stash was even attempted
    (no ``artifact_paths`` configured for the repo) — say nothing in that
    case, it's not news.  ``0`` means a stash DID run — on the remote host,
    over ssh — but 0 files matched the configured globs; that's the exact
    silent failure the issue reported (stderr redirected to ``/dev/null``
    hid the agent-side warning), so surface it loudly here instead of
    letting it disappear again on the operator's own terminal.
    """
    n = getattr(fr, "artifacts_stashed", None)
    if n is None:
        return
    if n == 0:
        click.echo(
            "  warning: remote stash copied 0 artifacts — check "
            "artifact_paths config and that the build actually produced "
            "the expected outputs",
            err=True,
        )
    else:
        click.echo(f"  stashed {n} artifact(s)")


def _smoke_report_reminder(*, assignment_id: str, smoke_of: str) -> str:
    """The reporting contract prepended to an interactive smoke briefing
    (#1349, updated #1351).

    Recording the verdict is the ONE artifact this session exists to produce.
    Until #1351, a test verdict had NO PATH-independent fallback channel — a
    review has always had one (the transcript-recoverable ``REVIEW_VERDICT``
    block, #606/#651): if ``coord test`` never ran, or ran and failed, the
    verdict was simply lost and the operator was re-prompted on exit for
    something the agent was dispatched to do. #1351 closes that gap by giving
    the Test gate the same two-channel contract a review has always had: a
    PRIMARY fast path (``coord test``, written straight to the board) plus a
    BACKUP structured block (``TEST_VERDICT:``/``TEST_REASON:``/
    ``END_TEST``) recovered from the session transcript even when the
    primary path never ran. Both are REQUIRED, every time — the backup does
    not excuse skipping the primary, and vice versa.

    Extracted to module scope so the contract is unit-testable — the briefing
    text IS the product here, and a silent edit that drops the obligation is
    exactly the drift this session is trying to stop.
    """
    return (
        f"[Coordinator smoke assignment {assignment_id}] HUMAN-ATTENDED "
        "interactive smoke test.\n"
        "  Record your verdict TWICE before you finish — belt and braces, "
        "neither step substitutes for the other, even if the session was "
        "short or the operator seems done:\n"
        "  a. PRIMARY (do this first, once the operator has a clear "
        "position):\n"
        f"       coord test --passed {smoke_of}\n"
        f"       coord test --fail {smoke_of} --reason \"<full failure brief>\"\n"
        "     Run exactly one of them. Then CHECK THE OUTPUT: the command "
        "prints a confirmation. If it errored, or `coord` is not on your "
        "PATH, say so plainly to the operator — and fall through to step b "
        "anyway; it is REQUIRED regardless. Do NOT report the verdict as "
        "recorded when you have not seen it confirmed.\n"
        "  b. BACKUP (always do this too, even after a successful step a): "
        "at the END of your session, output your verdict in this exact "
        "format:\n\n"
        "TEST_VERDICT: passed\n"
        "TEST_REASON:\n"
        "<none, or brief notes>\n"
        "END_TEST\n\n"
        "Or for a failure:\n\n"
        "TEST_VERDICT: failed\n"
        "TEST_REASON:\n"
        "<the COMPLETE failure brief: what was checked, expected vs actual, "
        "the repro steps, and the suspected files/area — not just one "
        "line; this reason IS what the fix worker is briefed with, so make "
        "it self-contained>\n"
        "END_TEST\n\n"
        "This printed block is the PATH-independent fallback recovered from "
        "your session transcript even when step a never ran or failed — it "
        "is REQUIRED every time, not just when `coord test` is unavailable. "
        "`END_TEST` is a HARD REQUIREMENT: the coordinator only records a "
        "verdict recovered this way when it sees that exact line. The LAST "
        "LINE of your LAST MESSAGE must be exactly `END_TEST` on its own "
        "line, with nothing after it.\n"
        f"  Then run `coord report-result --assignment {assignment_id} "
        "--status done --summary <one-line summary>` so this session's row "
        "closes.\n\n"
    )


def _dispatch_review_of(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    model: str | None,
    dry_run: bool,
    review_of: str,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_data: dict,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _svc: object,
    _interactive_board: object,
    _issue_ctx: str,
) -> None:
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        _launch_via_tmux as _tmux_launch,
        finalize_interactive_exit,
        launch_human_attended_interactive,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )

    from coord.review import (  # noqa: PLC0415
        REVIEWER_SYSTEM_PROMPT,
        build_review_briefing,
        read_repo_claude_md,
    )
    from coord.models import Assignment  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        build_board as _build_board_rv,
        record_dispatched_assignment,
        save_board as _save_board_rv,
    )
    from coord.agent import AssignmentSpec as _AssignmentSpecRv  # noqa: PLC0415

    _rv_board = _interactive_board(_build_board_rv)
    work = _rv_board.find_by_id(review_of)
    if work is None:
        click.echo(
            f"error: --review-of {review_of}: no such assignment on the "
            "board (use the work id from `coord status`).",
            err=True,
        )
        sys.exit(2)
    if not work.branch:
        click.echo(
            f"error: work assignment {review_of} has no branch to review.",
            err=True,
        )
        sys.exit(2)

    # Track B / #486: the review runs either on the LOCAL TTY (in the
    # live checkout) or on a REMOTE machine over ssh+tmux.  A review is
    # read-only either way (no worktree, no branch mutation), so the
    # remote path is the lowest-risk Track-B leg.
    if _is_local:
        # Expand `~` — the path is handed straight to a local child cwd.
        review_repo_path = str(
            Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
        )
    else:
        # Keep the raw path so the remote shell expands `~`/$HOME itself.
        review_repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"
    review_default_branch = repo_cfg.default_branch or "main"
    resolved_model = model if model else cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]

    # CLAUDE.md: local → read the live checkout; remote → leave empty and
    # have the reviewer read ./CLAUDE.md in the remote checkout it sits
    # in (its actual rules, not the coordinator's possibly-divergent copy).
    claude_md = (
        read_repo_claude_md(Path(review_repo_path)) if _is_local else ""
    )

    # #612: embed the merge-base (three-dot) diff so the reviewer has
    # nothing to compute — a stale-base diff would show already-merged
    # commits as spurious deletions (#546).  Prefer the PR diff via gh
    # when a PR exists; otherwise (the #546 no-PR case) compute the
    # local three-dot diff in the live checkout.  Remote / on-failure
    # → diff_text=None and the briefing keeps its fallback instructions.
    review_diff_text: str | None = None
    try:
        _existing_pr = github_ops.find_pr_for_branch(
            repo_cfg.github, work.branch
        )
    except RuntimeError:
        _existing_pr = None
    if _existing_pr is not None:
        review_diff_text = github_ops.pr_diff(
            repo_cfg.github, _existing_pr["number"]
        )
    elif _is_local:
        # No PR: compute the branch's own changes locally with a
        # three-dot (merge-base) diff against origin/<default_branch>.
        try:
            subprocess.run(
                ["git", "-C", review_repo_path, "fetch", "origin"],
                capture_output=True, text=True, timeout=60,
            )
            _diff_proc = subprocess.run(
                [
                    "git", "-C", review_repo_path, "diff",
                    f"origin/{review_default_branch}...origin/{work.branch}",
                ],
                capture_output=True, text=True, timeout=60,
            )
            if _diff_proc.returncode == 0 and _diff_proc.stdout.strip():
                review_diff_text = _diff_proc.stdout
                if len(review_diff_text) > 60000:
                    review_diff_text = (
                        review_diff_text[:60000]
                        + "\n... [diff truncated at 60000 chars] ..."
                    )
        except (subprocess.SubprocessError, OSError):
            review_diff_text = None

    review_briefing = build_review_briefing(
        pr_number=None,
        pr_url=None,
        repo_github=repo_cfg.github,
        repo_name=repo,
        issue_number=issue,
        issue_title=issue_title,
        issue_body=issue_data.get("body", ""),
        branch=work.branch,
        worker_machine=work.machine_name or machine,
        same_as_worker=False,
        reviews_cfg=cfg.reviews,
        repo_claude_md=claude_md,
        default_branch=review_default_branch,
        review_iteration=getattr(work, "review_iteration", 0) or 0,
        diff_text=review_diff_text,
    )
    # #651 (reversed by #1457): #651 told the reviewer to report via
    # report-result "instead of" the REVIEW_VERDICT block the briefing's
    # tail (below) describes — two conflicting instructions for the same
    # handoff — and framed the printed block as PRIMARY with
    # `coord report-result` as an OPTIONAL fast path never actually asked
    # for. That left the "preferred self-report path" (the #606 PATH-fix's
    # own words, coord/interactive.py:_with_coord_on_path) unreachable
    # without a human asking for it every session: the operator always had
    # to prompt the agent to run report-result before exiting. #1457: BOTH
    # are now REQUIRED, belt and braces — `coord report-result` FIRST as
    # the authoritative write straight to the coordinator's board (its
    # PATH is fixed for exactly this by #606, its assignment id is $COORD_
    # ASSIGNMENT_ID exported below, and #590 makes it work from a remote
    # session too), and the REVIEW_VERDICT block ALWAYS ALSO, unconditionally,
    # as the PATH-independent backup the #606 transcript-floor recovery scans
    # for when report-result never ran or failed. Neither step substitutes
    # for the other, and the exit finalize's `already_recorded` check
    # (coord/interactive.py:_assignment_already_recorded) means a successful
    # report-result is never clobbered by the backstop.
    _remote_note = (
        ""
        if _is_local
        else (
            "running on a REMOTE machine. You are in the live checkout — "
            "read ./CLAUDE.md (and any sub-repo CLAUDE.md) for the project "
            "rules before reviewing. "
        )
    )
    report_reminder = (
        f"[Coordinator review assignment {assignment_id}] This is a "
        f"HUMAN-ATTENDED interactive review {_remote_note}When you finish, "
        "record your verdict TWICE — belt and braces, neither step "
        "substitutes for the other:\n"
        "  1. REQUIRED, PRIMARY — write your full findings (every blocking "
        f"item, with file:line) to a file (e.g. /tmp/review-{assignment_id}.md) "
        "and run, BEFORE you end your session:\n"
        f"     coord report-result --assignment {assignment_id} "
        "--status done --verdict approve|request-changes "
        f"--summary <one-line summary> --body-file /tmp/review-{assignment_id}.md\n"
        "     This writes your verdict straight to the coordinator's shared "
        "board (#590) — the authoritative record, and it reaches the merge "
        "gate the moment you run it. `coord` is on your PATH (the coordinator "
        "prepends its venv bin for this) and your assignment id is also in "
        "$COORD_ASSIGNMENT_ID. Check the command's output for a "
        "confirmation; if it errors, say so plainly — then continue to "
        "step 2 regardless.\n"
        "  2. REQUIRED, BACKUP — ALWAYS ALSO, regardless of whether step 1 "
        "ran or succeeded, end your session by outputting the "
        "`REVIEW_VERDICT: / REVIEW_BODY: / END_REVIEW` block described at "
        "the end of this briefing, with the SAME full findings in "
        "REVIEW_BODY. This is the PATH-independent fallback — it is "
        "recovered from your session transcript even when step 1 never ran. "
        "It does NOT replace step 1; do both, every time.\n"
        "     The three marker lines are parsed by machine: emit each at "
        "the start of its own line as literal plain text, with NO Markdown "
        "decoration. `**REVIEW_VERDICT: request-changes**` is WRONG and is "
        "silently dropped (#1346); `REVIEW_VERDICT: request-changes` is "
        "right. The body between the markers may be Markdown.\n"
        "  Do NOT run any `gh` commands; the coordinator posts the verdict + "
        "findings for you.\n\n"
    )
    effective_briefing = _issue_ctx + report_reminder + review_briefing

    spec = _AssignmentSpecRv(
        repo_name=repo,
        repo_path=review_repo_path,
        issue_number=issue,
        issue_title=f"[review] {issue_title}",
        briefing=effective_briefing,
        model=resolved_model,
        type="review",
        provider="claude-pty",
    )
    # A review is READ-ONLY: use the reviewer system prompt (not the
    # worker default build_command would otherwise apply for an
    # unrecognised type) and drop Edit/Write from the tool set so the
    # session can't mutate the live checkout.  Bash stays for
    # git fetch/diff/log; Read/Grep/Glob for inspecting the code.
    argv = provider.build_command(
        spec,
        resolved_model=resolved_model,
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        allowed_tools="Read,Bash,Grep,Glob",
    )
    # Remote: a bare "claude" is not on the SSH login PATH (#424/#425);
    # swap argv[0] for the absolute path the remote shell can find.
    if not _is_local:
        argv = ["~/.local/bin/claude"] + list(argv)[1:]

    _rv_location = (
        "local TTY" if _is_local
        else f"{machine_obj.host} (remote tmux)"
    )
    click.echo(
        f"{machine} ({_rv_location}) → REVIEW of #{issue} "
        f"on branch {work.branch}: {issue_title}"
    )
    click.echo(
        "  mode: HUMAN-ATTENDED interactive review "
        "(migration A1 / Track B #486)"
    )
    click.echo(
        f"  assignment id: {assignment_id}  (review_of={review_of})"
    )
    if _is_local:
        click.echo(
            f"  cwd: {review_repo_path} (live checkout — read-only, "
            "no worktree)"
        )
    else:
        click.echo(
            f"  remote checkout: {review_repo_path} on "
            f"{machine_obj.host} (read-only, no worktree)"
        )
    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        return

    review_assignment = Assignment(
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=f"[review] {issue_title}",
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        branch=work.branch,
        dispatched_at=_time.time(),
        type="review",
        review_of_assignment_id=review_of,
        review_target=work.branch,
        model=resolved_model,
        provider_name="claude-pty",
    )
    record_dispatched_assignment(
        assignment=review_assignment, repo_github=repo_cfg.github
    )
    if _svc is None:
        _save_board_rv(_build_board_rv())
    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

    started_at = _time.time()
    if _is_local:
        exit_code = launch_human_attended_interactive(
            argv,
            effective_briefing,
            assignment_id=assignment_id,
            cwd=review_repo_path,
        )
        if exit_code != 0:
            click.echo(
                f"  claude exited with status {exit_code}", err=True
            )

        _sname = _tmux_name(assignment_id) if _tmux_avail() else None
        if _sname and _tmux_alive(_sname):
            click.echo(
                f"  session still running in tmux: {_sname}\n"
                f"  reattach with:  coord reattach {assignment_id}"
            )
            sys.exit(0)

        # finalize with worktree_path=None — the backstop must never push
        # or remove the live checkout.  If the reviewer ran
        # `coord report-result --verdict`, finalize sees the terminal row
        # and leaves the verdict untouched.
        try:
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                worktree_path=None,
                base_branch=review_default_branch,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=None,
            )
            if finalize_result.already_recorded:
                click.echo("  verdict recorded via `coord report-result`")
            else:
                # The reviewer exited without running `coord
                # report-result`.  Mirror the remote path (#486d): prompt
                # the operator here (this is a TTY) and relay the verdict
                # through the same issue_store seam, so the merge gate /
                # Fix routing sees it instead of silently stalling on a
                # missing verdict.
                _verdict_cmd = (
                    f"    coord report-result --assignment {assignment_id} "
                    "--status done --verdict approve|request-changes "
                    "--summary <one-line summary>"
                )
                if not _prompt_and_relay_review_verdict(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    verdict_cmd_hint=_verdict_cmd,
                    started_at=started_at,
                    # Local review — session ran on THIS machine; no ssh needed.
                    # The transcript-floor already scanned the local projects dir
                    # in finalize_interactive_exit; ssh_target=None skips the
                    # remote SSH path while still checking the board (#877).
                    ssh_target=None,
                ):
                    click.echo(
                        "  review session ended with no verdict reported "
                        f"(status={finalize_result.terminal_status}) — the "
                        "merge gate stays blocked until a verdict is "
                        "reported."
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: backstop failed to record review exit: {exc}",
                err=True,
            )
        return

    # ── REMOTE REVIEW (Track B / #486) ────────────────────────────
    # Read-only: cd into the remote LIVE checkout, fetch+prune so the
    # reviewer can diff origin/<branch>, then launch the reviewer.  NO
    # worktree and NO branch mutation — the live checkout is the worker
    # worktree base and must not be disturbed.  Verdict comes back via
    # the operator running `coord report-result` on THIS coordinator
    # (the assignment row lives here); a remote report-result would
    # write the remote DB and never reach the merge gate (#486d).
    import shlex as _shlex_rv  # noqa: PLC0415

    _rp_sh = (
        "$HOME/" + review_repo_path[2:]
        if review_repo_path.startswith("~/")
        else ("$HOME" if review_repo_path == "~" else review_repo_path)
    )
    _claude_args = _shlex_rv.join(list(argv)[1:])
    _remote_cmd = (
        f"cd {_rp_sh}"
        f" && git fetch origin --prune 2>/dev/null || true"
        f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {_claude_args}"
    )
    _tmux_host = TmuxHost(ssh_target=machine_obj.host)
    _sname = _tmux_name(assignment_id)

    # Echo the briefing to the LOCAL terminal before attaching, so the
    # operator can read it before pressing Enter (mirrors the remote
    # work path).
    if effective_briefing.strip():
        _hdr = (
            "--- seeded briefing -- review below; "
            "submit the pre-filled input in Claude to send ---"
        )
        _ftr = "-" * len(_hdr)
        _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
        try:
            os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
        except OSError:
            pass

    _rc = _tmux_launch(
        argv,
        effective_briefing,
        _sname,
        cwd=None,
        host=_tmux_host,
        raw_shell_cmd=_remote_cmd,
    )
    if _rc is None:
        click.echo(
            "  error: could not create remote tmux session on "
            f"{machine_obj.host}",
            err=True,
        )
        sys.exit(1)
    exit_code = _rc
    if exit_code != 0:
        click.echo(f"  claude exited with status {exit_code}", err=True)

    _still_alive = _tmux_alive(_sname, host=_tmux_host)
    # Verdict-out (#486d): the verdict is recorded on THIS coordinator,
    # where the assignment row lives and the merge gate reads
    # `review_verdict` — never on the remote machine's DB.
    _verdict_cmd = (
        f"    coord report-result --assignment {assignment_id} "
        "--status done --verdict approve|request-changes "
        "--summary <one-line summary>"
    )
    if _still_alive:
        click.echo(
            f"  session still running in remote tmux: {_sname}\n"
            f"  reattach with:  ssh -t {machine_obj.host}"
            f" tmux attach-session -t {_sname}"
        )
        click.echo(
            "  to record the verdict (the merge gate keys on it), run ON "
            f"THIS coordinator:\n{_verdict_cmd}"
        )
        sys.exit(0)

    # Session ended.  Record a terminal state so the review row does NOT
    # linger as a phantom 'running' worker that holds the issue claim
    # forever — the bug this path used to have (it printed the verdict
    # reminder and exited, never going terminal).  A review is
    # read-only, so finalize with worktree_path=None / repo_path=None:
    # the backstop only writes the coordinator DB (no push, no worktree
    # touch), identical to the local review path above.  An operator
    # `coord report-result` is respected (already_recorded → no clobber).
    try:
        finalize_result = finalize_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=issue,
            machine_name=machine,
            worktree_path=None,
            base_branch=review_default_branch,
            exit_code=exit_code,
            started_at=started_at,
            log_path=None,
            repo_path=None,
            # #617: this review ran on the REMOTE host, so its Claude
            # session transcript lives THERE — hand the transcript-floor
            # the ssh target so it recovers the verdict + findings from
            # the session's OWN host instead of scanning this (blind)
            # coordinator's `~/.claude/projects`.  Without it the #606
            # recovery always misses for a remote review and the exit
            # falls straight to the operator prompt (the #607 silent
            # drop).  Mirrors the `coord reattach` remote-review path.
            ssh_target=machine_obj.host,
        )
        if finalize_result.already_recorded:
            click.echo("  verdict recorded via `coord report-result`")
        else:
            # #486d: don't leave the verdict as a manual step — prompt the
            # operator here (on the coordinator, where the row lives) and
            # relay it, so the merge gate / leg-3 Fix routing sees it.
            _prompt_and_relay_review_verdict(
                assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                verdict_cmd_hint=_verdict_cmd,
                started_at=started_at,
                # #877: remote review — session ran on machine_obj.host, so the
                # transcript lives THERE.  Pass the ssh_target so the board-
                # content gate can re-run the remote transcript-floor here
                # (a 2nd attempt; the first ran in finalize_interactive_exit via
                # its own ssh_target).  Catches any timing window where the
                # JSONL wasn't fully flushed when finalize ran.
                ssh_target=machine_obj.host,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: backstop failed to record review exit: {exc}",
            err=True,
        )
    sys.exit(exit_code)


def _dispatch_smoke_of(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    model: str | None,
    dry_run: bool,
    smoke_of: str,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_data: dict,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _svc: object,
    _interactive_board: object,
    _issue_ctx: str,
) -> None:
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        _launch_via_tmux as _tmux_launch,
        finalize_interactive_exit,
        launch_human_attended_interactive,
        snapshot_live_checkout_for_smoke,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )

    from coord.models import Assignment as _AssignmentSm  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        build_board as _build_board_sm,
        get_test_plan as _get_test_plan_sm,
        record_dispatched_assignment as _record_sm,
        save_board as _save_board_sm,
    )
    from coord.agent import AssignmentSpec as _AssignmentSpecSm  # noqa: PLC0415

    _sm_board = _interactive_board(_build_board_sm)
    work = _sm_board.find_by_id(smoke_of)
    if work is None:
        click.echo(
            f"error: --smoke-of {smoke_of}: no such assignment on the "
            "board (use the work id from `coord status`).",
            err=True,
        )
        sys.exit(2)
    if not work.branch:
        click.echo(
            f"error: work assignment {smoke_of} has no branch to test.",
            err=True,
        )
        sys.exit(2)

    # #1010 (mirrors --review-of / Track B #486): smoke is READ-ONLY (no
    # worktree, no branch mutation) either way, so the remote path is the
    # same low-risk shape as the review path — ssh+tmux into the live
    # checkout, no worktree machinery needed.
    if _is_local:
        smoke_repo_path = str(
            Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
        )
    else:
        # Keep the raw path so the remote shell expands `~`/$HOME itself.
        smoke_repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"
    smoke_default_branch = repo_cfg.default_branch or "main"
    resolved_model = model if model else cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]

    # Surface the cached smoke-test plan (#342) when one exists so the
    # agent can lead with the concrete steps instead of re-deriving them.
    try:
        _plan = _get_test_plan_sm(smoke_of)
    except Exception:  # noqa: BLE001
        _plan = None
    if _plan and isinstance(_plan, dict) and _plan.get("steps"):
        import json as _json_sm  # noqa: PLC0415
        _plan_block = (
            "A cached smoke-test plan exists for this branch:\n\n"
            "```json\n" + _json_sm.dumps(_plan, indent=2) + "\n```\n"
        )
    else:
        _plan_block = (
            "No cached smoke-test plan was found. Run "
            f"`coord test-plan {smoke_of}` to generate one (it reads the "
            "PR diff, the repo's CLAUDE.md and the artifact manifest), "
            "then read it back to the operator.\n"
        )

    INTERACTIVE_SMOKE_SYSTEM_PROMPT = (
        "You are a human-attended smoke-test guide dispatched by the "
        "coordinator. A human operator is at the keyboard with you. Your "
        "job is to walk them through validating a completed branch and "
        "then record their verdict.\n\n"
        "Rules:\n"
        "- Do NOT modify code, push commits, or open/merge PRs. You only "
        "help validate. (Edit/Write are not available to you.)\n"
        "- Do NOT run `gh` commands. The coordinator owns GitHub.\n"
        "- You MAY run git (read-only), build/run commands, and the "
        "`coord pull-artifact` / `coord test-plan` / `coord test` "
        "commands.\n"
        "- Keep it conversational: propose ONE concrete next command at a "
        "time, wait for the operator to run it (or run it yourself when "
        "it's safe and read-only) and tell you what they saw.\n\n"
        "Flow:\n"
        "1. Read the smoke-test plan (below, or generate one). List the "
        "checks for the operator.\n"
        "2. Offer to pull the prebuilt artifact for this branch with "
        "`coord pull-artifact <work_aid>` so they don't have to rebuild.\n"
        "3. Walk through each check. Ask what they observed. If something "
        "is wrong, interview them for a clear repro (expected vs actual, "
        "suspected area/files) — this becomes the fix brief.\n"
        "4. When every check has a clear position, record the verdict:\n"
        "   - All good  → run `coord test --passed <work_aid>`\n"
        "   - Broken    → run `coord test --fail <work_aid> --reason "
        "\"<story>\"` where <story> is the COMPLETE failure brief the fix "
        "worker needs: what was checked, expected vs actual, the repro "
        "steps, and the suspected files/area — not just one line. This "
        "reason IS what the fix worker is briefed with, so make it "
        "self-contained.\n"
        "   Then ALSO print the TEST_VERDICT:/TEST_REASON:/END_TEST backup "
        "block described in your first message, even after `coord test` "
        "succeeds — it is the PATH-independent fallback recovered from this "
        "session's transcript if `coord test` didn't actually land.\n"
        "   Then tell the operator exactly what happens next (the TUI "
        "will offer the fix or merge step).\n"
    )

    smoke_briefing = (
        f"# Smoke-test assignment: {repo_cfg.github} #{issue}\n\n"
        f"**Issue:** {issue_title}\n"
        f"**Branch under test:** `{work.branch}` "
        f"(worker: {work.machine_name or machine})\n"
        f"**Work assignment id (use this for `coord test` / "
        f"`coord pull-artifact`):** `{smoke_of}`\n"
        f"**Repo checkout:** {smoke_repo_path}\n"
        f"**Default branch:** {smoke_default_branch}\n\n"
        "## ⚠ Do NOT move this checkout's branch (#601)\n\n"
        f"`{smoke_repo_path}` is the **live checkout that runs the "
        "coordinator itself** (and the worktree base for workers). Do "
        "**NOT** `git checkout` / `git switch` / `git reset` / "
        "`git stash` it — checking out the branch here silently "
        "downgrades the running `coord` to this branch's code until it's "
        "restored. To inspect the branch under test WITHOUT moving it:\n"
        f"  - `git -C {smoke_repo_path} fetch origin && "
        f"git -C {smoke_repo_path} diff {smoke_default_branch}...origin/{work.branch}`\n"
        f"  - `git -C {smoke_repo_path} show origin/{work.branch}:<path>` for a single file\n"
        f"  - or make your OWN scratch worktree: "
        f"`git -C {smoke_repo_path} worktree add /tmp/smoke-{smoke_of} origin/{work.branch}` "
        f"(remove it with `git -C {smoke_repo_path} worktree remove /tmp/smoke-{smoke_of}` when done)\n"
        "  - prefer `coord pull-artifact` (above) for the prebuilt binary.\n\n"
        f"## Issue body\n\n{issue_data.get('body', '') or '(none)'}\n\n"
        f"## Smoke-test plan\n\n{_plan_block}\n"
        "## Your job\n\n"
        "Guide the operator through validating this branch (see the "
        "system prompt for the flow), then record the verdict with "
        f"`coord test --passed {smoke_of}` or `coord test --fail "
        f"{smoke_of} --reason \"...\"`.\n"
    )

    report_reminder = _smoke_report_reminder(
        assignment_id=assignment_id, smoke_of=smoke_of
    )
    effective_briefing = _issue_ctx + report_reminder + smoke_briefing

    spec = _AssignmentSpecSm(
        repo_name=repo,
        repo_path=smoke_repo_path,
        issue_number=issue,
        issue_title=f"[smoke] {issue_title}",
        briefing=effective_briefing,
        model=resolved_model,
        type="smoke",
        provider="claude-pty",
    )
    # READ-ONLY like --review-of: no Edit/Write — the smoke agent
    # validates, it does not fix.  Bash stays for build/run + the
    # coord helper commands; Read/Grep/Glob for inspecting the code.
    argv = provider.build_command(
        spec,
        resolved_model=resolved_model,
        system_prompt=INTERACTIVE_SMOKE_SYSTEM_PROMPT,
        allowed_tools="Read,Bash,Grep,Glob",
    )
    # Remote: a bare "claude" is not on the SSH login PATH (#424/#425).
    if not _is_local:
        argv = ["~/.local/bin/claude"] + list(argv)[1:]

    _sm_location = (
        "local TTY" if _is_local
        else f"{machine_obj.host} (remote tmux)"
    )
    click.echo(
        f"{machine} ({_sm_location}) → SMOKE TEST of #{issue} "
        f"on branch {work.branch}: {issue_title}"
    )
    click.echo(
        "  mode: HUMAN-ATTENDED interactive smoke test (leg 3c / A3, #1010)"
    )
    click.echo(
        f"  assignment id: {assignment_id}  (smoke_of={smoke_of})"
    )
    if _is_local:
        click.echo(
            f"  cwd: {smoke_repo_path} (live checkout — read-only, "
            "no worktree)"
        )
    else:
        click.echo(
            f"  remote checkout: {smoke_repo_path} on "
            f"{machine_obj.host} (read-only, no worktree)"
        )
    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        return

    smoke_assignment = _AssignmentSm(
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=f"[smoke] {issue_title}",
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        branch=work.branch,
        dispatched_at=_time.time(),
        type="smoke",
        review_of_assignment_id=smoke_of,
        review_target=work.branch,
        model=resolved_model,
        provider_name="claude-pty",
    )
    _record_sm(assignment=smoke_assignment, repo_github=repo_cfg.github)
    if _svc is None:
        _save_board_sm(_build_board_sm())
    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

    # #923: Test-verdict backstop hint — shared by both the local and
    # remote paths below.  The verdict itself lands on the shared board the
    # moment `coord test` runs INSIDE the session (routes through the
    # daemon, #590, same as `coord report-result`) — this hint/prompt only
    # covers the case where the agent exited without running it.
    _test_verdict_cmd = (
        f"    coord test --passed {smoke_of}   # all good\n"
        f"    coord test --fail {smoke_of} --reason \"<story>\"   # broken"
    )

    if _is_local:
        # #1256: snapshot the live checkout's branch + dirty-path baseline
        # BEFORE the agent can mutate it (e.g. a path-scoped
        # `git checkout <branch> -- <path>` used to exercise an agent-side
        # file that must actually sit in this editable-install checkout to
        # take effect).  finalize_interactive_exit's restore-on-exit safety
        # net reverts anything new dirtied since this point.
        snapshot_live_checkout_for_smoke(smoke_repo_path, assignment_id)

        started_at = _time.time()
        exit_code = launch_human_attended_interactive(
            argv,
            effective_briefing,
            assignment_id=assignment_id,
            cwd=smoke_repo_path,
        )
        if exit_code != 0:
            click.echo(f"  claude exited with status {exit_code}", err=True)

        _sname = _tmux_name(assignment_id) if _tmux_avail() else None
        if _sname and _tmux_alive(_sname):
            click.echo(
                f"  session still running in tmux: {_sname}\n"
                f"  reattach with:  coord reattach {assignment_id}"
            )
            sys.exit(0)

        # worktree_path=None: read-only smoke runs in the live checkout, the
        # backstop must never push or remove it.  The verdict that matters
        # is the `coord test` write on the WORK row, not this session's row.
        # smoke_repo_path=... triggers the #1256 restore-on-exit safety net.
        try:
            _fr = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                worktree_path=None,
                base_branch=smoke_default_branch,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=None,
                smoke_repo_path=smoke_repo_path,
                smoke_of=smoke_of,
            )
            if _fr.test_verdict_recovered:
                click.echo(
                    f"  test verdict {_fr.test_verdict_recovered!r} recovered "
                    "from the session transcript and recorded on the work "
                    f"assignment {smoke_of} (#1351)"
                )
            if _fr.smoke_restored_paths:
                click.echo(
                    "  live checkout restored (#1256): reverted "
                    + ", ".join(_fr.smoke_restored_paths)
                )
            if _fr.smoke_restore_error:
                click.echo(
                    f"  warning: live-checkout restore failed: "
                    f"{_fr.smoke_restore_error}",
                    err=True,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: backstop failed to record smoke exit: {exc}",
                err=True,
            )

        try:
            if not _prompt_and_relay_test_verdict(
                work_assignment_id=smoke_of,
                smoke_assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                verdict_cmd_hint=_test_verdict_cmd,
            ):
                click.echo(
                    "  smoke session ended with no test verdict recorded "
                    "— the merge gate stays blocked until a verdict is "
                    "reported."
                )
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: test-verdict backstop failed: {exc}",
                err=True,
            )
        return

    # ── REMOTE SMOKE TEST (#1010) ──────────────────────────────────
    # Read-only: cd into the remote LIVE checkout, fetch+prune so the
    # smoke agent can inspect origin/<branch>, then launch it.  NO
    # worktree and NO branch mutation — the live checkout is the worker
    # worktree base and must not be disturbed (same shape as the remote
    # review path).  The Test verdict is recorded on the shared board the
    # moment `coord test --passed|--fail` runs INSIDE the remote session
    # (routes through the daemon, #590) — no remote-specific plumbing
    # needed for that part; only the SESSION row's terminal state (this
    # smoke assignment, not the WORK row) needs a coordinator-side backstop.
    import shlex as _shlex_sm  # noqa: PLC0415

    _rp_sh = (
        "$HOME/" + smoke_repo_path[2:]
        if smoke_repo_path.startswith("~/")
        else ("$HOME" if smoke_repo_path == "~" else smoke_repo_path)
    )
    # #1256: snapshot the remote live checkout's branch + dirty-path
    # baseline before launch — same rationale as the local branch above.
    snapshot_live_checkout_for_smoke(
        _rp_sh, assignment_id, ssh_target=machine_obj.host
    )
    _claude_args = _shlex_sm.join(list(argv)[1:])
    _remote_cmd = (
        f"cd {_rp_sh}"
        f" && git fetch origin --prune 2>/dev/null || true"
        f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {_claude_args}"
    )
    _tmux_host = TmuxHost(ssh_target=machine_obj.host)
    _sname = _tmux_name(assignment_id)

    # Echo the briefing to the LOCAL terminal before attaching, so the
    # operator can read it before pressing Enter (mirrors the remote
    # review/work paths).
    if effective_briefing.strip():
        _hdr = (
            "--- seeded briefing -- review below; "
            "submit the pre-filled input in Claude to send ---"
        )
        _ftr = "-" * len(_hdr)
        _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
        try:
            os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
        except OSError:
            pass

    started_at = _time.time()
    _rc = _tmux_launch(
        argv,
        effective_briefing,
        _sname,
        cwd=None,
        host=_tmux_host,
        raw_shell_cmd=_remote_cmd,
    )
    if _rc is None:
        click.echo(
            "  error: could not create remote tmux session on "
            f"{machine_obj.host}",
            err=True,
        )
        sys.exit(1)
    exit_code = _rc
    if exit_code != 0:
        click.echo(f"  claude exited with status {exit_code}", err=True)

    if _tmux_alive(_sname, host=_tmux_host):
        click.echo(
            f"  session still running in remote tmux: {_sname}\n"
            f"  reattach with:  ssh -t {machine_obj.host}"
            f" tmux attach-session -t {_sname}"
        )
        sys.exit(0)

    # Session ended.  Record a terminal state for THIS SMOKE SESSION row
    # (not the WORK row) so it doesn't linger as a phantom 'running' worker
    # holding the issue claim forever — worktree_path=None/repo_path=None
    # since this is read-only (no push, no worktree touch), identical to
    # the local path above.  ssh_target lets any future transcript-floor
    # recovery read the session's OWN host (mirrors the #617 review fix).
    try:
        _fr = finalize_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=issue,
            machine_name=machine,
            worktree_path=None,
            base_branch=smoke_default_branch,
            exit_code=exit_code,
            started_at=started_at,
            log_path=None,
            repo_path=None,
            ssh_target=machine_obj.host,
            smoke_repo_path=_rp_sh,
            smoke_of=smoke_of,
        )
        if _fr.test_verdict_recovered:
            click.echo(
                f"  test verdict {_fr.test_verdict_recovered!r} recovered "
                "from the session transcript and recorded on the work "
                f"assignment {smoke_of} (#1351)"
            )
        if _fr.smoke_restored_paths:
            click.echo(
                "  live checkout restored (#1256): reverted "
                + ", ".join(_fr.smoke_restored_paths)
            )
        if _fr.smoke_restore_error:
            click.echo(
                f"  warning: live-checkout restore failed: "
                f"{_fr.smoke_restore_error}",
                err=True,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: backstop failed to record smoke exit: {exc}",
            err=True,
        )

    # #923: Test-verdict backstop — see comment above the shared
    # _test_verdict_cmd definition.
    try:
        if not _prompt_and_relay_test_verdict(
            work_assignment_id=smoke_of,
            smoke_assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=issue,
            machine_name=machine,
            verdict_cmd_hint=_test_verdict_cmd,
        ):
            click.echo(
                "  smoke session ended with no test verdict recorded "
                "— the merge gate stays blocked until a verdict is reported."
            )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: test-verdict backstop failed: {exc}",
            err=True,
        )
    sys.exit(exit_code)


def _dispatch_audit_of(
    *,
    machine: str,
    repo: str,
    model: str | None,
    dry_run: bool,
    audit_of: str,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    provider: object,
    _is_local: bool,
    _issue_ctx: str,
) -> None:
    """Milestone Outcome Audit Phase 1 (#885): human-attended, READ-ONLY
    milestone-outcome analyst for the milestone's tracking EPIC ISSUE.

    Mirrors `_dispatch_smoke_of`'s read-only / live-checkout / no-worktree
    shape, but the target (``audit_of``) is a GitHub issue number — the
    milestone's tracking epic — not a board work-assignment id, so there is
    no board lookup here. The epic's own body (goals/acceptance/plan
    checklist) and its milestone's issue states are fetched fresh and handed
    to the agent, which measures each goal against the code with shell tools
    and posts a scorecard via `coord report-result` (landing as a comment on
    the epic, per issue_number below).
    """
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.board_service import read_board as _read_board_au  # noqa: PLC0415
    from coord.board_service import write_board as _write_board_au  # noqa: PLC0415
    from coord.interactive import (  # noqa: PLC0415
        finalize_interactive_exit,
        launch_human_attended_interactive,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )
    from coord.models import Assignment as _AssignmentAu  # noqa: PLC0415
    from coord.state import record_dispatched_assignment as _record_au  # noqa: PLC0415
    from coord.agent import AssignmentSpec as _AssignmentSpecAu  # noqa: PLC0415

    if not _is_local:
        click.echo(
            "error: --audit-of is local-only for now; run it on the "
            "machine that holds the checkout.",
            err=True,
        )
        sys.exit(2)

    try:
        epic_num = int(audit_of)
    except ValueError:
        click.echo(
            f"error: --audit-of {audit_of!r} must be a GitHub issue number "
            "(the milestone's tracking epic).",
            err=True,
        )
        sys.exit(2)

    try:
        epic_data = github_ops.get_issue(repo_cfg.github, epic_num)
    except RuntimeError as e:
        click.echo(f"error: could not fetch epic issue #{epic_num}: {e}", err=True)
        sys.exit(1)

    epic_title = epic_data.get("title") or f"Issue #{epic_num}"
    epic_body = epic_data.get("body") or "(no body)"
    milestone = epic_data.get("milestone")
    if not milestone or not milestone.get("title"):
        click.echo(
            f"error: --audit-of {epic_num}: issue has no milestone — the "
            "audit needs a milestone to enumerate issue states for (assign "
            "one with `coord milestone assign` first).",
            err=True,
        )
        sys.exit(2)
    milestone_title = milestone["title"]

    try:
        milestone_issues = github_ops.get_milestone_issues(repo_cfg.github, milestone_title)
    except RuntimeError as e:
        click.echo(
            f"error: could not list issues for milestone {milestone_title!r}: {e}",
            err=True,
        )
        sys.exit(1)

    audit_repo_path = str(
        Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
    )
    audit_default_branch = repo_cfg.default_branch or "main"
    resolved_model = model if model else cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]

    # Table of milestone issue states (number/state/title/labels) so the
    # agent has them up front — it has no `gh` access to fetch this itself.
    _issue_lines = [
        "- #{num} [{state}] {title}{labels}".format(
            num=iss.get("number"),
            state=iss.get("state", "?"),
            title=iss.get("title", ""),
            labels=(
                "  ({})".format(", ".join(lbl.get("name", "") for lbl in iss.get("labels") or []))
                if iss.get("labels")
                else ""
            ),
        )
        for iss in sorted(milestone_issues, key=lambda i: i.get("number", 0))
    ]
    milestone_issues_block = "\n".join(_issue_lines) or "(no issues found under this milestone)"

    # #886 Phase 2: fetch prior `--audit-of` runs against this epic (if any) so
    # the agent measures AGAINST the last verdict — the concrete "re-ask the
    # question" loop the issue asks for — and so the next run_number is known
    # up front. Best-effort: a lookup failure just means this looks like run 1.
    import json as _json_au  # noqa: PLC0415

    from coord import issue_store as _issue_store_au  # noqa: PLC0415

    try:
        _prior_audit_runs = _issue_store_au.get_audit_runs_for_epic(repo, epic_num)
    except Exception:  # noqa: BLE001 — best-effort; treat as no prior runs
        _prior_audit_runs = []
    _next_run_number = len(_prior_audit_runs) + 1
    if _prior_audit_runs:
        _last_run = _prior_audit_runs[-1]
        _last_goals: list = []
        try:
            _last_goals = _json_au.loads(_last_run.get("audit_goals_json") or "[]")
        except (TypeError, ValueError):
            _last_goals = []
        _prior_goal_lines = [
            "- {goal}: {verdict} ({evidence})".format(
                goal=g.get("goal", "?"),
                verdict=g.get("verdict", "?"),
                evidence=g.get("evidence", "") or "no evidence recorded",
            )
            for g in _last_goals
        ]
        prior_run_block = (
            f"## Prior audit run (v{_last_run.get('audit_run_number')})\n\n"
            f"**Bottom line:** {_last_run.get('audit_bottom_line') or '(none recorded)'}\n\n"
            + ("\n".join(_prior_goal_lines) or "(no goals recorded)")
            + "\n\n**This is run v"
            + str(_next_run_number)
            + "** — measure each goal again from scratch (never trust the "
            "prior verdict without re-checking), then report whether each "
            "moved (gap→met, still open, regressed, or new).\n\n"
        )
    else:
        prior_run_block = (
            f"## Prior audit runs\n\n(none — this is run v{_next_run_number}, "
            "the first audit of this milestone.)\n\n"
        )

    INTERACTIVE_AUDIT_SYSTEM_PROMPT = (
        "You are a human-attended MILESTONE OUTCOME AUDITOR dispatched by "
        "the coordinator (#885). You are an independent analyst — measure "
        "reality, do not rubber-stamp ticket state.\n\n"
        "Rules:\n"
        "- READ-ONLY. Do NOT modify code, commit, push, or open/merge PRs "
        "(Edit/Write are not available to you).\n"
        "- Do NOT run `gh` commands. The coordinator owns GitHub — the epic "
        "body and milestone issue states are already in your briefing "
        "below.\n"
        "- You MAY run read-only git (log/diff/show/merge-base/branch -r) "
        "and any read-only shell tool (wc, grep -r, find, cat, test/coverage "
        "runs, etc.) against the LIVE checkout to measure reality.\n\n"
        "Method:\n"
        "1. Read the epic's goals, acceptance criteria, and any plan/work-"
        "order checklist from its body (below).\n"
        "2. Review the milestone's issue states (below) — which are open, "
        "closed, still in flight.\n"
        "3. For EACH goal, MEASURE it against the actual code — never trust "
        "ticket state or a self-reported summary. Concretely:\n"
        "   - decomposition/size claims -> `wc -l <file>` on the files in "
        "question\n"
        "   - \"feature X exists\" claims -> `grep -rn` for the actual "
        "symbols/call sites\n"
        "   - \"branch Y merged\" claims -> `git merge-base --is-ancestor "
        "<branch> origin/main` (or confirm the file exists on the default "
        "branch)\n"
        "   - test/coverage claims -> run the test suite or read coverage "
        "output where relevant\n"
        "4. Emit a scorecard: one row per goal — before/after, "
        "met|partial|gap, and the concrete evidence (command + result) that "
        "backs the verdict. Call out specific files/line-counts/gaps by "
        "name (e.g. a god-file that grew instead of shrank, an open seam "
        "issue that was supposed to close).\n"
        "5. End with a one-line bottom-line verdict (e.g. \"5/6 goals "
        "met\").\n"
        "6. If a PRIOR AUDIT RUN is included in your briefing below, measure "
        "each of ITS goals again too (same goal text) so the coordinator can "
        "compute the diff — which gaps closed, which are still open, whether "
        "anything regressed.\n\n"
        "When done, write BOTH of these and relay them together in ONE "
        "`coord report-result` call:\n"
        "  (a) the FULL prose scorecard to a temp file (--body-file), AND\n"
        "  (b) a STRUCTURED JSON file (--audit-json) shaped exactly like:\n"
        '      {"bottom_line": "5/6 goals met", "goals": [{"goal": '
        '"short goal name — MUST match the prior run\'s goal text exactly '
        'when re-measuring it", "metric_before": "...", "metric_after": '
        '"...", "verdict": "met|partial|gap", "evidence": "command + '
        'result"}, ...]}\n'
        "  coord report-result --assignment <assignment_id> --status done "
        "--summary \"<bottom-line, one paragraph>\" --body-file "
        "<path-to-scorecard.md> --audit-json <path-to-verdict.json>\n"
        "Both flags are REQUIRED — --body-file posts the readable scorecard "
        "as a comment; --audit-json is what makes the verdict structured, "
        "versioned, and diffable run-over-run (#886). Omitting --audit-json "
        "leaves this run un-diffable against the next one.\n"
    )

    audit_briefing = (
        f"# Milestone outcome audit: {repo_cfg.github} epic #{epic_num}\n\n"
        f"**Epic:** {epic_title}\n"
        f"**Milestone:** {milestone_title}\n"
        f"**Repo checkout:** {audit_repo_path}\n\n"
        "## ⚠ Do NOT move this checkout's branch\n\n"
        f"`{audit_repo_path}` is the **live checkout that runs the "
        "coordinator itself** (and the worktree base for workers). Do "
        "**NOT** `git checkout` / `git switch` / `git reset` / "
        "`git stash` it. Read files, run read-only git/shell commands, and "
        "measure — never move the branch.\n\n"
        f"## Epic body (goals / acceptance / plan)\n\n{epic_body}\n\n"
        f"## Milestone issue states ({milestone_title})\n\n"
        f"{milestone_issues_block}\n\n"
        f"{prior_run_block}"
        "## Your job\n\n"
        "Measure each goal against the code (see the system prompt for the "
        "method), emit a scorecard, and relay it with `coord report-result "
        f"--assignment {assignment_id} --status done --summary \"...\" "
        "--body-file <scorecard.md> --audit-json <verdict.json>`.\n"
    )

    report_reminder = (
        f"[Coordinator audit assignment {assignment_id}] HUMAN-ATTENDED "
        "read-only milestone-outcome audit (#885/#886), run v"
        f"{_next_run_number}. When done, run `coord "
        f"report-result --assignment {assignment_id} --status done "
        "--summary \"<bottom-line>\" --body-file <scorecard.md> --audit-json "
        "<verdict.json>` so the scorecard AND the structured verdict post "
        "together and this session's row closes.\n\n"
    )
    effective_briefing = _issue_ctx + report_reminder + audit_briefing

    spec = _AssignmentSpecAu(
        repo_name=repo,
        repo_path=audit_repo_path,
        issue_number=epic_num,
        issue_title=f"[audit] {epic_title}",
        briefing=effective_briefing,
        model=resolved_model,
        type="audit",
        provider="claude-pty",
    )
    # READ-ONLY: no Edit/Write — the audit measures, it never fixes.
    argv = provider.build_command(
        spec,
        resolved_model=resolved_model,
        system_prompt=INTERACTIVE_AUDIT_SYSTEM_PROMPT,
        allowed_tools="Read,Bash,Grep,Glob",
    )

    click.echo(
        f"{machine} (local TTY) → AUDIT of epic #{epic_num}: {epic_title}"
    )
    click.echo("  mode: HUMAN-ATTENDED read-only milestone-outcome audit (#885)")
    click.echo(f"  assignment id: {assignment_id}  (audit_of={epic_num})")
    click.echo(f"  milestone: {milestone_title}")
    click.echo(
        f"  cwd: {audit_repo_path} (live checkout — read-only, no worktree)"
    )
    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        return

    audit_assignment = _AssignmentAu(
        machine_name=machine,
        repo_name=repo,
        issue_number=epic_num,
        issue_title=f"[audit] {epic_title}",
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=_time.time(),
        type="audit",
        model=resolved_model,
        provider_name="claude-pty",
    )
    _record_au(assignment=audit_assignment, repo_github=repo_cfg.github)
    _write_board_au(_read_board_au())
    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

    started_at = _time.time()
    exit_code = launch_human_attended_interactive(
        argv,
        effective_briefing,
        assignment_id=assignment_id,
        cwd=audit_repo_path,
    )
    if exit_code != 0:
        click.echo(f"  claude exited with status {exit_code}", err=True)

    _sname = _tmux_name(assignment_id) if _tmux_avail() else None
    if _sname and _tmux_alive(_sname):
        click.echo(
            f"  session still running in tmux: {_sname}\n"
            f"  reattach with:  coord reattach {assignment_id}"
        )
        sys.exit(0)

    # worktree_path=None: read-only, live checkout — never push/remove it.
    try:
        finalize_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=epic_num,
            machine_name=machine,
            worktree_path=None,
            base_branch=audit_default_branch,
            exit_code=exit_code,
            started_at=started_at,
            log_path=None,
            repo_path=None,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: backstop failed to record audit exit: {exc}",
            err=True,
        )
    return


def _dispatch_milestone_chat_of(
    *,
    machine: str,
    repo: str,
    model: str | None,
    dry_run: bool,
    milestone_chat_of: str,
    add_child: str | None,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    provider: object,
    _is_local: bool,
) -> None:
    """#1029: human-attended, genuine tmux-attached interactive milestone-chat
    session — replaces the headless `claude -p` / SSE-overlay mechanism
    (`coord/milestone_chat.py::dispatch_milestone_chat`, still used by the
    remote/headless `coord milestone chat` CLI path and by
    `dispatch_new_milestone_chat`'s `--new` flow, which stays out of scope).

    Mirrors `_dispatch_audit_of`'s shape: the target (`milestone_chat_of`) is
    a GitHub issue number — the milestone's tracking issue — not a board
    work-assignment id, so there is no board lookup. The milestone/issue
    resolution and seed-briefing content are shared with the headless path
    via `coord.milestone_chat.resolve_milestone_chat_briefing` so the two
    dispatch mechanisms can never drift apart on what the agent is told.

    `--system-prompt`/`--allowedTools` are deliberately NOT passed to
    `provider.build_command` — `ClaudePtyProvider.build_command` already has
    a `spec.type == "milestone-chat"` branch (added for the headless path)
    that supplies `MILESTONE_CHAT_SYSTEM_PROMPT` +
    `build_deny_prompt(MILESTONE_CHAT_DENY_COMMANDS)` and `Read,Bash` tools
    automatically from `spec.type` alone.
    """
    import tempfile as _tempfile  # noqa: PLC0415
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        finalize_interactive_exit,
        launch_human_attended_interactive,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )
    from coord.milestone_chat import resolve_milestone_chat_briefing  # noqa: PLC0415
    from coord.board_service import read_board as _read_board_mc  # noqa: PLC0415
    from coord.board_service import write_board as _write_board_mc  # noqa: PLC0415
    from coord.models import Assignment as _AssignmentMc  # noqa: PLC0415
    from coord.state import record_dispatched_assignment as _record_mc  # noqa: PLC0415
    from coord.agent import AssignmentSpec as _AssignmentSpecMc  # noqa: PLC0415

    if not _is_local:
        click.echo(
            "error: --milestone-chat-of is local-only for now; run it on the "
            "machine that holds the checkout.",
            err=True,
        )
        sys.exit(2)

    try:
        tracking_issue = int(milestone_chat_of)
    except ValueError:
        click.echo(
            f"error: --milestone-chat-of {milestone_chat_of!r} must be a GitHub "
            "issue number (the milestone's tracking issue).",
            err=True,
        )
        sys.exit(2)

    add_child_issue: int | None = None
    if add_child is not None:
        try:
            add_child_issue = int(add_child)
        except ValueError:
            click.echo(
                f"error: --add-child {add_child!r} must be a GitHub issue number.",
                err=True,
            )
            sys.exit(2)

    try:
        ctx = resolve_milestone_chat_briefing(
            repo, tracking_issue, cfg, add_child_issue=add_child_issue,
        )
    except RuntimeError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    mc_repo_path = str(
        Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
    )
    mc_default_branch = repo_cfg.default_branch or "main"
    resolved_model = model if model else cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]

    # Write the full briefing to a temp file and pre-fill only a SHORT
    # single-line seed pointing at it — mirrors `_run_troubleshoot_or_chat`'s
    # rationale: a multi-KB multi-line paste over the embedded-terminal /
    # tmux path is less reliable than a short one, and this degrades
    # gracefully (the operator can open the file by hand if the paste misses).
    _mc_brief_path = str(
        Path(_tempfile.gettempdir()) / f"coord-milestone-chat-{tracking_issue}.md"
    )
    Path(_mc_brief_path).write_text(ctx.briefing, encoding="utf-8")
    if add_child_issue is not None:
        seed_prompt = (
            f"Milestone chat for {repo} #{tracking_issue} ({ctx.milestone_title}): "
            f"read the full context at {_mc_brief_path} — it includes candidate "
            f"sub-issue #{add_child_issue}. Discuss whether it belongs under this "
            "epic and, once I confirm, run the `coord milestone add-child` splice "
            "described there."
        )
    else:
        seed_prompt = (
            f"Milestone chat for {repo} #{tracking_issue} ({ctx.milestone_title}): "
            f"read the full context at {_mc_brief_path} (tracking issue body, open "
            "issues under the milestone, current work order) and let's talk it "
            "through. Once I confirm a work order / edit / assignment, write it "
            "with the exact `coord milestone ...` command described there."
        )

    spec = _AssignmentSpecMc(
        repo_name=repo,
        repo_path=mc_repo_path,
        issue_number=tracking_issue,
        issue_title=f"[milestone-chat] {ctx.tracking_title}",
        briefing=ctx.briefing,
        model=resolved_model,
        type="milestone-chat",
        provider="claude-pty",
    )
    # No explicit system_prompt/allowed_tools: ClaudePtyProvider.build_command's
    # spec.type == "milestone-chat" branch supplies MILESTONE_CHAT_SYSTEM_PROMPT
    # + the deny-list + Read,Bash automatically.
    argv = provider.build_command(spec, resolved_model=resolved_model)

    click.echo(
        f"{machine} (local TTY) → MILESTONE CHAT #{tracking_issue}: "
        f"{ctx.milestone_title}"
    )
    click.echo(
        "  mode: HUMAN-ATTENDED interactive milestone chat "
        "(live checkout, no claim, no worktree) (#1029)"
    )
    click.echo(f"  assignment id: {assignment_id}  (milestone_chat_of={tracking_issue})")
    if add_child_issue is not None:
        click.echo(f"  add-child candidate: #{add_child_issue}")
    click.echo(f"  cwd: {mc_repo_path} (live checkout — read-only, no worktree)")
    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        return

    mc_assignment = _AssignmentMc(
        machine_name=machine,
        repo_name=repo,
        issue_number=tracking_issue,
        issue_title=f"[milestone-chat] {ctx.tracking_title}",
        briefing=ctx.briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=_time.time(),
        type="milestone-chat",
        model=resolved_model,
        provider_name="claude-pty",
    )
    _record_mc(assignment=mc_assignment, repo_github=repo_cfg.github)
    _write_board_mc(_read_board_mc())
    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

    started_at = _time.time()
    exit_code = launch_human_attended_interactive(
        argv,
        seed_prompt,
        assignment_id=assignment_id,
        cwd=mc_repo_path,
    )
    if exit_code != 0:
        click.echo(f"  claude exited with status {exit_code}", err=True)

    _sname = _tmux_name(assignment_id) if _tmux_avail() else None
    if _sname and _tmux_alive(_sname):
        click.echo(
            f"  session still running in tmux: {_sname}\n"
            f"  reattach with:  coord reattach {assignment_id}"
        )
        sys.exit(0)

    # worktree_path=None: read-only, live checkout — never push/remove it.
    try:
        finalize_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=tracking_issue,
            machine_name=machine,
            worktree_path=None,
            base_branch=mc_default_branch,
            exit_code=exit_code,
            started_at=started_at,
            log_path=None,
            repo_path=None,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: backstop failed to record milestone-chat exit: {exc}",
            err=True,
        )
    return


def _run_troubleshoot_or_chat(
    is_chat: bool,
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    briefing_file: str | None,
    model: str | None,
    dry_run: bool,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _issue_ctx: str,
    _ctx_write_hint: str,
) -> None:
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        finalize_interactive_exit,
        launch_human_attended_interactive,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )

    from coord.board_service import read_board as _read_board_ts  # noqa: PLC0415
    from coord.board_service import write_board as _write_board_ts  # noqa: PLC0415
    from coord.models import Assignment as _AssignmentTs  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        record_dispatched_assignment as _record_ts,
    )
    from coord.agent import AssignmentSpec as _AssignmentSpecTs  # noqa: PLC0415

    # #628: --chat shares troubleshoot's shape (human-attended, live
    # checkout, no claim/worktree, briefed-from-file, finalize
    # worktree_path=None). It differs only in the system prompt/seed
    # (general Q&A + may EDIT the issue via coord) and type=chat.
    _is_chat = is_chat
    _mode = "chat" if _is_chat else "troubleshoot"
    if not _is_chat and not (briefing or "").strip():
        click.echo(
            "error: --troubleshoot requires a briefing "
            "(--briefing or --briefing-file).",
            err=True,
        )
        sys.exit(2)
    if _is_chat and not (briefing or "").strip():
        # The TUI seeds a rich briefing; a bare CLI --chat gets a minimal
        # one so the session still knows what issue it's about.
        briefing = (
            f"Chat about {repo} #{issue}. Gather any context you need with "
            f"`gh issue view {issue} --comments` and `coord` reads."
        )
    if not _is_local:
        click.echo(f"error: --{_mode} is local-only for now.", err=True)
        sys.exit(2)

    ts_repo_path = str(
        Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
    )
    ts_default_branch = repo_cfg.default_branch or "main"
    resolved_model = model if model else cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]

    if _is_chat:
        _ts_system_prompt = (
            "You are a coordinator assistant in a HUMAN-ATTENDED 'chat "
            "about this issue' session. Help the operator think it "
            "through: answer open questions (is it still needed? what "
            "milestone? scope?), sketch designs/UX, and diagnose a stalled "
            "pipeline item. You MAY update the issue itself with `coord "
            "issue edit <repo> <issue> --title/--body-file` and send it to "
            "the Pending stage with `coord ready <repo> <issue>` — confirm "
            "with the operator before each write. Do NOT edit files in this "
            "live checkout, do NOT commit, and do NOT run `gh` to mutate — "
            "go through `coord` so the tracker stays behind the seam. "
            "Type /update-issue at any point to synthesize what we agreed "
            "and write it back to the issue body. "
            "CRITICAL — this session is diagnostic-only: do NOT run "
            "`coord report-result --status done` or `--status blocked` "
            "(you have no committed work to back a success claim, and doing "
            "so leaves a false-done box on the pipeline). "
            "For a stalled item, start with `coord diagnose <repo> <issue>` "
            "— it auto-recovers phantoms, orphaned worktrees, and dropped "
            "findings; confirm any --reset with the operator first."
        )
        ts_reminder = (
            f"[Coordinator chat assignment {assignment_id}] HUMAN-ATTENDED "
            "chat about this issue. You may edit the issue (`coord issue "
            "edit`) and send it to Pending (`coord ready`) — confirm first. "
            "Type /update-issue to synthesize what we agreed and write it "
            "back to the issue body. "
            "You are in the LIVE checkout: do NOT modify files or commit "
            "here (it is the editable coordinator + worker-worktree base); "
            "for a code change, surface a plan so the operator can dispatch "
            "Work. "
            "IMPORTANT: do NOT run `coord report-result --status done` — "
            "this session has no committed work and claiming done would "
            "leave a false-good-to-go box on the pipeline (#676).\n\n"
        )
    else:
        _ts_system_prompt = (
            "You are a coordinator troubleshooter in a HUMAN-ATTENDED "
            "session. You are READ-ONLY in a live checkout: do NOT modify "
            "files, do NOT commit, do NOT run `gh`. Investigate the stalled "
            "pipeline item using coord, git, and sqlite3 reads; explain "
            "what is wrong and what will unstick it; and surface any plan "
            "for the operator to approve before mutating anything. "
            "Start with `coord diagnose <repo> <issue>` — it auto-recovers "
            "phantoms, orphaned worktrees, and dropped findings; confirm "
            "any --reset with the operator first. "
            "CRITICAL: do NOT run `coord report-result --status done` or "
            "`--status blocked` — this session is diagnostic-only with no "
            "committed work, and claiming done leaves a false status on the "
            "pipeline (#676)."
        )
        ts_reminder = (
            f"[Coordinator troubleshoot assignment {assignment_id}] "
            "HUMAN-ATTENDED, READ-ONLY diagnostic for a stalled pipeline "
            "item. You are in the LIVE checkout — do NOT modify files here "
            "(it is the editable coordinator and the worker-worktree base). "
            "If a code fix is needed, surface the plan so the operator can "
            "dispatch a proper Fix. "
            "IMPORTANT: do NOT run `coord report-result --status done` — "
            "this session has no committed work (#676).\n\n"
        )
    effective_briefing = _issue_ctx + ts_reminder + briefing + _ctx_write_hint

    # Pre-fill a SHORT, single-line prompt that points the session at
    # the full diagnostic on disk, rather than pasting the whole
    # multi-line briefing into the input box.  A short paste lands
    # reliably; a multi-KB multi-line paste over the embedded-terminal /
    # nested-tmux path often is dropped by the readiness poll
    # (interactive._inject_briefing_into_tmux_session is best-effort).
    # And it degrades gracefully — if the paste is missed, the operator
    # can type the one short line by hand instead of being stranded with
    # no context.  The full briefing still lives in the file and on the
    # assignment row.
    if briefing_file:
        _ts_brief_path = str(Path(briefing_file).expanduser())
    else:
        import tempfile as _tempfile  # noqa: PLC0415

        _ts_brief_path = str(
            Path(_tempfile.gettempdir()) / f"coord-{_mode}-{issue}.md"
        )
        Path(_ts_brief_path).write_text(effective_briefing, encoding="utf-8")
    if _is_chat:
        seed_prompt = (
            f"Chat about {repo} #{issue}: read the full context at "
            f"{_ts_brief_path} (the issue, comments, and board state), then "
            "let's talk it through. Ask me what I want — questions about "
            "scope/milestone/whether it's still needed, a UX sketch, or "
            "diagnosing a stall. You can update the issue with `coord issue "
            "edit` and send it to Pending with `coord ready` once we've "
            "settled it — just confirm with me first."
        )
    else:
        seed_prompt = (
            f"Troubleshoot {repo} #{issue}: read the diagnostic briefing at "
            f"{_ts_brief_path} (board state, assignments, merge-queue, CI, "
            "and a playbook of likely causes), then tell me what's wrong and "
            "the options to unstick it. You are read-only — do not modify "
            "files, commit, or run gh; surface any fix plan for me to "
            "approve."
        )

    spec = _AssignmentSpecTs(
        repo_name=repo,
        repo_path=ts_repo_path,
        issue_number=issue,
        issue_title=f"[{_mode}] {issue_title}",
        briefing=effective_briefing,
        model=resolved_model,
        type=_mode,
        provider="claude-pty",
    )
    # No Edit/Write — the live checkout must not be mutated. Bash carries
    # coord/git/sqlite3 reads (and, for chat, the `coord issue edit` /
    # `coord ready` ISSUE writes); Read/Grep/Glob for inspection.
    argv = provider.build_command(
        spec,
        resolved_model=resolved_model,
        system_prompt=_ts_system_prompt,
        allowed_tools="Read,Bash,Grep,Glob",
    )

    click.echo(
        f"{machine} (local TTY) → {_mode.upper()} #{issue}: {issue_title}"
    )
    click.echo(
        "  mode: HUMAN-ATTENDED interactive "
        + ("chat about the issue " if _is_chat else "diagnostic ")
        + "(live checkout, no claim, no worktree) "
        + ("(#628)" if _is_chat else "(#569)")
    )
    click.echo(f"  assignment id: {assignment_id}")
    click.echo(f"  cwd: {ts_repo_path} (live checkout — read-only)")
    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        return

    ts_assignment = _AssignmentTs(
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=f"[{_mode}] {issue_title}",
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=_time.time(),
        type=_mode,
        model=resolved_model,
        provider_name="claude-pty",
    )
    _record_ts(assignment=ts_assignment, repo_github=repo_cfg.github)
    # #749: previously unconditional local build_board/save_board — this
    # function (unlike the --x-of variants) never had thin-client awareness,
    # so it silently wrote board_meta to a non-canonical local DB.
    _write_board_ts(_read_board_ts())
    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

    started_at = _time.time()
    # Pre-fill the SHORT seed prompt (not the full briefing) — see the
    # seed_prompt rationale above.
    exit_code = launch_human_attended_interactive(
        argv,
        seed_prompt,
        assignment_id=assignment_id,
        cwd=ts_repo_path,
    )
    if exit_code != 0:
        click.echo(f"  claude exited with status {exit_code}", err=True)

    _sname = _tmux_name(assignment_id) if _tmux_avail() else None
    if _sname and _tmux_alive(_sname):
        click.echo(
            f"  session still running in tmux: {_sname}\n"
            f"  reattach with:  coord reattach {assignment_id}"
        )
        sys.exit(0)

    # finalize with worktree_path=None — read-only, never push/remove the
    # live checkout; the git-floor backstop just records a terminal row.
    try:
        finalize_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=issue,
            machine_name=machine,
            worktree_path=None,
            base_branch=ts_default_branch,
            exit_code=exit_code,
            started_at=started_at,
            log_path=None,
            repo_path=None,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: backstop failed to record troubleshoot exit: {exc}",
            err=True,
        )
    return


def _dispatch_troubleshoot(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    briefing_file: str | None,
    model: str | None,
    dry_run: bool,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _issue_ctx: str,
    _ctx_write_hint: str,
) -> None:
    """#569: human-attended, READ-ONLY diagnostic interactive session."""
    _run_troubleshoot_or_chat(
        is_chat=False,
        machine=machine,
        repo=repo,
        issue=issue,
        briefing=briefing,
        briefing_file=briefing_file,
        model=model,
        dry_run=dry_run,
        cfg=cfg,
        machine_obj=machine_obj,
        repo_cfg=repo_cfg,
        issue_title=issue_title,
        provider=provider,
        _is_local=_is_local,
        _issue_ctx=_issue_ctx,
        _ctx_write_hint=_ctx_write_hint,
    )


def _dispatch_chat(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    briefing_file: str | None,
    model: str | None,
    dry_run: bool,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _issue_ctx: str,
    _ctx_write_hint: str,
) -> None:
    """#628: human-attended 'Chat about issue' interactive session."""
    _run_troubleshoot_or_chat(
        is_chat=True,
        machine=machine,
        repo=repo,
        issue=issue,
        briefing=briefing,
        briefing_file=briefing_file,
        model=model,
        dry_run=dry_run,
        cfg=cfg,
        machine_obj=machine_obj,
        repo_cfg=repo_cfg,
        issue_title=issue_title,
        provider=provider,
        _is_local=_is_local,
        _issue_ctx=_issue_ctx,
        _ctx_write_hint=_ctx_write_hint,
    )


def _dispatch_fix_of(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    model: str | None,
    dry_run: bool,
    force: bool,
    fix_of: str,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _svc: object,
    _interactive_board: object,
    _issue_ctx: str,
    _ctx_write_hint: str,
) -> None:
    """Leg 3 (#517): human-attended FIX for a review whose verdict was request-changes (also accepts a failed-test work id, #581)."""
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        _holder_is_base_checkout as _holder_is_base,
        _launch_via_tmux as _tmux_launch,
        _remote_base_checkout_free_branch as _remote_free_base,
        _remote_orphan_is_safe_to_prune as _remote_orphan_safe,
        _remote_worktree_remove as _remote_wt_remove,
        find_remote_branch_holder as _find_branch_holder,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
        launch_human_attended_interactive,
        remote_worktree_exists as _remote_wt_exists,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )

    from coord.auto_loop import (  # noqa: PLC0415
        _build_fix_briefing,
        _fix_model_for_iteration,
        _load_review_findings,
    )
    from coord.agent import (  # noqa: PLC0415
        AssignmentSpec as _AssignmentSpecFx,
        _GitError as _AgentGitErrorFx,
        narrow_artifact_paths as _narrow_ap_fx,
        setup_interactive_worktree as _setup_wt_fx,
    )
    from coord.models import Assignment as _AssignmentFx  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        COORD_DIR as _COORD_DIR_FX,
        build_board as _build_board_fx,
        record_dispatched_assignment as _record_fx,
        save_board as _save_board_fx,
        set_assignment_failure_reason as _set_fail_reason_fx,
    )

    # Track B / #486: a fix runs on the LOCAL TTY or on a REMOTE
    # machine over ssh+tmux.  Unlike review, a fix WRITES — it needs a
    # worktree on the EXISTING branch and its commits must be pushed
    # back to origin (the #486d push-back, via
    # finalize_remote_interactive_exit; the local finalize only sees
    # the local filesystem).
    _fx_board = _interactive_board(_build_board_fx)
    review = _fx_board.find_by_id(fix_of)
    if review is None:
        click.echo(
            f"error: --fix-of {fix_of}: no such assignment on the board.",
            err=True,
        )
        sys.exit(2)
    # Two accepted shapes for --fix-of (#581):
    #   (a) a REVIEW assignment whose verdict was request-changes — the
    #       original leg-3a path; work = review.review_of_assignment_id,
    #       findings = the reviewer's findings.
    #   (b) a WORK assignment whose Test gate FAILED — the test-fail fix
    #       front door; work = the target itself, findings = the recorded
    #       test-failure story (test_reason).
    _fix_from_test_fail = (
        review.type != "review"
        and (getattr(review, "test_state", None) == "failed")
    )
    if review.type != "review" and not _fix_from_test_fail:
        click.echo(
            f"error: --fix-of {fix_of} is type={review.type!r} with "
            f"test_state={getattr(review, 'test_state', None)!r}. Pass "
            "either a REVIEW id whose verdict was request-changes, or a "
            "WORK id whose Test gate failed.",
            err=True,
        )
        sys.exit(2)
    if _fix_from_test_fail:
        work = review  # the failed work row IS the thing to fix
    else:
        work = (
            _fx_board.find_by_id(review.review_of_assignment_id)
            if review.review_of_assignment_id
            else None
        )
    if work is None:
        click.echo(
            f"error: review {fix_of} has no linked work assignment "
            "(review_of_assignment_id is unset).",
            err=True,
        )
        sys.exit(2)
    if not work.branch:
        click.echo(
            f"error: work assignment {work.assignment_id} has no branch "
            "to fix.",
            err=True,
        )
        sys.exit(2)

    # Iteration accounting mirrors the auto-loop fix path so the merge
    # gate and the next review see an identical work→fix→review chain.
    next_iteration = (work.review_iteration or 0) + 1
    max_iter = cfg.pipeline.max_review_iterations
    if next_iteration > max_iter:
        if force:
            # #855-follow-up: intractable stories (e.g. vimcode#515) legitimately
            # need more than the cap.  --force is the operator's explicit "I know,
            # keep going" override so a hard stop isn't the only exit.
            click.echo(
                f"warning: max_review_iterations ({max_iter}) reached for "
                f"work {work.assignment_id}; dispatching iteration "
                f"{next_iteration} anyway (--force).",
                err=True,
            )
        else:
            click.echo(
                f"error: max_review_iterations ({max_iter}) reached for "
                f"work {work.assignment_id}; not dispatching another fix. "
                "Re-run with --force to override for this dispatch, or bump "
                "pipeline.max_review_iterations in coordinator.yml.",
                err=True,
            )
            sys.exit(2)

    # Findings: reuse the SAME loader the claude -p fix path uses (DB
    # cache → log → agent).  Local-only ⇒ no machine_host.  Fall back to
    # a pointer-to-the-review brief when nothing structured was captured
    # (interactive reviews may report only a one-line verdict summary).
    if _fix_from_test_fail:
        # The findings ARE the recorded test-failure story (#581).  No
        # reviewer log to consult — the operator's `coord test --fail
        # --reason` text is what the fix worker needs.
        # #1337: the board wire carries only a bounded PREVIEW of test_reason;
        # the briefing quotes it verbatim, so read the full text through the
        # detail endpoint (falls back to the board-carried value).
        from coord.state import load_assignment_test_reason as _load_tr  # noqa: PLC0415

        _test_story = (
            _load_tr(work.assignment_id or "")
            or getattr(work, "test_reason", None)
            or ""
        ).strip()
        _findings_body = (
            "The manual smoke test FAILED. The operator reported:\n\n"
            f"> {_test_story}\n\n"
            "Reproduce the failure, fix the root cause, and re-validate "
            "before pushing."
            if _test_story
            else (
                "The manual smoke test FAILED (no reason text was "
                "recorded). Pull the branch, reproduce the failure the "
                "operator hit, and fix the root cause before pushing."
            )
        )
    else:
        _fx_log = _COORD_DIR_FX / "logs" / f"{fix_of}.log"
        _fx_log_path = str(_fx_log) if _fx_log.exists() else None
        try:
            findings = _load_review_findings(
                review, _fx_log_path, None, repo_github=repo_cfg.github,
            )
        except Exception:  # noqa: BLE001 — best-effort; fall back below
            findings = None
        if findings is not None and (getattr(findings, "body", "") or "").strip():
            _findings_body = findings.body.strip()
        else:
            _findings_body = (
                f"(No structured findings were captured for review {fix_of}.) "
                f"The review verdict was {review.review_verdict or 'request-changes'!r}. "
                "Read the reviewer's feedback on the PR / issue and address "
                "every blocking item before pushing."
            )
    from types import SimpleNamespace as _SNS  # noqa: PLC0415
    fix_briefing = _build_fix_briefing(
        work, _SNS(body=_findings_body), next_iteration, max_iter,
    )

    # #803: escalate the model per fix iteration, mirroring the headless
    # auto-loop path.  Explicit --model always wins; when omitted,
    # _fix_model_for_iteration returns the appropriate tier (or None when
    # pipeline.escalate_fix_model=False), falling back to cfg.models.default.
    resolved_model = (
        model
        or _fix_model_for_iteration(cfg, next_iteration)
        or cfg.models.default
    )
    assignment_id = _uuid.uuid4().hex[:12]
    if _is_local:
        fix_repo_path = str(
            Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
        )
    else:
        fix_repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"
    fix_default_branch = repo_cfg.default_branch or "main"

    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
    if _is_local:
        report_reminder = (
            f"[Coordinator fix assignment {assignment_id}] HUMAN-ATTENDED "
            f"fix iteration {next_iteration}/{max_iter} on branch "
            f"{work.branch}. Before you exit, run `coord report-result "
            f"--assignment {assignment_id} --status done --summary <text>` "
            "so the coordinator records the result and can re-review.\n\n"
        )
    else:
        report_reminder = (
            f"[Coordinator fix assignment {assignment_id}] HUMAN-ATTENDED "
            f"fix iteration {next_iteration}/{max_iter} on branch "
            f"{work.branch}, running on a REMOTE machine. Make your "
            "changes and COMMIT them. Before you exit, run `coord "
            f"report-result --assignment {assignment_id} --status done "
            "--summary <text>` — since #590 it routes to the "
            "coordinator's shared board, so the result is recorded from "
            "here. Do NOT run any `gh` commands. When you exit, the "
            f"coordinator also pushes your commits to origin/{work.branch} "
            "and re-reviews.\n\n"
        )
    effective_briefing = _issue_ctx + report_reminder + fix_briefing + _ctx_write_hint

    spec = _AssignmentSpecFx(
        repo_name=repo,
        repo_path=fix_repo_path,
        issue_number=issue,
        issue_title=f"[fix-{next_iteration}] {issue_title}",
        briefing=effective_briefing,
        model=resolved_model,
        type="work",
        provider="claude-pty",
    )
    # type="work" ⇒ default worker tool set (Read/Edit/Write/Bash): the
    # fix session must be able to mutate the checkout, unlike --review-of.
    argv = provider.build_command(spec, resolved_model=resolved_model)
    # Remote: bare "claude" isn't on the SSH login PATH (#424/#425).
    if not _is_local:
        argv = ["~/.local/bin/claude"] + list(argv)[1:]

    _fx_location = (
        "local TTY" if _is_local
        else f"{machine_obj.host} (remote tmux)"
    )
    click.echo(
        f"{machine} ({_fx_location}) → FIX of #{issue} "
        f"(iteration {next_iteration}/{max_iter}) on branch {work.branch}"
    )
    click.echo(
        "  mode: HUMAN-ATTENDED interactive fix "
        "(migration leg 3 / Track B #486)"
    )
    # #803: show the escalated model so an opus escalation is visible.
    _explicit = "(explicit --model override)" if model else "(auto-selected)"
    click.echo(f"  model: {resolved_model} {_explicit}")
    click.echo(
        f"  assignment id: {assignment_id}  (fix_of={fix_of}, "
        f"work={work.assignment_id})"
    )
    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would continue branch: {work.branch}")
        if not _is_local:
            click.echo(
                f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
                f" on {machine_obj.host} (branch: {work.branch})"
            )
        click.echo(f"  would exec: {argv}")
        return

    fix_assignment = _AssignmentFx(
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=f"[fix-{next_iteration}] {issue_title}",
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        branch=work.branch,
        pr_url=work.pr_url,
        dispatched_at=_time.time(),
        type="work",
        review_of_assignment_id=work.assignment_id,
        review_iteration=next_iteration,
        model=resolved_model,
        provider_name="claude-pty",
    )
    _record_fx(assignment=fix_assignment, repo_github=repo_cfg.github)
    if _svc is None:
        _save_board_fx(_build_board_fx())

    if _is_local:
        try:
            _wt_path, _ = _setup_wt_fx(
                Path(fix_repo_path),
                issue_number=issue,
                issue_title=issue_title,
                assignment_id=assignment_id,
                default_branch=fix_default_branch,
                existing_branch=work.branch,
            )
            worktree_path = str(_wt_path)
        except (_AgentGitErrorFx, OSError) as _wt_err:
            _reason_fx = (
                f"worktree-add failed for branch {work.branch}: {_wt_err}"
            )
            click.echo(f"  error: {_reason_fx}", err=True)
            # #618: persist reason + mark terminal immediately so the
            # TUI shows WHY the box is red (no log file on this path).
            _set_fail_reason_fx(assignment_id, _reason_fx)
            sys.exit(1)
        click.echo(f"  worktree: {worktree_path} (branch: {work.branch})")

        started_at = _time.time()
        exit_code = launch_human_attended_interactive(
            argv,
            effective_briefing,
            assignment_id=assignment_id,
            cwd=worktree_path,
        )
        if exit_code != 0:
            click.echo(f"  claude exited with status {exit_code}", err=True)

        _sname = _tmux_name(assignment_id) if _tmux_avail() else None
        if _sname and _tmux_alive(_sname):
            click.echo(
                f"  session still running in tmux: {_sname}\n"
                f"  reattach with:  coord reattach {assignment_id}"
            )
            sys.exit(0)

        # #982: narrow the stash to only the examples named in the work
        # assignment's smoke tests (if any).  Falls back to the repo-wide
        # glob when no smoke tests were captured or nothing matched.
        _aps_fx = _narrow_ap_fx(
            list(repo_cfg.artifact_paths),
            getattr(work, "smoke_tests", None),
        )
        try:
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                worktree_path=worktree_path,
                base_branch=fix_default_branch,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=fix_repo_path,
                artifact_paths=_aps_fx,
                branch=work.branch,
            )
            if finalize_result.already_recorded:
                click.echo(
                    "  result recorded via `coord report-result`; backstop "
                    "did not overwrite"
                )
            else:
                click.echo(
                    f"  backstop: status={finalize_result.terminal_status} "
                    f"commits_ahead={finalize_result.commits_ahead}"
                )
                if not finalize_result.push_ok:
                    click.echo(
                        f"  warning: git push failed: {finalize_result.push_error}",
                        err=True,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: backstop failed to record fix exit: {exc}",
                err=True,
            )
        sys.exit(exit_code)

    # ── REMOTE FIX (Track B / #486) ───────────────────────────────
    # A remote worktree on the EXISTING branch (`-B <branch>
    # origin/<branch>` resets a dedicated local branch to the reviewed
    # work's branch); the session commits there; on exit the
    # coordinator pushes the commits back to origin + records the
    # completion (#486d) so the re-review fires.
    import shlex as _shlex_fx  # noqa: PLC0415

    _remote_wt = "$HOME/.coord/worktrees/" + assignment_id
    _rp_sh = (
        "$HOME/" + fix_repo_path[2:]
        if fix_repo_path.startswith("~/")
        else ("$HOME" if fix_repo_path == "~" else fix_repo_path)
    )
    _claude_args = _shlex_fx.join(list(argv)[1:])
    _br_q = _shlex_fx.quote(work.branch)
    _orig_ref = _shlex_fx.quote(f"origin/{work.branch}")
    _remote_cmd = (
        f"mkdir -p $HOME/.coord/worktrees"
        f" && cd {_rp_sh}"
        f" && git fetch origin --prune 2>/dev/null || true"
        f" && git worktree prune 2>/dev/null || true"
        f" && git worktree add -B {_br_q} {_remote_wt} {_orig_ref}"
        f" && cd {_remote_wt}"
        f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {_claude_args}"
    )
    _tmux_host = TmuxHost(ssh_target=machine_obj.host)
    _sname = _tmux_name(assignment_id)
    click.echo(
        f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
        f" on {machine_obj.host} (branch: {work.branch})"
    )

    if effective_briefing.strip():
        _hdr = (
            "--- seeded briefing -- review below; "
            "submit the pre-filled input in Claude to send ---"
        )
        _ftr = "-" * len(_hdr)
        _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
        try:
            os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
        except OSError:
            pass

    started_at = _time.time()
    _rc = _tmux_launch(
        argv,
        effective_briefing,
        _sname,
        cwd=None,
        host=_tmux_host,
        raw_shell_cmd=_remote_cmd,
    )
    if _rc is None:
        click.echo(
            "  error: could not create remote tmux session on "
            f"{machine_obj.host}",
            err=True,
        )
        sys.exit(1)
    exit_code = _rc

    if _tmux_alive(_sname, host=_tmux_host):
        click.echo(
            f"  session still running in remote tmux: {_sname}\n"
            f"  reattach with:  ssh -t {machine_obj.host}"
            f" tmux attach-session -t {_sname}\n"
            "  (the fix is not pushed until the session ends and the "
            "coordinator finalizes)"
        )
        sys.exit(0)

    # ── #560: detect setup failures (worktree never created) ──────
    # If the session exited non-zero AND the remote worktree directory
    # was never created, the failure is a setup error — most commonly
    # git worktree add refused because the branch is already checked
    # out in another worktree (a leftover interactive session or
    # orphaned non-interactive worktree).  Print an actionable message
    # and skip the misleading "commits preserved" noise.  The finalize
    # call below still records the failure on the board so the pipeline
    # shows "failed" rather than leaving the row as "running".
    _wt_setup_ok: bool = True
    if exit_code != 0:
        if not _remote_wt_exists(machine_obj.host, _remote_wt):
            _wt_setup_ok = False
            _holder = _find_branch_holder(
                machine_obj.host, _rp_sh, work.branch,
            )
            if _holder:
                _holder_aid = Path(_holder).name
                _holder_sname = f"coord-{_holder_aid}"
                _holder_live = _tmux_alive(_holder_sname, host=_tmux_host)
                _err_header = (
                    f"  error: setup failed — branch {work.branch!r} is "
                    f"already checked out at {_holder} on {machine_obj.host}."
                )
                if _holder_live:
                    click.echo("\n".join([
                        _err_header,
                        f"  active tmux session: {_holder_sname}",
                        f"  reattach:  coord reattach {_holder_aid}",
                        "  exit the session first, then retry this fix.",
                    ]), err=True)
                else:
                    # Dead holder — detect base checkout vs stale orphan (#814).
                    if _holder_is_base(_holder):
                        # #814: the base checkout ~/src/<repo> is on the
                        # issue branch.  NEVER prune the base (#561) —
                        # checkout the default branch to free the lock.
                        click.echo(
                            f"  base checkout {_holder!r} is on branch"
                            f" {work.branch!r} on {machine_obj.host}"
                            f" — checking out {fix_default_branch!r}"
                            f" to free it …"
                        )
                        _freed = _remote_free_base(
                            machine_obj.host, _rp_sh, fix_default_branch,
                        )
                        if _freed:
                            click.echo(
                                "  base checkout freed — retrying launch …"
                            )
                            _rc2 = _tmux_launch(
                                argv, effective_briefing, _sname,
                                cwd=None, host=_tmux_host,
                                raw_shell_cmd=_remote_cmd,
                            )
                            if _rc2 is None:
                                click.echo(
                                    f"  error: could not create remote tmux"
                                    f" session on {machine_obj.host}"
                                    f" (retry after base-checkout free)",
                                    err=True,
                                )
                            else:
                                exit_code = _rc2
                                if _tmux_alive(_sname, host=_tmux_host):
                                    click.echo(
                                        f"  session still running in remote"
                                        f" tmux: {_sname}\n"
                                        f"  reattach with:  ssh -t"
                                        f" {machine_obj.host}"
                                        f" tmux attach-session -t {_sname}\n"
                                        "  (the fix is not pushed until the"
                                        " session ends and the coordinator"
                                        " finalizes)"
                                    )
                                    sys.exit(0)
                                if (
                                    exit_code == 0
                                    or _remote_wt_exists(
                                        machine_obj.host, _remote_wt
                                    )
                                ):
                                    _wt_setup_ok = True  # retry succeeded
                        else:
                            click.echo("\n".join([
                                _err_header,
                                f"  base checkout is stuck on"
                                f" {work.branch!r} —",
                                f"  free it manually with:",
                                f"    ssh {machine_obj.host}"
                                f" 'git -C {_rp_sh} checkout"
                                f" {fix_default_branch}'",
                            ]), err=True)
                    else:
                        # Dead orphan — safety-gated auto-prune-and-retry (#759).
                        _auto_pruned = False
                        if _remote_orphan_safe(
                            machine_obj.host, _rp_sh, _holder, work.branch,
                        ):
                            click.echo(
                                f"  auto-pruning stale orphan {_holder}"
                                f" on {machine_obj.host} …"
                            )
                            _auto_pruned = _remote_wt_remove(
                                machine_obj.host, _rp_sh, _holder,
                            )
                        if _auto_pruned:
                            click.echo(
                                "  stale orphan auto-pruned — retrying launch …"
                            )
                            _rc2 = _tmux_launch(
                                argv, effective_briefing, _sname,
                                cwd=None, host=_tmux_host,
                                raw_shell_cmd=_remote_cmd,
                            )
                            if _rc2 is None:
                                click.echo(
                                    f"  error: could not create remote tmux"
                                    f" session on {machine_obj.host}"
                                    f" (retry after prune)",
                                    err=True,
                                )
                            else:
                                exit_code = _rc2
                                if _tmux_alive(_sname, host=_tmux_host):
                                    click.echo(
                                        f"  session still running in remote"
                                        f" tmux: {_sname}\n"
                                        f"  reattach with:  ssh -t"
                                        f" {machine_obj.host}"
                                        f" tmux attach-session -t {_sname}\n"
                                        "  (the fix is not pushed until the"
                                        " session ends and the coordinator"
                                        " finalizes)"
                                    )
                                    sys.exit(0)
                                if (
                                    exit_code == 0
                                    or _remote_wt_exists(
                                        machine_obj.host, _remote_wt
                                    )
                                ):
                                    _wt_setup_ok = True  # retry succeeded
                        else:
                            # Not safe or prune failed → manual command.
                            click.echo("\n".join([
                                _err_header,
                                "  stale worktree — prune it first:",
                                f"    ssh {machine_obj.host} 'cd {_rp_sh}"
                                f" && git worktree remove --force"
                                f" {_shlex_fx.quote(_holder)}'",
                            ]), err=True)
            else:
                click.echo(
                    f"  error: setup failed — the remote worktree was "
                    f"never created (git worktree add refused on "
                    f"{machine_obj.host}).",
                    err=True,
                )
        else:
            click.echo(
                f"  claude exited with status {exit_code}", err=True,
            )

    # Remote finalize (#486d): push the fix commits to origin/<branch>,
    # record the completion, and clean up the remote worktree.  Safe
    # even when the worktree was never created — _remote_push_and_count
    # detects the missing directory (exit 91) and marks push_ok=False.
    #
    # #982: narrow the stash to only the examples named in the work
    # assignment's smoke tests (if any).
    _aps_fx_remote = _narrow_ap_fx(
        list(repo_cfg.artifact_paths),
        getattr(work, "smoke_tests", None),
    )
    try:
        _fr = finalize_remote_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=issue,
            machine_name=machine,
            ssh_target=machine_obj.host,
            remote_worktree_sh=_remote_wt,
            remote_repo_sh=_rp_sh,
            branch=work.branch,
            base_branch=fix_default_branch,
            exit_code=exit_code,
            started_at=started_at,
            artifact_paths=_aps_fx_remote,
        )
        if _fr.already_recorded:
            click.echo(
                "  result recorded via `coord report-result`; remote "
                "backstop did not overwrite"
            )
        else:
            click.echo(
                f"  remote backstop: status={_fr.terminal_status} "
                f"commits_ahead={_fr.commits_ahead} pushed={_fr.push_ok}"
            )
            if not _fr.push_ok and _wt_setup_ok:
                # Worker ran but push failed — commits ARE preserved in
                # the worktree (which was successfully created).
                click.echo(
                    f"  warning: remote push failed: {_fr.push_error}",
                    err=True,
                )
                click.echo(
                    f"  fix commits preserved in {_remote_wt} on "
                    f"{machine_obj.host} (worktree NOT removed)",
                    err=True,
                )
            # else _wt_setup_ok is False: setup never happened; error
            # already printed above — no "commits preserved" noise.
        _echo_artifact_stash(_fr)
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: remote backstop failed to record fix exit: {exc}",
            err=True,
        )
    sys.exit(exit_code)


def _dispatch_rework_of(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    model: str | None,
    dry_run: bool,
    force: bool,
    rework_of: str,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _svc: object,
    _interactive_board: object,
    _issue_ctx: str,
) -> None:
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        _holder_is_base_checkout as _holder_is_base,
        _launch_via_tmux as _tmux_launch,
        _remote_base_checkout_free_branch as _remote_free_base,
        _remote_orphan_is_safe_to_prune as _remote_orphan_safe,
        _remote_worktree_remove as _remote_wt_remove,
        find_remote_branch_holder as _find_branch_holder,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
        launch_human_attended_interactive,
        remote_worktree_exists as _remote_wt_exists,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )

    from coord.agent import (  # noqa: PLC0415
        AssignmentSpec as _AssignmentSpecRw,
        _GitError as _AgentGitErrorRw,
        narrow_artifact_paths as _narrow_ap_rw,
        setup_interactive_worktree as _setup_wt_rw,
    )
    from coord.models import Assignment as _AssignmentRw  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        COORD_DIR as _COORD_DIR_RW,
        build_board as _build_board_rw,
        record_dispatched_assignment as _record_rw,
        save_board as _save_board_rw,
        set_assignment_failure_reason as _set_fail_reason_rw,
    )

    # Resolve branch: try to find a work assignment by ID first, then
    # fall back to treating the argument as a literal branch name.
    _rw_board = _interactive_board(_build_board_rw)
    _rw_work = _rw_board.find_by_id(rework_of)
    if _rw_work is not None:
        if not _rw_work.branch:
            click.echo(
                f"error: work assignment {rework_of} has no branch.",
                err=True,
            )
            sys.exit(2)
        rw_branch = _rw_work.branch
        next_rw_iteration = (_rw_work.review_iteration or 0) + 1
        rw_work_id: str | None = _rw_work.assignment_id
    else:
        # Treat the argument as a branch name — useful when the
        # original assignment has aged off the board.
        rw_branch = rework_of
        # Look for any completed work on that branch to inherit
        # the iteration counter; default to 1 if none found.
        _branch_work = next(
            (
                a for a in _rw_board.completed
                if a.branch == rw_branch and a.type in ("work", "plan")
            ),
            None,
        )
        next_rw_iteration = (
            (_branch_work.review_iteration or 0) + 1
            if _branch_work is not None
            else 1
        )
        rw_work_id = (
            _branch_work.assignment_id if _branch_work is not None else None
        )

    resolved_model = model if model else cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]
    if _is_local:
        rw_repo_path = str(
            Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
        )
    else:
        rw_repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"
    rw_default_branch = repo_cfg.default_branch or "main"

    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
    if _is_local:
        rw_report_reminder = (
            f"[Coordinator rework assignment {assignment_id}] "
            f"HUMAN-ATTENDED rework (iteration {next_rw_iteration}) "
            f"on branch {rw_branch}. Before you exit, run "
            f"`coord report-result --assignment {assignment_id} "
            "--status done --summary <text>` so the coordinator "
            "records the result and can re-review.\n\n"
        )
    else:
        rw_report_reminder = (
            f"[Coordinator rework assignment {assignment_id}] "
            f"HUMAN-ATTENDED rework (iteration {next_rw_iteration}) "
            f"on branch {rw_branch}, running on a REMOTE machine. "
            "Make your changes and COMMIT them. Before you exit, run "
            f"`coord report-result --assignment {assignment_id} "
            "--status done --summary <text>` — since #590 it routes to "
            "the coordinator's shared board, so the result is recorded "
            "from here. Do NOT run any `gh` commands. When you exit, the "
            f"coordinator also pushes your commits to origin/{rw_branch} "
            "and re-reviews.\n\n"
        )
    effective_briefing = _issue_ctx + rw_report_reminder + briefing

    spec = _AssignmentSpecRw(
        repo_name=repo,
        repo_path=rw_repo_path,
        issue_number=issue,
        issue_title=f"[rework-{next_rw_iteration}] {issue_title}",
        briefing=effective_briefing,
        model=resolved_model,
        type="work",
        provider="claude-pty",
    )
    argv = provider.build_command(spec, resolved_model=resolved_model)
    if not _is_local:
        argv = ["~/.local/bin/claude"] + list(argv)[1:]

    _rw_location = (
        "local TTY" if _is_local
        else f"{machine_obj.host} (remote tmux)"
    )
    _rw_max_iter = cfg.pipeline.max_review_iterations
    click.echo(
        f"{machine} ({_rw_location}) → REWORK of #{issue} "
        f"(iteration {next_rw_iteration}/{_rw_max_iter}) on branch {rw_branch}"
    )
    click.echo(
        "  mode: HUMAN-ATTENDED interactive rework (#563)"
    )
    click.echo(
        f"  assignment id: {assignment_id}  "
        f"(rework_of={rework_of!r}, branch={rw_branch})"
    )
    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would continue branch: {rw_branch}")
        if not _is_local:
            click.echo(
                f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
                f" on {machine_obj.host} (branch: {rw_branch})"
            )
        click.echo(f"  would exec: {argv}")
        return

    rw_assignment = _AssignmentRw(
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=f"[rework-{next_rw_iteration}] {issue_title}",
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        branch=rw_branch,
        pr_url=_rw_work.pr_url if _rw_work is not None else None,
        dispatched_at=_time.time(),
        type="work",
        review_of_assignment_id=rw_work_id,
        review_iteration=next_rw_iteration,
        model=resolved_model,
        provider_name="claude-pty",
    )
    _record_rw(assignment=rw_assignment, repo_github=repo_cfg.github)
    if _svc is None:
        _save_board_rw(_build_board_rw())

    if _is_local:
        try:
            _wt_path, _ = _setup_wt_rw(
                Path(rw_repo_path),
                issue_number=issue,
                issue_title=issue_title,
                assignment_id=assignment_id,
                default_branch=rw_default_branch,
                existing_branch=rw_branch,
            )
            worktree_path = str(_wt_path)
        except (_AgentGitErrorRw, OSError) as _wt_err:
            _reason_rw = (
                f"worktree-add failed for branch {rw_branch}: {_wt_err}"
            )
            click.echo(f"  error: {_reason_rw}", err=True)
            # #618: persist reason + mark terminal immediately so the
            # TUI shows WHY the box is red (no log file on this path).
            _set_fail_reason_rw(assignment_id, _reason_rw)
            sys.exit(1)
        click.echo(f"  worktree: {worktree_path} (branch: {rw_branch})")

        started_at = _time.time()
        exit_code = launch_human_attended_interactive(
            argv,
            effective_briefing,
            assignment_id=assignment_id,
            cwd=worktree_path,
        )
        if exit_code != 0:
            click.echo(f"  claude exited with status {exit_code}", err=True)

        _sname = _tmux_name(assignment_id) if _tmux_avail() else None
        if _sname and _tmux_alive(_sname):
            click.echo(
                f"  session still running in tmux: {_sname}\n"
                f"  reattach with:  coord reattach {assignment_id}"
            )
            sys.exit(0)

        # #982: narrow the stash to only the examples named in the
        # rework-target assignment's smoke tests (if any).
        _aps_rw = _narrow_ap_rw(
            list(repo_cfg.artifact_paths),
            getattr(_rw_work, "smoke_tests", None) if _rw_work is not None else None,
        )
        try:
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                worktree_path=worktree_path,
                base_branch=rw_default_branch,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=rw_repo_path,
                artifact_paths=_aps_rw,
                branch=rw_branch,
            )
            if finalize_result.already_recorded:
                click.echo(
                    "  result recorded via `coord report-result`; backstop "
                    "did not overwrite"
                )
            else:
                click.echo(
                    f"  backstop: status={finalize_result.terminal_status} "
                    f"commits_ahead={finalize_result.commits_ahead}"
                )
                if not finalize_result.push_ok:
                    click.echo(
                        f"  warning: git push failed: {finalize_result.push_error}",
                        err=True,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: backstop failed to record rework exit: {exc}",
                err=True,
            )
        sys.exit(exit_code)

    # ── REMOTE REWORK ─────────────────────────────────────────────
    import shlex as _shlex_rw  # noqa: PLC0415

    _remote_wt = "$HOME/.coord/worktrees/" + assignment_id
    _rp_sh = (
        "$HOME/" + rw_repo_path[2:]
        if rw_repo_path.startswith("~/")
        else ("$HOME" if rw_repo_path == "~" else rw_repo_path)
    )
    _claude_args = _shlex_rw.join(list(argv)[1:])
    _br_q = _shlex_rw.quote(rw_branch)
    _orig_ref = _shlex_rw.quote(f"origin/{rw_branch}")
    _remote_cmd = (
        f"mkdir -p $HOME/.coord/worktrees"
        f" && cd {_rp_sh}"
        f" && git fetch origin --prune 2>/dev/null || true"
        f" && git worktree prune 2>/dev/null || true"
        f" && git worktree add -B {_br_q} {_remote_wt} {_orig_ref}"
        f" && cd {_remote_wt}"
        f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {_claude_args}"
    )
    _tmux_host = TmuxHost(ssh_target=machine_obj.host)
    _sname = _tmux_name(assignment_id)
    click.echo(
        f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
        f" on {machine_obj.host} (branch: {rw_branch})"
    )

    if effective_briefing.strip():
        _hdr = (
            "--- seeded briefing -- review below; "
            "submit the pre-filled input in Claude to send ---"
        )
        _ftr = "-" * len(_hdr)
        _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
        try:
            os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
        except OSError:
            pass

    started_at = _time.time()
    _rc = _tmux_launch(
        argv,
        effective_briefing,
        _sname,
        cwd=None,
        host=_tmux_host,
        raw_shell_cmd=_remote_cmd,
    )
    if _rc is None:
        click.echo(
            "  error: could not create remote tmux session on "
            f"{machine_obj.host}",
            err=True,
        )
        sys.exit(1)
    exit_code = _rc

    if _tmux_alive(_sname, host=_tmux_host):
        click.echo(
            f"  session still running in remote tmux: {_sname}\n"
            f"  reattach with:  ssh -t {machine_obj.host}"
            f" tmux attach-session -t {_sname}\n"
            "  (the rework is not pushed until the session ends and "
            "the coordinator finalizes)"
        )
        sys.exit(0)

    # ── #560: detect setup failures (worktree never created) ──────
    _wt_setup_ok_rw: bool = True
    if exit_code != 0:
        if not _remote_wt_exists(machine_obj.host, _remote_wt):
            _wt_setup_ok_rw = False
            _holder = _find_branch_holder(
                machine_obj.host, _rp_sh, rw_branch,
            )
            if _holder:
                _holder_aid = Path(_holder).name
                _holder_sname = f"coord-{_holder_aid}"
                _holder_live = _tmux_alive(_holder_sname, host=_tmux_host)
                _err_header = (
                    f"  error: setup failed — branch {rw_branch!r} is "
                    f"already checked out at {_holder} on {machine_obj.host}."
                )
                if _holder_live:
                    click.echo("\n".join([
                        _err_header,
                        f"  active tmux session: {_holder_sname}",
                        f"  reattach:  coord reattach {_holder_aid}",
                        "  exit the session first, then retry this rework.",
                    ]), err=True)
                else:
                    # Dead holder — detect base checkout vs stale orphan (#814).
                    if _holder_is_base(_holder):
                        # #814: the base checkout ~/src/<repo> is on the
                        # rework branch.  NEVER prune the base (#561) —
                        # checkout the default branch to free the lock.
                        click.echo(
                            f"  base checkout {_holder!r} is on branch"
                            f" {rw_branch!r} on {machine_obj.host}"
                            f" — checking out {rw_default_branch!r}"
                            f" to free it …"
                        )
                        _freed = _remote_free_base(
                            machine_obj.host, _rp_sh, rw_default_branch,
                        )
                        if _freed:
                            click.echo(
                                "  base checkout freed — retrying launch …"
                            )
                            _rc2 = _tmux_launch(
                                argv, effective_briefing, _sname,
                                cwd=None, host=_tmux_host,
                                raw_shell_cmd=_remote_cmd,
                            )
                            if _rc2 is None:
                                click.echo(
                                    f"  error: could not create remote tmux"
                                    f" session on {machine_obj.host}"
                                    f" (retry after base-checkout free)",
                                    err=True,
                                )
                            else:
                                exit_code = _rc2
                                if _tmux_alive(_sname, host=_tmux_host):
                                    click.echo(
                                        f"  session still running in remote"
                                        f" tmux: {_sname}\n"
                                        f"  reattach with:  ssh -t"
                                        f" {machine_obj.host}"
                                        f" tmux attach-session -t {_sname}\n"
                                        "  (the rework is not pushed until"
                                        " the session ends and the coordinator"
                                        " finalizes)"
                                    )
                                    sys.exit(0)
                                if (
                                    exit_code == 0
                                    or _remote_wt_exists(
                                        machine_obj.host, _remote_wt
                                    )
                                ):
                                    _wt_setup_ok_rw = True  # retry succeeded
                        else:
                            click.echo("\n".join([
                                _err_header,
                                f"  base checkout is stuck on"
                                f" {rw_branch!r} —",
                                f"  free it manually with:",
                                f"    ssh {machine_obj.host}"
                                f" 'git -C {_rp_sh} checkout"
                                f" {rw_default_branch}'",
                            ]), err=True)
                    else:
                        # Dead orphan — safety-gated auto-prune-and-retry (#759).
                        _auto_pruned = False
                        if _remote_orphan_safe(
                            machine_obj.host, _rp_sh, _holder, rw_branch,
                        ):
                            click.echo(
                                f"  auto-pruning stale orphan {_holder}"
                                f" on {machine_obj.host} …"
                            )
                            _auto_pruned = _remote_wt_remove(
                                machine_obj.host, _rp_sh, _holder,
                            )
                        if _auto_pruned:
                            click.echo(
                                "  stale orphan auto-pruned — retrying launch …"
                            )
                            _rc2 = _tmux_launch(
                                argv, effective_briefing, _sname,
                                cwd=None, host=_tmux_host,
                                raw_shell_cmd=_remote_cmd,
                            )
                            if _rc2 is None:
                                click.echo(
                                    f"  error: could not create remote tmux"
                                    f" session on {machine_obj.host}"
                                    f" (retry after prune)",
                                    err=True,
                                )
                            else:
                                exit_code = _rc2
                                if _tmux_alive(_sname, host=_tmux_host):
                                    click.echo(
                                        f"  session still running in remote"
                                        f" tmux: {_sname}\n"
                                        f"  reattach with:  ssh -t"
                                        f" {machine_obj.host}"
                                        f" tmux attach-session -t {_sname}\n"
                                        "  (the rework is not pushed until"
                                        " the session ends and the coordinator"
                                        " finalizes)"
                                    )
                                    sys.exit(0)
                                if (
                                    exit_code == 0
                                    or _remote_wt_exists(
                                        machine_obj.host, _remote_wt
                                    )
                                ):
                                    _wt_setup_ok_rw = True  # retry succeeded
                        else:
                            # Not safe or prune failed → manual command.
                            click.echo("\n".join([
                                _err_header,
                                "  stale worktree — prune it first:",
                                f"    ssh {machine_obj.host} 'cd {_rp_sh}"
                                f" && git worktree remove --force"
                                f" {_shlex_rw.quote(_holder)}'",
                            ]), err=True)
            else:
                click.echo(
                    f"  error: setup failed — the remote worktree was "
                    f"never created (git worktree add refused on "
                    f"{machine_obj.host}).",
                    err=True,
                )
        else:
            click.echo(
                f"  claude exited with status {exit_code}", err=True,
            )

    # Remote finalize: push the rework commits to origin/<branch>,
    # record the completion, and clean up the remote worktree.
    # #982: narrow the stash to the examples named in the rework-target's
    # smoke tests (if any).
    _aps_rw_remote = _narrow_ap_rw(
        list(repo_cfg.artifact_paths),
        getattr(_rw_work, "smoke_tests", None) if _rw_work is not None else None,
    )
    try:
        _fr = finalize_remote_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=issue,
            machine_name=machine,
            ssh_target=machine_obj.host,
            remote_worktree_sh=_remote_wt,
            remote_repo_sh=_rp_sh,
            branch=rw_branch,
            base_branch=rw_default_branch,
            exit_code=exit_code,
            started_at=started_at,
            artifact_paths=_aps_rw_remote,
        )
        if _fr.already_recorded:
            click.echo(
                "  result recorded via `coord report-result`; remote "
                "backstop did not overwrite"
            )
        else:
            click.echo(
                f"  remote backstop: status={_fr.terminal_status} "
                f"commits_ahead={_fr.commits_ahead} pushed={_fr.push_ok}"
            )
            if not _fr.push_ok and _wt_setup_ok_rw:
                click.echo(
                    f"  warning: remote push failed: {_fr.push_error}",
                    err=True,
                )
                click.echo(
                    f"  rework commits preserved in {_remote_wt} on "
                    f"{machine_obj.host} (worktree NOT removed)",
                    err=True,
                )
            # else _wt_setup_ok_rw is False: setup never happened; error
            # already printed above — no "commits preserved" noise.
        _echo_artifact_stash(_fr)
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: remote backstop failed to record rework exit: {exc}",
            err=True,
        )
    sys.exit(exit_code)


def _dispatch_merge_of(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    model: str | None,
    dry_run: bool,
    force: bool,
    merge_of: str,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _svc: object,
    _interactive_board: object,
    _issue_ctx: str,
) -> None:
    """Leg 3c (#517, #306): human-attended MERGE-PREP for approved work.

    #1007: runs on the LOCAL TTY or on a REMOTE machine over ssh+tmux,
    mirroring the local/remote split already in production for
    ``--review-of`` / ``--fix-of`` / ``--rework-of``.  Like a fix, a merge
    WRITES (rebase + force-push) — it needs a worktree on the EXISTING
    branch; unlike a fix, the merge-prep agent itself runs `git push
    --force-with-lease` and `coord merge` as part of its own instructed
    flow, so the coordinator's remote finalize does not need a push-back
    step of its own (the #604 verify-merge gate is the piece that must run
    on whichever host holds the worktree).
    """
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        _holder_is_base_checkout as _holder_is_base,
        _launch_via_tmux as _tmux_launch,
        _remote_base_checkout_free_branch as _remote_free_base,
        _remote_orphan_is_safe_to_prune as _remote_orphan_safe,
        _remote_worktree_remove as _remote_wt_remove,
        find_remote_branch_holder as _find_branch_holder,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
        launch_human_attended_interactive,
        remote_worktree_exists as _remote_wt_exists,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )

    from coord.agent import (  # noqa: PLC0415
        AssignmentSpec as _AssignmentSpecMg,
        _GitError as _AgentGitErrorMg,
        setup_interactive_worktree as _setup_wt_mg,
    )
    from coord.models import Assignment as _AssignmentMg  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        build_board as _build_board_mg,
        record_dispatched_assignment as _record_mg,
        save_board as _save_board_mg,
    )

    def _echo_merge_finalize(finalize_result: object, target_branch: str) -> None:
        # Forensics (#604): the worktree + reflog are gone by now, so this
        # echo is the only post-hoc record of what would merge.
        _mv = finalize_result.merge_verify
        if _mv is not None:
            click.echo(
                f"  merge verify: {target_branch}-ahead="
                f"{_mv.default_ahead} added={len(_mv.added)} commit(s)"
            )
            for _sha, _subj in _mv.added:
                _flag = " [FOREIGN]" if (_sha, _subj) in _mv.foreign else ""
                click.echo(f"    {_sha[:9]} {_subj}{_flag}")
            if not _mv.ok:
                click.echo(
                    "  ✗ MERGE BLOCKED (#604): "
                    f"{_mv.block_summary(target_branch)}",
                    err=True,
                )
        if finalize_result.already_recorded and (_mv is None or _mv.ok):
            click.echo(
                "  result recorded via `coord report-result`; backstop "
                "did not overwrite"
            )
        else:
            click.echo(
                f"  backstop: status={finalize_result.terminal_status} "
                f"commits_ahead={finalize_result.commits_ahead}"
            )

    _mg_board = _interactive_board(_build_board_mg)
    work = _mg_board.find_by_id(merge_of)
    if work is None:
        click.echo(
            f"error: --merge-of {merge_of}: no such assignment on the "
            "board (use the work id from `coord status`).",
            err=True,
        )
        sys.exit(2)
    if not work.branch:
        click.echo(
            f"error: work assignment {merge_of} has no branch to merge.",
            err=True,
        )
        sys.exit(2)

    # #2545: a `type="test-author"`/`"mock-author"` work row's `issue_number`
    # (== `issue` above) is always the milestone's TRACKING issue, but its
    # commit correctly cites the slice's OWN child issue — resolved via the
    # canonical `coord.models.effective_issue_number` helper (built for
    # exactly this tracking-vs-slice distinction, #1553) rather than reading
    # `for_issue_number` directly, so this stays in sync with the other
    # callers of that helper (e.g. `merge_queue._test_author_effective_
    # issue_number`) instead of drifting as an independent copy. Without
    # allowing the resolved issue too, the #604 FOREIGN check below flags
    # every JIT slice's own, correctly-labeled commit on every single verify.
    # Ordinary `work` assignments have no `for_issue_number`, so this
    # resolves to `issue` itself — already in the "home" set, a harmless
    # no-op.
    from coord.models import effective_issue_number as _mg_effective_issue_number  # noqa: PLC0415

    _mg_for_issue = _mg_effective_issue_number(work)
    _mg_extra_allowed = (
        frozenset({_mg_for_issue}) if _mg_for_issue and _mg_for_issue != issue else frozenset()
    )

    resolved_model = model if model else cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]
    if _is_local:
        merge_repo_path = str(
            Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
        )
    else:
        merge_repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"
    merge_target_branch = repo_cfg.default_branch or "main"
    _merge_test_cmd = None
    try:
        _merge_test_cmd = getattr(repo_cfg, "test_command", None)
    except Exception:  # noqa: BLE001
        _merge_test_cmd = None

    INTERACTIVE_MERGE_SYSTEM_PROMPT = (
        "You are a merge-prep agent dispatched by the coordinator. This "
        "session may be attended by a human operator or it may be "
        "unattended — do not assume anyone is watching. The branch has "
        "been reviewed/approved; your job is to get it cleanly rebased, "
        "verified, and MERGED.\n\n"
        "Rules:\n"
        "- Stay on the worker's branch. NEVER push to the default branch "
        "directly.\n"
        "- Use `git push --force-with-lease` (NOT plain --force) after a "
        "rebase.\n"
        "- Resolve MECHANICAL conflicts (non-overlapping struct fields, "
        "list/import entries, separate functions) additively — keep both "
        "sides. For SEMANTIC conflicts (same logic changed two ways), do "
        "NOT guess: explain the conflict to the operator and let them "
        "decide.\n"
        "- NEVER push to the default branch directly or use `gh`. "
        f"`coord merge --repo {repo}` is the ONLY merge path — it routes "
        "to the coordinator and re-checks the CI / review / smoke gates "
        "before merging via PR. You run it yourself, but ONLY after "
        "`coord verify-merge` passes clean.\n"
        "- If `coord merge` reports a gate failure (CI red, conflict, "
        "missing verdict), STOP and report it to the operator — never "
        "`--force-merge` / `--skip-*` on your own.\n\n"
        "Flow:\n"
        "1. `git fetch origin`.\n"
        "2. Rebase the branch onto `origin/<default_branch>`.\n"
        "3. Resolve conflicts (mechanical additively; semantic with the "
        "operator).\n"
        "4. Run the project's build/tests to confirm nothing broke.\n"
        "5. `git push --force-with-lease`.\n"
        f"6. Run `coord verify-merge {merge_of}` to self-check the "
        "result.\n"
        "   `default-ahead != 0` means the rebase landed on a stale "
        "base — redo the rebase onto the current `origin/<default_branch>` "
        "and re-push.\n"
        "   FOREIGN commits: `coord verify-merge` flags a commit as "
        "FOREIGN when its *subject line* does not contain the issue "
        "number. It NEVER reads the diff — only the subject string. "
        "This means a commit that is this branch's own work can be "
        "flagged if the subject has a typo'd or omitted issue ref. "
        "Before treating any FOREIGN flag as a real strand, apply this "
        "decision procedure for each flagged commit:\n"
        "   (a) Run `git show <sha>` and read the diff. If the content "
        "is clearly this issue's own work → reword the commit subject "
        "to include the issue number (NEVER drop the commit). Verify "
        "the tree is unchanged: `git rev-parse HEAD^{tree}` before and "
        "after must be identical. Re-run `coord verify-merge` — it "
        "should now pass.\n"
        "   (b) Genuinely unrelated content AND the commit is already an "
        "ancestor of the base branch → drop it from the branch.\n"
        "   (c) Genuinely unrelated content AND NOT on the base branch → "
        "this is a real foreign strand. Escalate to the operator.\n"
        "   Exhaust-before-blocking checklist (complete this BEFORE "
        "reporting `--status blocked`):\n"
        "   [ ] List every FOREIGN-flagged commit\n"
        "   [ ] Run `git show <sha>` on each to read the diff\n"
        "   [ ] Categorise each: (a) own-mislabeled, (b) ancestor-on-"
        "base, (c) genuine strand\n"
        "   [ ] Resolve (a) and (b) autonomously; only escalate (c)\n"
        "   A `--status blocked` report MUST state: what you tried, "
        "which specific commit is the obstacle, and the single decision "
        "the operator must make. Blocking without that detail is itself "
        "a prompt violation.\n"
        "   Blocking cost: the operator must context-switch to this "
        "session and the branch sits stranded until they do. It is not "
        "a free exit — exhaust autonomous options first.\n"
        "   The coordinator also runs this check on exit and will record "
        "`blocked` (not `done`) if it still fails after you exit, so a "
        "bad rebase cannot slip through regardless.\n"
        f"7. Complete the merge: run `coord merge --repo {repo}` (it opens "
        "the PR, enforces the gates, and merges in dependency order). "
        "Report the outcome — merged, or which gate blocked.\n"
    )

    merge_briefing = (
        f"# Merge-prep assignment: {repo_cfg.github} #{issue}\n\n"
        f"**Issue:** {issue_title}\n"
        f"**Branch to merge:** `{work.branch}` "
        f"(worker: {work.machine_name or machine})\n"
        f"**Rebase onto:** `origin/{merge_target_branch}`\n"
        f"**Work assignment id:** `{merge_of}`\n"
        + (
            f"**Test command:** `{_merge_test_cmd}`\n"
            if _merge_test_cmd
            else ""
        )
        + "\n## Your job\n\n"
        "This branch is approved. Fetch, rebase it onto "
        f"`origin/{merge_target_branch}` (#306 proactive rebase), resolve "
        "any conflicts (mechanical additively; semantic with the "
        "operator), run the tests, and `git push --force-with-lease`. "
        f"Before you report done, run `coord verify-merge {merge_of}`. "
        f"If `{merge_target_branch}-ahead != 0`, the rebase is on a "
        "stale base — redo it. If FOREIGN commits are listed, read the "
        "system-prompt decision procedure (a/b/c) before deciding whether "
        "to reword, drop, or escalate — FOREIGN is a subject-string check "
        "that never reads diffs, so it can fire on this branch's own "
        "mislabeled commit (#604). "
        f"Once verify-merge passes clean, COMPLETE the merge yourself: "
        f"run `coord merge --repo {repo}` (it re-checks CI/review/smoke "
        "on the coordinator and merges via PR). If it reports a gate "
        "failure, report that to the operator instead of forcing.\n"
    )

    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
    report_reminder = (
        f"[Coordinator merge assignment {assignment_id}] HUMAN-ATTENDED "
        f"merge-prep on branch {work.branch} (rebasing onto "
        f"{merge_target_branch}). Before you exit, run `coord "
        f"report-result --assignment {assignment_id} --status done "
        "--summary <text>` (use --status blocked only for a semantic "
        "conflict or genuine foreign strand that needs the operator; "
        "blocked reports must name what was tried and the single decision "
        "needed).\n\n"
    )
    effective_briefing = _issue_ctx + report_reminder + merge_briefing

    spec = _AssignmentSpecMg(
        repo_name=repo,
        repo_path=merge_repo_path,
        issue_number=issue,
        issue_title=f"[merge] {issue_title}",
        briefing=effective_briefing,
        model=resolved_model,
        type="conflict-fix",
        provider="claude-pty",
    )
    # Full worker tool set (Read/Edit/Write/Bash) — rebasing and resolving
    # conflicts mutates the checkout.
    argv = provider.build_command(
        spec,
        resolved_model=resolved_model,
        system_prompt=INTERACTIVE_MERGE_SYSTEM_PROMPT,
    )
    # Remote: bare "claude" isn't on the SSH login PATH (#424/#425).
    if not _is_local:
        argv = ["~/.local/bin/claude"] + list(argv)[1:]

    _mg_location = (
        "local TTY" if _is_local
        else f"{machine_obj.host} (remote tmux)"
    )
    click.echo(
        f"{machine} ({_mg_location}) → MERGE-PREP of #{issue} "
        f"on branch {work.branch}: {issue_title}"
    )
    click.echo("  mode: HUMAN-ATTENDED interactive merge agent (leg 3c)")
    click.echo(
        f"  assignment id: {assignment_id}  (merge_of={merge_of}, "
        f"rebase onto origin/{merge_target_branch})"
    )
    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would continue branch: {work.branch}")
        if not _is_local:
            click.echo(
                f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
                f" on {machine_obj.host} (branch: {work.branch})"
            )
        click.echo(f"  would exec: {argv}")
        return

    merge_assignment = _AssignmentMg(
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=f"[merge] {issue_title}",
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        branch=work.branch,
        pr_url=work.pr_url,
        dispatched_at=_time.time(),
        type="conflict-fix",
        review_of_assignment_id=work.assignment_id,
        model=resolved_model,
        provider_name="claude-pty",
    )
    _record_mg(assignment=merge_assignment, repo_github=repo_cfg.github)
    if _svc is None:
        _save_board_mg(_build_board_mg())

    if _is_local:
        try:
            _wt_path, _ = _setup_wt_mg(
                Path(merge_repo_path),
                issue_number=issue,
                issue_title=issue_title,
                assignment_id=assignment_id,
                default_branch=merge_target_branch,
                existing_branch=work.branch,
            )
            worktree_path = str(_wt_path)
        except (_AgentGitErrorMg, OSError) as _wt_err:
            click.echo(
                f"  error: could not create merge worktree on branch "
                f"{work.branch}: {_wt_err}",
                err=True,
            )
            sys.exit(1)
        click.echo(f"  worktree: {worktree_path} (branch: {work.branch})")

        started_at = _time.time()
        exit_code = launch_human_attended_interactive(
            argv,
            effective_briefing,
            assignment_id=assignment_id,
            cwd=worktree_path,
        )
        if exit_code != 0:
            click.echo(f"  claude exited with status {exit_code}", err=True)

        _sname = _tmux_name(assignment_id) if _tmux_avail() else None
        if _sname and _tmux_alive(_sname):
            click.echo(
                f"  session still running in tmux: {_sname}\n"
                f"  reattach with:  coord reattach {assignment_id}"
            )
            sys.exit(0)

        try:
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                worktree_path=worktree_path,
                base_branch=merge_target_branch,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=merge_repo_path,
                # #604: git truth overrides the agent's self-report on the
                # merge path — a botched rebase records `blocked`, not `done`.
                verify_merge=True,
                # #2545: allow the JIT slice's own child issue, not just the
                # tracking issue, in the FOREIGN check.
                extra_allowed_issue_numbers=_mg_extra_allowed,
                branch=work.branch,
            )
            _echo_merge_finalize(finalize_result, merge_target_branch)
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: backstop failed to record merge exit: {exc}",
                err=True,
            )
        sys.exit(exit_code)

    # ── REMOTE MERGE (#1007) ──────────────────────────────────────
    # A remote worktree on the EXISTING branch (`-B <branch>
    # origin/<branch>` resets a dedicated local branch to the reviewed
    # work's branch) — same shape as the remote fix path.  The merge-prep
    # agent itself runs `git push --force-with-lease` and `coord merge` as
    # part of its instructed flow (INTERACTIVE_MERGE_SYSTEM_PROMPT above),
    # so on exit the coordinator's job is to VERIFY the rebase (#604) on
    # the remote worktree before trusting any self-report, then record
    # the completion.
    import shlex as _shlex_mg  # noqa: PLC0415

    _remote_wt = "$HOME/.coord/worktrees/" + assignment_id
    _rp_sh = (
        "$HOME/" + merge_repo_path[2:]
        if merge_repo_path.startswith("~/")
        else ("$HOME" if merge_repo_path == "~" else merge_repo_path)
    )
    _claude_args = _shlex_mg.join(list(argv)[1:])
    _br_q = _shlex_mg.quote(work.branch)
    _orig_ref = _shlex_mg.quote(f"origin/{work.branch}")
    _remote_cmd = (
        f"mkdir -p $HOME/.coord/worktrees"
        f" && cd {_rp_sh}"
        f" && git fetch origin --prune 2>/dev/null || true"
        f" && git worktree prune 2>/dev/null || true"
        f" && git worktree add -B {_br_q} {_remote_wt} {_orig_ref}"
        f" && cd {_remote_wt}"
        f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {_claude_args}"
    )
    _tmux_host = TmuxHost(ssh_target=machine_obj.host)
    _sname = _tmux_name(assignment_id)
    click.echo(
        f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
        f" on {machine_obj.host} (branch: {work.branch})"
    )

    if effective_briefing.strip():
        _hdr = (
            "--- seeded briefing -- review below; "
            "submit the pre-filled input in Claude to send ---"
        )
        _ftr = "-" * len(_hdr)
        _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
        try:
            os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
        except OSError:
            pass

    started_at = _time.time()
    _rc = _tmux_launch(
        argv,
        effective_briefing,
        _sname,
        cwd=None,
        host=_tmux_host,
        raw_shell_cmd=_remote_cmd,
    )
    if _rc is None:
        click.echo(
            "  error: could not create remote tmux session on "
            f"{machine_obj.host}",
            err=True,
        )
        sys.exit(1)
    exit_code = _rc

    if _tmux_alive(_sname, host=_tmux_host):
        click.echo(
            f"  session still running in remote tmux: {_sname}\n"
            f"  reattach with:  ssh -t {machine_obj.host}"
            f" tmux attach-session -t {_sname}\n"
            "  (the merge is not finalized until the session ends and "
            "the coordinator finalizes)"
        )
        sys.exit(0)

    # ── #560: detect setup failures (worktree never created) ──────
    _wt_setup_ok_mg: bool = True
    if exit_code != 0:
        if not _remote_wt_exists(machine_obj.host, _remote_wt):
            _wt_setup_ok_mg = False
            _holder = _find_branch_holder(
                machine_obj.host, _rp_sh, work.branch,
            )
            if _holder:
                _holder_aid = Path(_holder).name
                _holder_sname = f"coord-{_holder_aid}"
                _holder_live = _tmux_alive(_holder_sname, host=_tmux_host)
                _err_header = (
                    f"  error: setup failed — branch {work.branch!r} is "
                    f"already checked out at {_holder} on {machine_obj.host}."
                )
                if _holder_live:
                    click.echo("\n".join([
                        _err_header,
                        f"  active tmux session: {_holder_sname}",
                        f"  reattach:  coord reattach {_holder_aid}",
                        "  exit the session first, then retry this merge.",
                    ]), err=True)
                else:
                    # Dead holder — detect base checkout vs stale orphan (#814).
                    if _holder_is_base(_holder):
                        # #814: the base checkout ~/src/<repo> is on the
                        # merge branch.  NEVER prune the base (#561) —
                        # checkout the default branch to free the lock.
                        click.echo(
                            f"  base checkout {_holder!r} is on branch"
                            f" {work.branch!r} on {machine_obj.host}"
                            f" — checking out {merge_target_branch!r}"
                            f" to free it …"
                        )
                        _freed = _remote_free_base(
                            machine_obj.host, _rp_sh, merge_target_branch,
                        )
                        if _freed:
                            click.echo(
                                "  base checkout freed — retrying launch …"
                            )
                            _rc2 = _tmux_launch(
                                argv, effective_briefing, _sname,
                                cwd=None, host=_tmux_host,
                                raw_shell_cmd=_remote_cmd,
                            )
                            if _rc2 is None:
                                click.echo(
                                    f"  error: could not create remote tmux"
                                    f" session on {machine_obj.host}"
                                    f" (retry after base-checkout free)",
                                    err=True,
                                )
                            else:
                                exit_code = _rc2
                                if _tmux_alive(_sname, host=_tmux_host):
                                    click.echo(
                                        f"  session still running in remote"
                                        f" tmux: {_sname}\n"
                                        f"  reattach with:  ssh -t"
                                        f" {machine_obj.host}"
                                        f" tmux attach-session -t {_sname}\n"
                                        "  (the merge is not finalized until"
                                        " the session ends and the"
                                        " coordinator finalizes)"
                                    )
                                    sys.exit(0)
                                if (
                                    exit_code == 0
                                    or _remote_wt_exists(
                                        machine_obj.host, _remote_wt
                                    )
                                ):
                                    _wt_setup_ok_mg = True  # retry succeeded
                        else:
                            click.echo("\n".join([
                                _err_header,
                                f"  base checkout is stuck on"
                                f" {work.branch!r} —",
                                f"  free it manually with:",
                                f"    ssh {machine_obj.host}"
                                f" 'git -C {_rp_sh} checkout"
                                f" {merge_target_branch}'",
                            ]), err=True)
                    else:
                        # Dead orphan — safety-gated auto-prune-and-retry (#759).
                        _auto_pruned = False
                        if _remote_orphan_safe(
                            machine_obj.host, _rp_sh, _holder, work.branch,
                        ):
                            click.echo(
                                f"  auto-pruning stale orphan {_holder}"
                                f" on {machine_obj.host} …"
                            )
                            _auto_pruned = _remote_wt_remove(
                                machine_obj.host, _rp_sh, _holder,
                            )
                        if _auto_pruned:
                            click.echo(
                                "  stale orphan auto-pruned — retrying launch …"
                            )
                            _rc2 = _tmux_launch(
                                argv, effective_briefing, _sname,
                                cwd=None, host=_tmux_host,
                                raw_shell_cmd=_remote_cmd,
                            )
                            if _rc2 is None:
                                click.echo(
                                    f"  error: could not create remote tmux"
                                    f" session on {machine_obj.host}"
                                    f" (retry after prune)",
                                    err=True,
                                )
                            else:
                                exit_code = _rc2
                                if _tmux_alive(_sname, host=_tmux_host):
                                    click.echo(
                                        f"  session still running in remote"
                                        f" tmux: {_sname}\n"
                                        f"  reattach with:  ssh -t"
                                        f" {machine_obj.host}"
                                        f" tmux attach-session -t {_sname}\n"
                                        "  (the merge is not finalized until"
                                        " the session ends and the"
                                        " coordinator finalizes)"
                                    )
                                    sys.exit(0)
                                if (
                                    exit_code == 0
                                    or _remote_wt_exists(
                                        machine_obj.host, _remote_wt
                                    )
                                ):
                                    _wt_setup_ok_mg = True  # retry succeeded
                        else:
                            # Not safe or prune failed → manual command.
                            click.echo("\n".join([
                                _err_header,
                                "  stale worktree — prune it first:",
                                f"    ssh {machine_obj.host} 'cd {_rp_sh}"
                                f" && git worktree remove --force"
                                f" {_shlex_mg.quote(_holder)}'",
                            ]), err=True)
            else:
                click.echo(
                    f"  error: setup failed — the remote worktree was "
                    f"never created (git worktree add refused on "
                    f"{machine_obj.host}).",
                    err=True,
                )
        else:
            click.echo(
                f"  claude exited with status {exit_code}", err=True,
            )

    # Remote finalize (#1007): verify the rebase (#604) on the remote
    # worktree BEFORE trusting any self-report, then record the
    # completion and clean up.  Safe even when the worktree was never
    # created — remote_worktree_exists() gates the verify step and
    # _remote_push_and_count detects the missing directory.
    try:
        finalize_result = finalize_remote_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo,
            repo_github=repo_cfg.github,
            issue_number=issue,
            machine_name=machine,
            ssh_target=machine_obj.host,
            remote_worktree_sh=_remote_wt,
            remote_repo_sh=_rp_sh,
            branch=work.branch,
            base_branch=merge_target_branch,
            exit_code=exit_code,
            started_at=started_at,
            verify_merge=True,
            # #2545: allow the JIT slice's own child issue, not just the
            # tracking issue, in the FOREIGN check.
            extra_allowed_issue_numbers=_mg_extra_allowed,
        )
        _echo_merge_finalize(finalize_result, merge_target_branch)
        _mv = finalize_result.merge_verify
        if not finalize_result.push_ok and _wt_setup_ok_mg and (
            _mv is None or _mv.ok
        ):
            # Worker ran but the coordinator's own backstop push failed —
            # the agent's own `git push --force-with-lease` (briefing step
            # 5) may still have landed; this is a secondary signal only.
            click.echo(
                f"  warning: remote push failed: {finalize_result.push_error}",
                err=True,
            )
            click.echo(
                f"  merge commits preserved in {_remote_wt} on "
                f"{machine_obj.host} (worktree NOT removed)",
                err=True,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: backstop failed to record remote merge exit: {exc}",
            err=True,
        )
    sys.exit(exit_code)


def _dispatch_interactive_work(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    model: str | None,
    dry_run: bool,
    plan_only: bool,
    no_plan: bool,
    force: bool,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_title: str,
    provider: object,
    _is_local: bool,
    _issue_ctx: str,
    _ctx_write_hint: str,
) -> None:
    """#437: the flavour-less human-attended interactive work/plan launch (no --review-of/--fix-of/etc)."""
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        _holder_is_base_checkout as _holder_is_base,
        _launch_via_tmux as _tmux_launch,
        _remote_base_checkout_free_branch as _remote_free_base,
        _remote_orphan_is_safe_to_prune as _remote_orphan_safe,
        _remote_worktree_remove as _remote_wt_remove,
        find_remote_branch_holder as _find_branch_holder,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
        launch_human_attended_interactive,
        remote_worktree_exists as _remote_wt_exists,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )

    if _is_local:
        # Expand `~` — repo_paths in coordinator.yml use `~/src/...`,
        # and unlike the agent (which expands everywhere) this local
        # interactive launch passes the path straight to the child's cwd,
        # so a literal `~` would fail with "No such file or directory".
        # Local launch ⇒ local home.
        repo_path = str(
            Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
        )
    else:
        # Remote: keep the raw path from coordinator.yml so the remote
        # shell can expand `~` itself.  Fall back to ~/src/<repo> when no
        # repo_path is configured (common for new machines).
        repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"

    effective_plan_only = plan_only or (
        cfg.dispatch.require_plan and not no_plan
    )
    repo_default_branch = repo_cfg.default_branch or "main"

    # ── Claim check.  Without this an operator can spawn two
    # interactive sessions on the same issue and both push competing
    # branches.  --force bypasses the check (mirrors the
    # claude -p path below).
    from coord.board_service import read_board, write_board  # noqa: PLC0415
    from coord.claim import claim_message, find_work_claim  # noqa: PLC0415
    from coord.state import record_dispatched  # noqa: PLC0415

    board_check = read_board()
    if not force:
        claim = find_work_claim(issue, repo, repo_cfg.github, board_check)
        if claim is not None:
            click.echo(
                f"  skipping: {claim_message(claim)}",
                err=True,
            )
            sys.exit(1)

    # #1138: the oracle-loop issue-level gate applies to --interactive work
    # dispatch too, not just headless — a human at the keyboard can miss the
    # same gap a `claude -p` worker would. Deliberately NOT bypassed by
    # --force (that flag is documented as an infra-retry/claim escape hatch,
    # not a way around the acceptance-authoring requirement).
    from coord.dispatch import (  # noqa: PLC0415
        enforce_epic_dispatch_guard,
        enforce_oracle_readiness,
    )

    try:
        enforce_oracle_readiness(
            proposal_type="plan" if effective_plan_only else "work",
            repo=repo_cfg, config=cfg, issue_number=issue,
        )
        # #1314: same epic-target gate as the headless dispatch() path —
        # a human at the keyboard can dispatch --interactive directly
        # against a tracking issue's own number just as easily as a
        # headless worker.
        enforce_epic_dispatch_guard(
            proposal_type="plan" if effective_plan_only else "work",
            repo=repo_cfg, config=cfg, issue_number=issue,
        )
    except ValueError as e:
        click.echo(f"  skipping: {e}", err=True)
        sys.exit(1)

    from coord.agent import AssignmentSpec  # noqa: PLC0415
    from coord.models import Proposal  # noqa: PLC0415

    # #1430: deliberately NOT consulting models.labels here. This is the
    # human-attended `--interactive` launcher (subscription-billed, not
    # metered per dispatch) — the operator is at the keyboard and can pass
    # --model directly when a tier matters; unlike the headless paths, a
    # wrong default here doesn't silently burn API budget unattended.
    resolved_model = model if model else cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]

    spec = AssignmentSpec(
        repo_name=repo,
        repo_path=repo_path,
        issue_number=issue,
        issue_title=issue_title,
        briefing=briefing,
        model=resolved_model,
        type="plan" if effective_plan_only else "work",
        provider="claude-pty",
    )

    # Build a minimal Proposal — only the fields record_dispatched
    # consumes need to be set.  The actual record_dispatched call is
    # deferred until after the dry-run gate below so `--dry-run`
    # leaves no phantom "running" row in the DB.
    proposal = Proposal(
        id=0,
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=issue_title,
        rationale="manual --interactive dispatch (human-attended)",
        briefing=briefing,
        model=resolved_model,
        type="plan" if effective_plan_only else "work",
        required_gates=[],
    )

    argv = provider.build_command(spec, resolved_model=resolved_model)

    # For remote: replace the binary (argv[0]) with the absolute path
    # to claude on the remote machine.  A bare "claude" is not on the
    # SSH login PATH (#424/#425); ~/.local/bin/claude is the canonical
    # location installed by `claude` setup on Linux.
    _REMOTE_CLAUDE_BIN = "~/.local/bin/claude"
    if not _is_local:
        argv = [_REMOTE_CLAUDE_BIN] + list(argv)[1:]

    _location = "local TTY" if _is_local else f"{machine_obj.host} (remote tmux)"
    click.echo(f"{machine} ({_location}) → {repo} #{issue}: {issue_title}")
    click.echo("  mode: HUMAN-ATTENDED interactive launch (#437)")
    click.echo(f"  assignment id: {assignment_id}")
    click.echo(
        "  the briefing will be PRE-FILLED in the input box; "
        "press Enter to submit; Ctrl-C / `/exit` to end the session."
    )
    if dry_run:
        from coord.agent import _slugify as _slugify_dry  # noqa: PLC0415
        _dry_branch = f"issue-{issue}-{_slugify_dry(issue_title)}"
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        if _is_local:
            click.echo(
                f"  cwd: worktree for {_dry_branch} "
                f"(under ~/.coord/worktrees/<assignment_id>)"
            )
        else:
            _dry_wt = f"~/.coord/worktrees/{assignment_id}"
            click.echo(
                f"  remote worktree: {_dry_wt} on {machine_obj.host}"
                f" (branch: {_dry_branch})"
            )
        return

    if _is_local:
        # ── LOCAL PATH ────────────────────────────────────────────────
        # Byte-identical to the pre-#494 behaviour: create an isolated
        # worktree + feature branch locally via setup_interactive_worktree,
        # then attach the current terminal directly via
        # launch_human_attended_interactive.
        from coord.agent import (  # noqa: PLC0415
            _GitError as _AgentGitError,
            setup_interactive_worktree,
        )
        try:
            _wt_path, _interactive_branch = setup_interactive_worktree(
                Path(repo_path),
                issue_number=issue,
                issue_title=issue_title,
                assignment_id=assignment_id,
                default_branch=repo_default_branch,
            )
            worktree_path = str(_wt_path)
        except (_AgentGitError, OSError) as _wt_err:
            click.echo(
                f"  error: could not create worktree for interactive session: {_wt_err}",
                err=True,
            )
            sys.exit(1)

        click.echo(f"  worktree: {worktree_path} (branch: {_interactive_branch})")

        # State mutations (DB row, env var, board write) ONLY on real
        # dispatch — never in dry-run.  Record up front so:
        #   * claim detection refuses a duplicate the second the human
        #     hits Enter on a parallel `coord assign --interactive`,
        #   * the board shows the in-flight interactive session,
        #   * the issue_store seam has a row to UPDATE on exit.
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal,
            repo_github=repo_cfg.github,
            provider_name="claude-pty",
        )

        # #466: Inject the assignment id into the agent's process env so
        # the interactive Claude session can run
        # `coord report-result --assignment $COORD_ASSIGNMENT_ID …` to
        # report a structured result before exiting.  Also prepend a
        # short reminder to the briefing so the operator notices.
        #
        # #646: do NOT offer --verdict here — this is a work/plan session.
        # A verdict belongs only on a review session. Offering it led the
        # work agent to run `report-result --verdict approve` against its
        # OWN work id, stamping a bogus verdict and finalizing a still-live
        # session (the write seam now rejects that too, but don't tempt it).
        os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
        report_reminder = (
            f"[Coordinator assignment {assignment_id}] "
            "Before you exit, please run `coord report-result "
            f"--assignment {assignment_id} --status <done|blocked|"
            "already-implemented> --summary <text>` so the coordinator "
            "records the result.\n\n"
        )
        effective_briefing = _issue_ctx + report_reminder + briefing + _ctx_write_hint

        # Update board metadata (round_number / board_initialized).
        # `record_dispatched` already wrote the assignment row, so the
        # read_board → write_board round-trip is a no-op for the
        # assignments table; the useful side-effect is board_meta — and
        # write_board() now actually reaches the daemon on a thin client
        # instead of silently touching a local DB that isn't canonical (#749).
        write_board(read_board())

        started_at = _time.time()
        # #487: pass assignment_id so the tmux path names the session
        # coord-<assignment_id>, enabling reattach after a TUI crash.
        exit_code = launch_human_attended_interactive(
            argv, effective_briefing, assignment_id=assignment_id, cwd=worktree_path,
        )
        if exit_code != 0:
            click.echo(f"  claude exited with status {exit_code}", err=True)

        # #487: if the tmux session is still alive the user just detached
        # (Ctrl-b d) or the TUI crashed.  Skip finalize — the session is
        # still running.  Tell the operator how to reattach and let them
        # close the session themselves (at which point coord report-result
        # or coord reattach will record the terminal state).
        _sname = _tmux_name(assignment_id) if _tmux_avail() else None
        if _sname and _tmux_alive(_sname):
            click.echo(
                f"  session still running in tmux: {_sname}\n"
                f"  reattach with:  coord reattach {assignment_id}\n"
                f"  or from shell:  tmux attach-session -t {_sname}"
            )
            sys.exit(0)

        # #466 — git-floor backstop.  ALWAYS write a terminal state for
        # this assignment through the issue_store seam, regardless of
        # whether the agent typed `coord report-result` first.  The
        # finalizer respects an existing report (it checks the DB row's
        # status before clobbering).
        # #982: a fresh interactive work session has no pre-existing
        # smoke_tests to narrow against (none are available until the
        # worker's own session finishes, and that session runs in a live
        # tmux/TTY pane rather than a log file this code can parse), so
        # this always stashes the full repo-wide glob — narrowing happens
        # centrally in AgentServer._stash_artifacts for the headless Work
        # path instead.
        try:
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                worktree_path=worktree_path,
                base_branch=repo_default_branch,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=repo_path,
                artifact_paths=repo_cfg.artifact_paths,
                branch=_interactive_branch,
            )
            if finalize_result.already_recorded:
                click.echo(
                    "  result already recorded via `coord report-result`; "
                    "backstop did not overwrite",
                )
            else:
                click.echo(
                    f"  backstop: status={finalize_result.terminal_status} "
                    f"commits_ahead={finalize_result.commits_ahead}"
                )
                if not finalize_result.push_ok:
                    click.echo(
                        f"  warning: git push failed: {finalize_result.push_error}",
                        err=True,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: backstop failed to record completion: {exc}",
                err=True,
            )

        sys.exit(exit_code)

    else:
        # ── REMOTE PATH (#494 / #486b) ────────────────────────────────
        # The target machine is not the local host.  We create a named
        # tmux session ON THE REMOTE machine that:
        #   1. cd's into the remote repo checkout,
        #   2. fetches origin + prunes worktrees,
        #   3. creates a feature-branch worktree at
        #      ~/.coord/worktrees/<assignment_id>,
        #   4. cd's into the worktree,
        #   5. launches claude with COORD_ASSIGNMENT_ID set inline.
        #
        # The local terminal ATTACHES to the remote tmux session so the
        # operator can drive it as if it were a local session.
        #
        # We use $HOME in the shell command (not ~) so that the paths
        # survive single-quote wrapping during remote transmission.
        # (~ inside single quotes is NOT expanded; $HOME inside a
        # tmux-run shell command IS expanded by the final shell.)
        import shlex as _shlex  # noqa: PLC0415

        from coord.agent import _slugify as _remote_slugify  # noqa: PLC0415

        # Mirror setup_interactive_worktree branch/worktree naming.
        _remote_branch = f"issue-{issue}-{_remote_slugify(issue_title)}"
        _remote_wt = "$HOME/.coord/worktrees/" + assignment_id

        # repo_path may be ~/src/repo or an absolute path; replace
        # leading ~/ with $HOME/ so the shell expands it correctly.
        _rp_sh = (
            "$HOME/" + repo_path[2:]
            if repo_path.startswith("~/")
            else ("$HOME" if repo_path == "~" else repo_path)
        )

        # Build the shell command the remote tmux session will run.
        # Tries fresh -b first (new branch from origin/default), falls
        # back to -B (force-reset) from origin/<branch> (retry case),
        # then from origin/<default> as a last resort.
        _claude_args = _shlex.join(list(argv)[1:])
        _remote_cmd = (
            f"mkdir -p $HOME/.coord/worktrees"
            f" && cd {_rp_sh}"
            f" && git fetch origin --prune 2>/dev/null || true"
            f" && git worktree prune 2>/dev/null || true"
            f" && (git worktree add -b {_remote_branch} {_remote_wt}"
            f" origin/{repo_default_branch} 2>/dev/null"
            f" || git worktree add -B {_remote_branch} {_remote_wt}"
            f" origin/{_remote_branch} 2>/dev/null"
            f" || git worktree add -B {_remote_branch} {_remote_wt}"
            f" origin/{repo_default_branch})"
            f" && cd {_remote_wt}"
            f" && COORD_ASSIGNMENT_ID={assignment_id}"
            f" {argv[0]} {_claude_args}"
        )

        _tmux_host = TmuxHost(ssh_target=machine_obj.host)
        _sname = _tmux_name(assignment_id)

        click.echo(
            f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
            f" on {machine_obj.host} (branch: {_remote_branch})"
        )

        # State mutations (DB row, env var, board write) — same as local.
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal,
            repo_github=repo_cfg.github,
            provider_name="claude-pty",
        )
        # #486d: record the remote feature branch on the assignment so a
        # later `coord reattach` can push it back (record_dispatched writes
        # from the Proposal, which carries no branch).
        try:
            from coord import sql as _sql_wb  # noqa: PLC0415
            from coord.state import get_connection as _gc_wb  # noqa: PLC0415
            _conn_wb = _gc_wb()
            _sql_wb.execute(
                _conn_wb,
                "UPDATE assignments SET branch=? WHERE assignment_id=?",
                (_remote_branch, assignment_id),
            )
            _conn_wb.commit()
        except Exception:  # noqa: BLE001
            pass

        # Set COORD_ASSIGNMENT_ID in the local coordinator env as well
        # (for symmetry with local path; the remote process gets it
        # inline via the shell command).
        os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
        # #646: no --verdict for a work/plan session (review-only field).
        report_reminder = (
            f"[Coordinator assignment {assignment_id}] "
            "Before you exit, please run `coord report-result "
            f"--assignment {assignment_id} --status <done|blocked|"
            "already-implemented> --summary <text>` so the coordinator "
            "records the result.\n\n"
        )
        effective_briefing = _issue_ctx + report_reminder + briefing + _ctx_write_hint

        write_board(read_board())

        # Echo briefing to the LOCAL terminal before connecting to the
        # remote session, so the operator can read it before pressing
        # Enter (mirrors the tmux path in launch_human_attended_interactive).
        if effective_briefing.strip():
            _hdr = (
                "--- seeded briefing -- review below; "
                "submit the pre-filled input in Claude to send ---"
            )
            _ftr = "-" * len(_hdr)
            _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
            try:
                os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
            except OSError:
                pass

        # Launch the remote tmux session and attach to it.  Pass
        # raw_shell_cmd so _launch_via_tmux uses the verbatim shell
        # command (with $HOME paths and && operators) rather than
        # re-quoting argv through shlex.join.
        started_at = _time.time()
        _rc = _tmux_launch(
            argv,
            effective_briefing,
            _sname,
            cwd=None,
            host=_tmux_host,
            raw_shell_cmd=_remote_cmd,
        )
        if _rc is None:
            click.echo(
                f"  error: could not create remote tmux session on"
                f" {machine_obj.host}",
                err=True,
            )
            sys.exit(1)
        exit_code = _rc

        # Check if the remote session is still alive (user detached).
        if _tmux_alive(_sname, host=_tmux_host):
            click.echo(
                f"  session still running in remote tmux: {_sname}\n"
                f"  reattach with:  coord reattach {assignment_id}\n"
                "  (work commits are pushed when the session ends and the "
                "coordinator finalizes)"
            )
            sys.exit(0)

        # ── #560: detect setup failures (worktree never created) ──────
        _wt_setup_ok_work: bool = True
        if exit_code != 0:
            if not _remote_wt_exists(machine_obj.host, _remote_wt):
                _wt_setup_ok_work = False
                _holder = _find_branch_holder(
                    machine_obj.host, _rp_sh, _remote_branch,
                )
                if _holder:
                    _holder_aid = Path(_holder).name
                    _holder_sname = f"coord-{_holder_aid}"
                    _holder_live = _tmux_alive(_holder_sname, host=_tmux_host)
                    _err_header = (
                        f"  error: setup failed — branch {_remote_branch!r} is "
                        f"already checked out at {_holder} on {machine_obj.host}."
                    )
                    if _holder_live:
                        click.echo("\n".join([
                            _err_header,
                            f"  active tmux session: {_holder_sname}",
                            f"  reattach:  coord reattach {_holder_aid}",
                            "  exit the session first, then retry this work.",
                        ]), err=True)
                    else:
                        # Dead holder — detect base checkout vs stale orphan (#814).
                        if _holder_is_base(_holder):
                            # #814: the base checkout ~/src/<repo> is on the
                            # work branch.  NEVER prune the base (#561) —
                            # checkout the default branch to free the lock.
                            click.echo(
                                f"  base checkout {_holder!r} is on branch"
                                f" {_remote_branch!r} on {machine_obj.host}"
                                f" — checking out {repo_default_branch!r}"
                                f" to free it …"
                            )
                            _freed = _remote_free_base(
                                machine_obj.host, _rp_sh, repo_default_branch,
                            )
                            if _freed:
                                click.echo(
                                    "  base checkout freed — retrying launch …"
                                )
                                _rc2 = _tmux_launch(
                                    argv, effective_briefing, _sname,
                                    cwd=None, host=_tmux_host,
                                    raw_shell_cmd=_remote_cmd,
                                )
                                if _rc2 is None:
                                    click.echo(
                                        f"  error: could not create remote tmux"
                                        f" session on {machine_obj.host}"
                                        f" (retry after base-checkout free)",
                                        err=True,
                                    )
                                else:
                                    exit_code = _rc2
                                    if _tmux_alive(_sname, host=_tmux_host):
                                        click.echo(
                                            f"  session still running in remote"
                                            f" tmux: {_sname}\n"
                                            f"  reattach with:  coord reattach"
                                            f" {assignment_id}\n"
                                            "  (work commits are pushed when"
                                            " the session ends and the"
                                            " coordinator finalizes)"
                                        )
                                        sys.exit(0)
                                    if (
                                        exit_code == 0
                                        or _remote_wt_exists(
                                            machine_obj.host, _remote_wt
                                        )
                                    ):
                                        _wt_setup_ok_work = True  # retry succeeded
                            else:
                                click.echo("\n".join([
                                    _err_header,
                                    f"  base checkout is stuck on"
                                    f" {_remote_branch!r} —",
                                    f"  free it manually with:",
                                    f"    ssh {machine_obj.host}"
                                    f" 'git -C {_rp_sh} checkout"
                                    f" {repo_default_branch}'",
                                ]), err=True)
                        else:
                            # Dead orphan — safety-gated auto-prune-and-retry (#759).
                            _auto_pruned = False
                            if _remote_orphan_safe(
                                machine_obj.host, _rp_sh, _holder, _remote_branch,
                            ):
                                click.echo(
                                    f"  auto-pruning stale orphan {_holder}"
                                    f" on {machine_obj.host} …"
                                )
                                _auto_pruned = _remote_wt_remove(
                                    machine_obj.host, _rp_sh, _holder,
                                )
                            if _auto_pruned:
                                click.echo(
                                    "  stale orphan auto-pruned — retrying launch …"
                                )
                                _rc2 = _tmux_launch(
                                    argv, effective_briefing, _sname,
                                    cwd=None, host=_tmux_host,
                                    raw_shell_cmd=_remote_cmd,
                                )
                                if _rc2 is None:
                                    click.echo(
                                        f"  error: could not create remote tmux"
                                        f" session on {machine_obj.host}"
                                        f" (retry after prune)",
                                        err=True,
                                    )
                                else:
                                    exit_code = _rc2
                                    if _tmux_alive(_sname, host=_tmux_host):
                                        click.echo(
                                            f"  session still running in remote"
                                            f" tmux: {_sname}\n"
                                            f"  reattach with:  coord reattach"
                                            f" {assignment_id}\n"
                                            "  (work commits are pushed when"
                                            " the session ends and the"
                                            " coordinator finalizes)"
                                        )
                                        sys.exit(0)
                                    if (
                                        exit_code == 0
                                        or _remote_wt_exists(
                                            machine_obj.host, _remote_wt
                                        )
                                    ):
                                        _wt_setup_ok_work = True  # retry succeeded
                            else:
                                # Not safe or prune failed → manual command.
                                click.echo("\n".join([
                                    _err_header,
                                    "  stale worktree — prune it first:",
                                    f"    ssh {machine_obj.host} 'cd {_rp_sh}"
                                    f" && git worktree remove --force"
                                    f" {_shlex.quote(_holder)}'",
                                ]), err=True)
                else:
                    click.echo(
                        f"  error: setup failed — the remote worktree was "
                        f"never created (git worktree add refused on "
                        f"{machine_obj.host}).",
                        err=True,
                    )
            else:
                click.echo(
                    f"  claude exited with status {exit_code}", err=True,
                )

        # Remote finalize (#486d): push the work commits to origin/<branch>,
        # record the completion (so the pipeline advances + a re-review can
        # fire), and clean up the remote worktree.  Mirrors the remote-FIX
        # path; the only difference is the branch is the fresh feature
        # branch this work session created, not an existing one.
        # #982: same as the local finalize above — no pre-existing
        # smoke_tests for a fresh interactive work session, so this always
        # stashes the full repo-wide glob.
        try:
            _fr = finalize_remote_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo,
                repo_github=repo_cfg.github,
                issue_number=issue,
                machine_name=machine,
                ssh_target=machine_obj.host,
                remote_worktree_sh=_remote_wt,
                remote_repo_sh=_rp_sh,
                branch=_remote_branch,
                base_branch=repo_default_branch,
                exit_code=exit_code,
                started_at=started_at,
                artifact_paths=repo_cfg.artifact_paths,
            )
            if _fr.already_recorded:
                click.echo(
                    "  result recorded via `coord report-result`; remote "
                    "backstop did not overwrite"
                )
            else:
                click.echo(
                    f"  remote backstop: status={_fr.terminal_status} "
                    f"commits_ahead={_fr.commits_ahead} pushed={_fr.push_ok}"
                )
                if not _fr.push_ok and _wt_setup_ok_work:
                    click.echo(
                        f"  warning: remote push failed: {_fr.push_error}",
                        err=True,
                    )
                    click.echo(
                        f"  work commits preserved in {_remote_wt} on "
                        f"{machine_obj.host} (worktree NOT removed)",
                        err=True,
                    )
                # else _wt_setup_ok_work is False: setup never happened; error
                # already printed above — no "commits preserved" noise.
            _echo_artifact_stash(_fr)
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: remote backstop failed to record work exit: {exc}",
                err=True,
            )

        sys.exit(exit_code)


def _dispatch_headless(
    *,
    machine: str,
    repo: str,
    issue: int,
    briefing: str,
    model: str | None,
    dry_run: bool,
    plan_only: bool,
    no_plan: bool,
    force: bool,
    no_pull: bool,
    skip_freshness: bool,
    cfg: Config,
    machine_obj: object,
    repo_cfg: object,
    issue_data: dict,
    issue_title: str,
    driven_by: str | None = None,
    provider: str | None = None,
) -> None:
    """The plain (non --interactive) HTTP-dispatch path: build a Proposal,
    run the claim + dependency-freshness checks, POST to the agent server,
    and record + post the briefing.

    ``driven_by`` (#1499): durable provenance threaded from ``coord assign
    --driven-by`` (set by ``coord drive``'s work-stage dispatch). ``None``
    for a hand ``coord assign`` — the overwhelming majority of callers.

    ``provider`` (#1707): the raw ``--provider`` flag value, already
    validated against ``cfg.providers.definitions`` by ``assign()`` before
    this function runs. ``None`` (the default — no flag given) reproduces
    pre-#1707 behaviour exactly: ``Proposal.provider`` stays ``None`` and
    ``coord.dispatch.dispatch()``'s spec → repo → providers.default
    precedence chain falls through to the repo/global default unchanged.
    """
    from coord.board_service import read_board, write_board  # noqa: PLC0415
    from coord.dispatch import (  # noqa: PLC0415
        DispatchRefused,
        dispatch,
        post_briefing,
        resolve_dispatch_model_alias,
    )
    from coord.providers import resolve_provider_name  # noqa: PLC0415
    from coord.state import record_dispatched  # noqa: PLC0415


    # Build a Proposal inline
    from coord.models import Proposal

    # Resolve required_gates: check issue labels against pipeline.labels config,
    # fall back to pipeline.default_gates.
    issue_labels: list[str] = [
        lbl.get("name", "") for lbl in (issue_data.get("labels") or [])
    ]
    resolved_gates: list[str] = list(cfg.pipeline.default_gates)
    for lbl in issue_labels:
        if lbl in cfg.pipeline.labels:
            resolved_gates = list(cfg.pipeline.labels[lbl])
            break

    # Determine effective plan-only mode.
    # --plan-only always wins; --no-plan overrides dispatch.require_plan;
    # otherwise dispatch.require_plan sets the default.
    effective_plan_only = plan_only or (cfg.dispatch.require_plan and not no_plan)

    # #1707: resolve the effective provider BEFORE the model, so model
    # resolution can be provider-aware (#1706 review fix, below).
    # #1889: providers.labels (work dispatch only, mirrors models.labels'
    # #1430 gating) — must be resolved with the SAME issue_labels the model
    # resolution below uses, or the two could silently disagree about which
    # provider is effective.
    effective_provider_name = resolve_provider_name(
        provider, repo_cfg.provider, cfg.providers,
        issue_labels=issue_labels if not effective_plan_only else None,
    )

    # Resolve model: --model flag → models.labels (work dispatch only) →
    # the effective provider's own pinned model (non-claude/claude-pty
    # only) → config default. #1430: plan workers are read-only/cheap and
    # must not inherit a tier:large -> opus routing meant for the eventual
    # work dispatch, so label resolution only applies when this is a work
    # dispatch, not a plan-only one. #1706: `resolve_dispatch_model_alias`
    # is the single place the provider-aware exception to `models.default`
    # lives — see its docstring. Without this, `resolved_model` was always
    # truthy by the time it reached `Proposal.model`, which meant
    # `coord.dispatch.dispatch()`'s own (also provider-aware)
    # `definition.model` fallback could never fire for a plain `coord
    # assign` — the primary scenario #1706 was meant to unblock.
    label_model, matched_label, shadowed_labels = (
        cfg.models.model_for_labels_with_reason(issue_labels)
        if not effective_plan_only
        else (None, None, [])
    )
    resolved_model = resolve_dispatch_model_alias(
        explicit_model=model,
        label_model=label_model,
        config=cfg,
        effective_provider_name=effective_provider_name,
    )

    proposal = Proposal(
        id=0,
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=issue_title,
        rationale="manual assignment via coord assign",
        briefing=briefing,
        model=resolved_model,
        type="plan" if effective_plan_only else "work",
        required_gates=resolved_gates,
        issue_labels=issue_labels,
        driven_by=driven_by,
        provider=provider,
    )

    click.echo(f"{machine} → {repo} #{issue}: {issue_title}")
    if driven_by:
        click.echo(f"  driven by: {driven_by}")
    if effective_plan_only:
        if cfg.dispatch.require_plan and not plan_only:
            click.echo("  mode: plan-only (dispatch.require_plan=true; use --no-plan to override)")
        else:
            click.echo("  mode: plan-only (read-only, no worktree)")
    if resolved_model:
        # #1454: state *why* this model was picked — a silent fall-through
        # to models.default (stale/missing label) used to be indistinguishable
        # from an intentional default in this same line of output.
        from coord.config import describe_model_choice  # noqa: PLC0415

        click.echo(
            "  model: "
            + describe_model_choice(
                resolved_model=resolved_model,
                explicit_reason="explicit --model" if model else None,
                matched_label=matched_label,
                shadowed_labels=shadowed_labels,
            )
        )
    else:
        # #1706/#1798: resolved_model is None when the effective provider's
        # own `providers.definitions.<name>.model` is pinned and --model
        # wasn't given — state that explicitly instead of silently printing
        # nothing, so the operator can see --model was deliberately left
        # off the wire payload in favor of the pin. #1798: this now also
        # covers the case where a label DID match (e.g. `tier:small`) but
        # lost to the pin — surface that explicitly too, since a
        # namespace-mismatched label (a Claude alias) silently losing to a
        # non-claude provider's pin is exactly the fix this issue made.
        _pinned = cfg.providers.definitions.get(effective_provider_name)
        if _pinned is not None and _pinned.model:
            _via = f"providers.definitions[{effective_provider_name!r}].model"
            if matched_label:
                click.echo(
                    f"  model: {_pinned.model} (via {_via}, "
                    f"overriding label {matched_label!r})"
                )
            else:
                click.echo(f"  model: {_pinned.model} (via {_via})")

    # #1707: mirror the --model reasoning line above — state which link of
    # the spec (--provider) → providers.labels → repo (Repo.provider) →
    # providers.default chain (coord.providers.resolve_provider_name) won,
    # so a mixed claude/opencode fleet is legible at dispatch time
    # (including under --dry-run, before any of it is committed) instead of
    # only discoverable via coordinator.yml. #1889: issue_labels threaded
    # through so a providers.labels match (e.g. `harness:opencode`) is named
    # explicitly, not just inferred from source.
    from coord.providers import describe_provider_choice  # noqa: PLC0415

    click.echo(
        "  provider: "
        + describe_provider_choice(
            spec_provider=provider,
            repo_provider=repo_cfg.provider,
            providers_cfg=cfg.providers,
            issue_labels=issue_labels if not effective_plan_only else None,
        )
    )

    if dry_run:
        click.echo("  (dry run — not dispatched)")
        return

    # Claim check
    from coord.claim import claim_message, find_work_claim

    board = read_board()
    if not force:
        claim = find_work_claim(issue, repo, repo_cfg.github, board)
        if claim is not None:
            click.echo(
                f"  skipping: {claim_message(claim)}",
                err=True,
            )
            sys.exit(1)

    # #267: dependency freshness check — same machinery `coord approve`
    # uses.  Default for `coord assign` is `--auto-pull` (the manual /
    # right-click dispatch path is a deliberate user action; we want it
    # to be safe by default).  `--no-pull` falls back to the briefing
    # addendum; `--skip-freshness` bypasses entirely.
    # #268: `relevant_repos` covers both transitive `depends_on` (build
    # deps) and direct `reference_repos` (context).
    pull_repos: list[str] = []
    if not skip_freshness:
        from coord import freshness as _fresh  # noqa: PLC0415
        from coord.network import fetch_repos  # noqa: PLC0415

        agent_repos = fetch_repos(machine_obj) or {}

        repos_needed = _fresh.relevant_repos(proposal, cfg)
        github_heads: dict[str, str | None] = {}
        for dep_name, _kind in repos_needed:
            dep_cfg = cfg.repo(dep_name)
            if dep_cfg is None:
                github_heads[dep_name] = None
                continue
            try:
                github_heads[dep_name] = github_ops.get_default_branch_head(
                    dep_cfg.github, dep_cfg.default_branch
                )
            except RuntimeError as e:
                click.echo(
                    f"  warning: could not get HEAD of {dep_cfg.github}: {e}",
                    err=True,
                )
                github_heads[dep_name] = None

        freshness = _fresh.dependency_freshness(
            proposal, cfg, agent_repos, github_heads
        )
        needs = _fresh.stale_or_dirty(freshness)
        if needs:
            for f in needs:
                click.echo(
                    f"  dependency {f.repo_name}: {f.state}"
                    + (f" ({f.error})" if f.error else ""),
                )
            # #2804: never pull a build dep's single shared checkout out
            # from under some OTHER assignment on this machine that may be
            # building against it right now (see
            # `coord.freshness.repo_busy_elsewhere`'s docstring — this is
            # the same guard `coord approve` applies).
            busy = {
                f.repo_name
                for f in needs
                if f.kind == _fresh.BUILD
                and _fresh.repo_busy_elsewhere(f.repo_name, proposal.machine_name, cfg, board)
            }
            if busy:
                click.echo(
                    f"  not pulling (in use by a concurrent assignment on "
                    f"{proposal.machine_name}, #2804): {sorted(busy)}"
                )
            if not no_pull:
                pull_repos = [
                    f.repo_name for f in needs
                    if f.state == _fresh.STALE and f.repo_name not in busy
                ]
                if pull_repos:
                    click.echo(f"  will pull on agent before worker: {pull_repos}")
            else:
                addendum = _fresh.format_briefing_addendum(freshness, busy=busy)
                if addendum:
                    proposal.briefing = (proposal.briefing or "") + addendum

    # Dispatch to agent server
    try:
        response = dispatch(
            proposal, cfg, pull_repos=pull_repos, fresh_branch=force,
        )
    except httpx.HTTPError as e:
        click.echo(f"  dispatch failed: {e}", err=True)
        sys.exit(1)
    except DispatchRefused as e:
        # #1844: `enforce_oracle_readiness`/`enforce_epic_dispatch_guard`
        # raise THIS (a `ValueError` subclass, caught before the generic
        # branch below) specifically because their refusal is DETERMINISTIC —
        # nothing about retrying this exact command changes the condition
        # that refused it (no acceptance slice appears, no label gets added).
        # A distinct exit code is what lets `coord drive`'s own subprocess
        # call — and, through it, `coord drive-queue`'s tick — tell this
        # apart from a crash instead of retrying a guaranteed-to-fail
        # dispatch until `max_attempts` is exhausted (the #1817 overnight
        # incident this issue is named for).
        from coord.drive import EXIT_DISPATCH_REFUSED  # noqa: PLC0415

        click.echo(f"  dispatch failed: {e}", err=True)
        sys.exit(EXIT_DISPATCH_REFUSED)
    except ValueError as e:
        click.echo(f"  dispatch failed: {e}", err=True)
        sys.exit(1)

    assignment_id = response.get("id", "pending")
    click.echo(f"  dispatched (assignment {assignment_id})")

    # Record the dispatch
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
        provider_name=response.get("_provider_name"),
    )

    # Update board (read now so the briefing can see the full in-flight picture).
    board = read_board()

    # Post briefing to GitHub
    try:
        from coord.dispatch import compute_do_not_touch

        # #906: use board.active instead of load_dispatched() so a thin client
        # (empty local DB) still sees peer assignments running on other machines.
        # Exclude the just-dispatched assignment to avoid it listing itself.
        in_flight = [
            {"machine_name": a.machine_name, "repo_name": a.repo_name, "files_likely": a.files_allowed}
            for a in board.active
            if a.assignment_id != assignment_id
        ]
        do_not_touch = compute_do_not_touch(proposal, peers=[], in_flight=in_flight)
        post_briefing(proposal, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)
        click.echo("  briefing posted to GitHub")
    except Exception as e:
        click.echo(f"  briefing post failed: {e}", err=True)

    write_board(board)

    # Mark session start on first dispatch of the session
    from coord.state import load_session, write_session_start
    session = load_session()
    if session is None or session.get("clean_shutdown", True):
        write_session_start()