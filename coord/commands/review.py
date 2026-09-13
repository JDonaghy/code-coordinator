"""Review-gate commands: `report-result`, `set-review-findings`,
`fix-briefing`, and the shared `_prompt_and_relay_review_verdict` helper
used by both the dispatch and sessions modules. Extracted from
coord/cli.py (#747)."""

from __future__ import annotations

import logging
import shlex
import sys
from pathlib import Path

import click


from coord.commands._common import _CONFIG_OPTION, _load_config

log = logging.getLogger(__name__)


def _collect_review_body_via_editor(
    *, assignment_id: str, summary: str, pre_body: str | None = None
) -> str | None:
    """Open ``$EDITOR`` for the operator to enter the full review findings (#617).

    Last-resort body capture for :func:`_prompt_and_relay_review_verdict` when
    neither a durable ``coord report-result`` nor the (remote-aware)
    transcript-floor produced the findings.  A ``request-changes`` verdict must
    never be recorded bodyless — the write seam refuses it and the fix worker
    would be dispatched with nothing to fix (#607) — so this collects the body
    the operator just wrote in the review session.

    When *pre_body* is provided it is used as the initial seed, so the operator
    edits or confirms already-recovered findings rather than typing from scratch
    (#877 editor-blank fix).

    Returns the entered body (template comment lines stripped) or ``None`` when
    empty / no editor available, so the caller can refuse + print the manual
    ``--body-file`` hint.
    """
    template = (
        "\n\n"
        f"# ── Review findings for assignment {assignment_id} ───────────────\n"
        "# Enter the full findings above (Markdown). Every BLOCKING item, with\n"
        "# file:line. This is exactly what the fix worker is briefed with, and\n"
        "# what the #603 per-issue context store records for every future\n"
        "# iteration on this issue.\n"
        "# Lines starting with '#' are ignored. Save an empty file to cancel.\n"
    )
    if pre_body and pre_body.strip():
        seed = pre_body.strip() + "\n" + template
    else:
        seed = (f"{summary.strip()}\n" if summary.strip() else "") + template
    try:
        edited = click.edit(seed)
    except Exception:  # noqa: BLE001 — no editor / editor failed → treat as cancel
        edited = None
    if not edited:
        return None
    body = "\n".join(
        ln for ln in edited.splitlines() if not ln.lstrip().startswith("#")
    ).strip()
    return body or None


def _prompt_and_relay_review_verdict(
    *,
    assignment_id: str,
    repo_name: str,
    repo_github: str,
    issue_number: int,
    machine_name: str,
    verdict_cmd_hint: str,
    started_at: float | None = None,
    ssh_target: str | None = None,
) -> bool:
    """Prompt the operator for a review verdict on exit and relay it (#486d / #877).

    Backstop used by BOTH interactive-review exit paths when the reviewer left
    without running `coord report-result` (local or remote — since #590 a
    remote `report-result` routes to the coordinator's shared DB via the daemon,
    so both paths *can* self-report; this prompt only fires when they didn't).

    Without it the verdict silently never reaches the merge gate and the
    Work→Review→Fix flow stalls.  Prompt the operator here (the terminal is a
    TTY) and relay through the same `issue_store` seam `coord report-result`
    uses — which itself routes to the daemon when `board_service` is set.

    **#877 board-content gate (primary)**: before prompting, read the assignment
    from the local DB for ``review_verdict`` + ``review_findings``.  When both
    are present the editor is NOT opened — the already-captured body is used
    directly and the prompt defaults to the captured verdict (not ``[s]``).
    This handles the status=failed-with-valid-verdict inconsistency where the
    board already captured the findings (e.g. via ``notify`` or a previous
    partial attempt) but ``already_recorded`` was False when ``finalize`` ran.

    **#877 remote-transcript backstop (secondary)**: when the board is empty and
    ``ssh_target`` is set, the remote transcript-floor is re-run against the
    session's own host before any editor is opened.  Findings recovered here
    also seed the editor so it is never blank.

    **#1348 parse-failure diagnostic**: when neither the board nor the transcript
    floor recovered a verdict, but a ``REVIEW_VERDICT:`` marker WAS found in a
    transcript that passes both attribution gates, the operator sees a distinct
    "REVIEW PARSE FAILED" message (not the generic "no verdict reported"), a
    greppable ``log.warning`` with host + path, and the editor opens pre-seeded
    with the recovered excerpt.  When the marker line carries a recognisable
    verdict word, the prompt defaults to it instead of ``[s]kip``.  The operator
    still confirms; nothing is auto-recorded.

    *started_at*: session start timestamp (epoch).  Passed to the transcript-
    floor so only transcripts active during this session are considered.  When
    ``None``, the remote scan uses a permissive ``cutoff=0`` (bounded solely by
    the issue-number gate) and the local diagnostic scan is skipped.

    *ssh_target*: SSH hostname of the machine where the review session ran.
    ``None`` for local sessions; set to ``machine.host`` for remote reviews.

    No-op that prints the manual hint when stdin isn't a TTY AND no pre-
    captured data is available (tests / headless invocations where no prior
    recording happened).
    Returns True when a verdict was recorded.
    """
    # ── Step 1: board-content gate (#877) ───────────────────────────────────
    # Check the local DB first (cheapest) for already-captured verdict+findings.
    # This is intentionally BEFORE the TTY check so headless callers also benefit
    # (auto-relay the captured data without prompting).
    _pre_verdict: str | None = None
    _pre_body: str | None = None
    # #1348: diagnostic out-parameter.  A non-None list activates unparsed-marker
    # detection in the transcript scans below; the first matching UnparsedReviewMarker
    # (newest transcript) is appended when the strict parse failed.
    _unparsed_markers: list = []
    try:
        from coord.state import load_assignment_review_findings  # noqa: PLC0415

        _cached = load_assignment_review_findings(assignment_id)
        if _cached is not None:
            _pre_verdict, _pre_body = _cached
    except Exception as exc:  # noqa: BLE001 — #1349: log + surface, don't swallow
        # A *read* failure (timeout/transport/auth) is NOT the same outcome as
        # "board genuinely has no verdict yet" — collapsing the two here would
        # reproduce the #1349 incident (a read failure silently masquerading as
        # "the agent never reported", re-prompting the operator with zero
        # evidence of which happened). Falling through to Step 2 / the prompt
        # below is still fine — the eventual write is idempotent — but it must
        # not happen silently.
        log.warning(
            "review-verdict board-content gate: could not read cached verdict/"
            "findings for assignment %s: %s", assignment_id, exc,
        )
        click.echo(
            f"  warning: could not read the board for a cached review verdict "
            f"on {assignment_id}: {exc}\n"
            "  This does NOT mean no verdict was captured — the read itself "
            "failed. Falling back to the prompt below.",
            err=True,
        )

    # ── Step 2: remote transcript-floor (#877) ──────────────────────────────
    # When board is empty and ssh_target is known, re-run the transcript-floor
    # against the session's own host.  Reuses the same floor that
    # finalize_interactive_exit already attempted — a 2nd pass here catches the
    # flush-race / timing window where the JSONL wasn't fully written yet when
    # finalize ran.  started_at=None falls through to cutoff=0 (all transcripts
    # in the remote listing) — still bounded by the issue-number gate.
    if _pre_verdict is None and ssh_target:
        try:
            from coord.interactive import (  # noqa: PLC0415
                _review_findings_from_transcript,
            )

            _tf = _review_findings_from_transcript(
                issue_number, started_at, assignment_id=assignment_id,
                ssh_target=ssh_target,
                _diagnostic=_unparsed_markers,  # #1348
            )
            if _tf is not None:
                _pre_verdict = _tf.verdict
                _pre_body = _tf.body
        except Exception:  # noqa: BLE001 — ssh unavailable → fall through
            pass

    # ── Step 2b: local transcript diagnostic scan (#1348) ───────────────────
    # For LOCAL sessions (no ssh_target) the transcript-floor already ran in
    # finalize_interactive_exit without diagnostic collection, so any unparsed
    # marker was silently missed.  Re-run here with _unparsed_markers to catch
    # the case — same 2nd-pass rationale as Step 2, same attribution gates,
    # same "first/newest hit only" semantics.  Skipped when started_at is None
    # (no bounded window → too likely to match a stale unrelated transcript).
    if _pre_verdict is None and not ssh_target and started_at is not None:
        try:
            from coord.interactive import (  # noqa: PLC0415
                _review_findings_from_transcript,
            )

            _tf2 = _review_findings_from_transcript(
                issue_number, started_at, assignment_id=assignment_id,
                ssh_target=None,
                _diagnostic=_unparsed_markers,  # #1348
            )
            if _tf2 is not None:
                _pre_verdict = _tf2.verdict
                _pre_body = _tf2.body
        except Exception:  # noqa: BLE001 — scan failed → fall through
            pass

    # ── #1348: parse-failure diagnostic ─────────────────────────────────────
    # No verdict recovered, but a REVIEW_VERDICT: marker WAS found in a
    # transcript that passed both attribution gates.  This is a different failure
    # from "no verdict reported": the review text IS there; the strict parser
    # rejected it (e.g. bolded markers, missing END_REVIEW — the #1346 incident).
    # Make the distinction operator-visible and surface the excerpt so the editor
    # is never blank.
    #
    # _marker_excerpt: excerpt from the unparsed marker, passed to the editor as
    # pre_body (distinct from _pre_body, which is a fully-captured body that
    # bypasses the editor — the excerpt must always open the editor so the
    # operator can clean it up).
    # _marker_default: normalised verdict for the prompt default.
    _marker_excerpt: str | None = None
    _marker_default: str | None = None
    if _pre_verdict is None and _unparsed_markers:
        _um = _unparsed_markers[0]
        _loc = _um.transcript_path or "(unknown path)"
        _host_desc = f" on {_um.host!r}" if _um.host else ""
        log.warning(
            "[#1348] review parse FAILED for assignment %s: "
            "REVIEW_VERDICT: marker found in transcript %r%s "
            "but _REVIEW_BLOCK_RE rejected it — surfacing excerpt for operator "
            "confirmation (detected verdict word: %r)",
            assignment_id, _loc, _host_desc, _um.verdict_word,
        )
        _inspect_hint = (
            f"ssh {_um.host} cat {shlex.quote(_loc)}" if _um.host
            else f"cat {shlex.quote(_loc)}"
        )
        click.echo(
            f"\n  ⚠  REVIEW PARSE FAILED\n"
            f"  Transcript:  {_loc}{_host_desc}\n"
            "  A REVIEW_VERDICT: marker was found in the above transcript but the\n"
            "  strict parser could not extract the findings (e.g. bolded markers\n"
            "  like **REVIEW_VERDICT:** or a missing END_REVIEW terminator).\n"
            "\n"
            "  This is NOT 'no verdict reported' — the review text IS present.\n"
            "  The excerpt has been pre-loaded into the editor for confirmation.\n"
            f"  To inspect the raw transcript:  {_inspect_hint}"
        )
        _marker_excerpt = _um.excerpt
        # Normalize the detected verdict word: aliases (pass→approve,
        # fail→request-changes) and canonical forms are both valid defaults.
        if _um.verdict_word:
            from coord.review import _VERDICT_ALIASES  # noqa: PLC0415
            _norm = _VERDICT_ALIASES.get(_um.verdict_word, _um.verdict_word)
            if _norm in ("approve", "request-changes"):
                _marker_default = _norm

    # ── Surface pre-captured findings ───────────────────────────────────────
    if _pre_verdict is not None:
        click.echo(f"\n  Captured review verdict: {_pre_verdict!r}")
        if _pre_body:
            _preview = _pre_body[:300].rstrip()
            if len(_pre_body) > 300:
                _preview += f"\n  … ({len(_pre_body)} chars total)"
            click.echo(f"  Findings preview: {_preview}\n")

    # ── Non-TTY path ─────────────────────────────────────────────────────────
    if not sys.stdin.isatty():
        if _pre_verdict is None:
            if _marker_excerpt is not None:
                # #1348: parse failed — different message from "no verdict".
                _um = _unparsed_markers[0]
                _loc = _um.transcript_path or "(unknown path)"
                _host_desc = f" on {_um.host!r}" if _um.host else ""
                click.echo(
                    f"  review parse FAILED (non-TTY): REVIEW_VERDICT: marker found in "
                    f"{_loc!r}{_host_desc} but strict parser rejected it "
                    f"(detected verdict: {_um.verdict_word!r}). "
                    f"Cannot open editor without a TTY. Recover manually with:\n"
                    f"{verdict_cmd_hint}"
                )
            else:
                click.echo(f"  no verdict reported — record it with:\n{verdict_cmd_hint}")
            return False
        # Headless + pre-captured: auto-relay (no prompt, no editor).
        # Applies to CI / daemon scenarios where both verdict AND body are
        # already in the board — no human input needed.
        if _pre_verdict == "request-changes" and not _pre_body:
            click.echo(
                "  headless: captured verdict is request-changes but findings body "
                f"is missing — record manually with:\n{verdict_cmd_hint}"
            )
            return False
        verdict: str = _pre_verdict
        summary: str = ""
        findings_body: str | None = _pre_body
    else:
        # ── TTY prompt ──────────────────────────────────────────────────────
        # Default: board verdict > detected verdict from unparsed marker > [s]kip.
        # _pre_verdict is the fully-parsed, confirmed one; _marker_default is the
        # marker-line word (only used when _pre_verdict is None — #1348).
        _default = {"approve": "a", "request-changes": "r"}.get(
            _pre_verdict or _marker_default or "", "s"
        )
        ans = click.prompt(
            "  Review verdict — [a]pprove / [r]equest-changes / [s]kip",
            type=click.Choice(["a", "r", "s"], case_sensitive=False),
            default=_default,
            show_choices=True,
        )
        verdict = {"a": "approve", "r": "request-changes"}.get(ans.lower(), "")
        if not verdict:
            click.echo(f"  skipped — record the verdict later with:\n{verdict_cmd_hint}")
            return False
        summary = click.prompt(
            "  one-line summary (optional, Enter to skip)", default="", show_default=False
        )

        # ── Findings body for request-changes ───────────────────────────────
        # #617: request-changes MUST carry the full findings body.
        # #877: when the board/transcript already has the body, use it directly
        # (no blank editor).  When missing, open the editor — seeded with:
        #   * the unparsed-marker excerpt (#1348), when the parse failed but
        #     we recovered text — so the operator edits what the reviewer wrote;
        #   * blank as a last resort, with a hint pointing at the transcript.
        findings_body = None
        if verdict == "request-changes":
            if _pre_body:
                # Pre-captured body — use it directly; editor not opened (#877).
                findings_body = _pre_body
                click.echo(
                    f"  Using {len(findings_body)}-char findings body from "
                    "board/transcript (editor not opened)."
                )
            else:
                # No pre-captured body — open editor, seeded with the recovered
                # marker excerpt if available (#1348), or blank otherwise.
                if _marker_excerpt:
                    # Parse-failure case: excerpt already printed above.  Editor
                    # opens with the recovered text so the operator can clean it up.
                    pass
                elif ssh_target:
                    click.echo(
                        f"  Findings not recovered from {ssh_target!r}. "
                        "Opening editor — enter the full review (every blocking "
                        "item, file:line)."
                    )
                    click.echo(
                        f"  To fetch the transcript manually: "
                        f"ssh {ssh_target} "
                        r"'find $HOME/.claude/projects -name \"*.jsonl\" "
                        r"| sort -rn | head -10'"
                    )
                else:
                    click.echo(
                        "  request-changes needs your full findings — opening an "
                        "editor (every blocking item, file:line)…"
                    )
                findings_body = _collect_review_body_via_editor(
                    assignment_id=assignment_id, summary=summary,
                    pre_body=_marker_excerpt,  # #1348: None when no marker was found
                )
                if not findings_body:
                    click.echo(
                        "  verdict NOT recorded: request-changes requires the findings "
                        "body — recording it without one would strand the fix worker "
                        "(#607). Record it when ready with:\n"
                        f"    coord report-result --assignment {assignment_id} "
                        "--status done --verdict request-changes "
                        f"--body-file /tmp/review-{assignment_id}.md",
                        err=True,
                    )
                    return False

    # ── Relay through issue_store seam ──────────────────────────────────────
    try:
        from coord import issue_store  # noqa: PLC0415

        outcome = issue_store.post_result(
            issue_store.ResultRecord(
                assignment_id=assignment_id,
                machine_name=machine_name,
                repo_name=repo_name,
                repo_github=repo_github,
                issue_number=int(issue_number),
                status="done",
                verdict=verdict,  # type: ignore[arg-type]  # narrowed to approve/request-changes above
                summary=summary,
                branch=None,
                findings_body=findings_body,
            )
        )
        click.echo(
            f"  verdict '{verdict}' recorded (posted_to_github={outcome.posted})."
        )
        if not outcome.findings_written:
            # #650: this is exactly the shape of the original incident — a
            # second exit-prompt capture for a review that already has
            # non-empty findings on the row.  The stored verdict already
            # matched (or `_persist_review_verdict` would have raised), so
            # the good findings were preserved — just tell the operator.
            click.echo(
                "  note: existing review findings were NOT overwritten "
                "(#650 clobber guard) — this assignment already had "
                "different, non-empty findings recorded; they are "
                "preserved. Use `coord report-result ... --force` if this "
                "overwrite was intentional."
            )
        if outcome.error:
            click.echo(f"  github post warning: {outcome.error}", err=True)
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; fall back to the hint
        click.echo(
            f"  warning: failed to record verdict inline: {exc}\n{verdict_cmd_hint}",
            err=True,
        )
        return False


def _prompt_and_relay_test_verdict(
    *,
    work_assignment_id: str,
    smoke_assignment_id: str,
    repo_name: str,
    repo_github: str,
    issue_number: int,
    machine_name: str,
    verdict_cmd_hint: str,
) -> bool:
    """Prompt the operator for a Test-gate verdict on exit and relay it (#923).

    Backstop for interactive SMOKE-OF sessions that exit without the agent
    running ``coord test --passed|--fail|--skipped``.  Without it the test
    verdict is silently lost, the Test box greys, and the merge gate blocks
    with no error message.

    **Idempotent**: reads ``work_assignment_id`` from the board first; when
    ``test_state`` is already set (the agent DID run ``coord test``), this
    function prints a confirmation and returns True immediately — no
    double-prompt.

    **#1349 point lookup**: the idempotency read is a single field off a
    single row, so it uses ``GET /assignment/{id}`` (point endpoint, ~2.7 KB)
    instead of the full ``GET /board`` collection (4.4 MB, 0.7-1.2s to build
    against a 5s client timeout) — mirrors
    :func:`coord.state.load_assignment_review_findings`'s point-lookup-first
    shape, including its 404 compatibility fallback to the collection for a
    pre-#1336 daemon.  A read *failure* here is logged and surfaced to the
    operator distinctly from "no verdict recorded" — see the incident writeup
    in #1349: silently falling through to the prompt on any exception erased
    the evidence of what actually happened.

    Mirrors :func:`_prompt_and_relay_review_verdict` (which handles the REVIEW
    stage).  Same contract for headless callers: no-op + hint when stdin is
    not a TTY.

    *work_assignment_id*: the WORK assignment being tested (passed as
    ``smoke_of`` at dispatch time).  The test verdict is ALWAYS recorded on
    the work row, not the smoke session row.

    *smoke_assignment_id*: the smoke session assignment id (used for log
    messages only).

    Returns True when a verdict was successfully recorded.
    """
    # Context label for the log lines below — identifies which repo/issue/
    # machine/smoke-session this backstop is acting on (#923 review nit: these
    # params were previously accepted but never read).
    _ctx = (
        f"{repo_name} ({repo_github}) issue #{issue_number} on {machine_name} "
        f"[smoke={smoke_assignment_id}]"
    )

    # ── Idempotency gate (#1349) ────────────────────────────────────────────
    # Read the WORK row (not the smoke session) and check test_state.  If it
    # already has test_state set the agent self-reported — do nothing.
    #
    # Point lookup first (#1349): a thin client hits GET /assignment/{id}
    # instead of paying for the full GET /board collection just to read one
    # field off one row — see load_assignment_review_findings for the model
    # this mirrors, including the 404 compatibility fallback for a
    # pre-#1336 daemon.  On the daemon host itself (no board_service
    # configured) the local DB is canonical and this isn't an HTTP round
    # trip at all, so the pre-existing local Board lookup is unchanged.
    #
    # A *read* failure must stay distinguishable from "board says no verdict"
    # — the old code was `except Exception: pass`, which turned a timeout /
    # transport / auth failure into the exact same outcome as "genuinely no
    # verdict yet", silently re-prompting the operator with zero evidence of
    # which happened. That is why the incident that motivated this fix
    # (operator re-typing a verdict 5s after `coord test --passed` had
    # already succeeded) could not be diagnosed from the logs afterwards —
    # the failure erased its own evidence. Re-prompting after a read failure
    # is still fine and safe (the write is idempotent); being SILENT about
    # it is the bug.
    _test_state: str | None = None
    _read_exc: Exception | None = None
    try:
        from coord.board_service import resolve as _resolve_board_service  # noqa: PLC0415

        _svc = _resolve_board_service()
        if _svc is not None:
            from coord.client import fetch_assignment, fetch_board_payload  # noqa: PLC0415

            _row = fetch_assignment(_svc, work_assignment_id)
            if _row is None:
                # 404: unknown id, or a pre-#1336 daemon (an unmatched route
                # is also a 404) — one compatibility pass through the
                # collection payload, exactly like load_assignment_review_findings.
                _payload = fetch_board_payload(_svc)
                _row = next(
                    (
                        a
                        for a in _payload.get("assignments", [])
                        if a.get("assignment_id") == work_assignment_id
                    ),
                    None,
                )
            if _row is not None:
                _test_state = (_row.get("test_state") or "").strip() or None
        else:
            from coord.board_service import read_board as _read_board_tv  # noqa: PLC0415

            _work = _read_board_tv().find_by_id(work_assignment_id)
            if _work is not None:
                _test_state = (_work.test_state or "").strip() or None
    except Exception as exc:  # noqa: BLE001 — #1349: log + surface, never swallow
        _read_exc = exc
        log.warning(
            "test-verdict idempotency gate: could not read assignment %s "
            "from the board: %s", work_assignment_id, exc,
        )

    if _test_state:
        click.echo(
            f"  test verdict already recorded: {_test_state!r} for "
            f"{_ctx} (agent used `coord test` — no operator prompt needed)"
        )
        return True

    if _read_exc is not None:
        click.echo(
            "  warning: could not read the board to check for an existing "
            f"test verdict for {_ctx}: {_read_exc}\n"
            "  This does NOT mean the agent failed to report — the read "
            "itself failed. Falling back to the prompt below; re-answering "
            "is safe (the write is idempotent).",
            err=True,
        )

    # ── Non-TTY path ──────────────────────────────────────────────────────────
    if not sys.stdin.isatty():
        click.echo(
            f"  no test verdict recorded for {_ctx} — record it with:\n"
            f"{verdict_cmd_hint}"
        )
        return False

    # ── TTY prompt ────────────────────────────────────────────────────────────
    ans = click.prompt(
        "  Test verdict — [p]assed / [f]ailed / [s]kip",
        type=click.Choice(["p", "f", "s"], case_sensitive=False),
        default="s",
        show_choices=True,
    )
    if ans.lower() == "s":
        click.echo(
            f"  skipped — record the test verdict later with:\n{verdict_cmd_hint}"
        )
        return False

    test_state = {"p": "passed", "f": "failed"}[ans.lower()]
    reason: str = ""
    if test_state == "failed":
        reason = click.prompt(
            "  failure reason (what was checked, expected vs actual, repro "
            "steps, suspected files — this IS the fix worker's brief)",
            default="",
            show_default=False,
        ).strip()

    # ── Relay via daemon-routed record_test_verdict ────────────────────────────
    try:
        from coord.state import record_test_verdict as _record_tv  # noqa: PLC0415

        _record_tv(
            assignment_id=work_assignment_id,
            test_state=test_state,
            test_reason=reason if reason else None,
            # Mirror to legacy smoke_test columns (pipeline.py reads both).
            smoke_test="pass" if test_state == "passed" else "fail",
            smoke_test_reason=reason if test_state == "failed" and reason else None,
        )
        click.echo(
            f"  test verdict '{test_state}' recorded for work assignment "
            f"{work_assignment_id} ({_ctx})."
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; fall back to the hint
        click.echo(
            f"  warning: failed to record test verdict for {_ctx}: {exc}\n"
            f"{verdict_cmd_hint}",
            err=True,
        )
        return False


@click.command(
    "report-result",
    help=(
        "Report the outcome of an interactive session through the "
        "coordinator's issue_store seam (#466). "
        "REQUIRED for review sessions where the verdict can only come "
        "from the agent."
    ),
)


@click.option(
    "--assignment", "assignment_id_opt", default=None,
    help="The assignment id (defaults to $COORD_ASSIGNMENT_ID).",
)


@click.option(
    "--status",
    type=click.Choice(["done", "blocked", "already-implemented"]),
    required=True,
    help=(
        "Terminal result: `done` = work landed; `blocked` = cannot proceed; "
        "`already-implemented` = nothing to do (advisory)."
    ),
)


@click.option(
    "--verdict",
    type=click.Choice(["approve", "request-changes"]),
    default=None,
    help=(
        "Review verdict — only meaningful for review sessions where no "
        "commits are pushed. Recorded so the merge-gate sees the same "
        "field a claude-p reviewer would have populated."
    ),
)


@click.option(
    "--summary", default="",
    help="One-paragraph summary posted on the issue under the result.",
)


@click.option(
    "--body-file", "body_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Path to a file with the FULL findings body (markdown). For a REVIEW "
        "session, write your complete review here and pass this — it is persisted "
        "on the assignment AND posted to the issue under a machine-parseable "
        "marker, so the fix worker is briefed with the actual findings (from any "
        "machine, via the GitHub message bus), not just the one-line --summary. "
        "REQUIRED with `--verdict request-changes` (#580)."
    ),
)


@click.option(
    "--body", "body_inline", default=None,
    help=(
        "Inline alternative to --body-file (the full findings body as a string, "
        "e.g. --body \"$(cat findings.md)\"). One of --body/--body-file is "
        "required with `--verdict request-changes`."
    ),
)


@click.option(
    "--audit-json", "audit_json_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Milestone Outcome Audit (#886 Phase 2) ONLY — path to a JSON file "
        "with the structured verdict: "
        '{"bottom_line": "...", "goals": [{"goal": "...", "metric_before": '
        '"...", "metric_after": "...", "verdict": "met|partial|gap", '
        '"evidence": "..."}]}. Routes the result through the audit dual-write '
        "path (assignment row + epic comment + #603 context store) instead of "
        "the generic done-comment body. Only valid on a --audit-of assignment."
    ),
)


@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Confirm overwriting already-captured review findings for this "
        "assignment (#650). Without it, a --verdict request-changes/--body "
        "write that would replace non-empty existing review_findings with a "
        "different body is refused — a second capture for the same "
        "assignment is, by construction, a re-run (finishing the exit "
        "process twice, a stray reattach), never a legitimate new review."
    ),
)


@click.option(
    "--verdict-source",
    type=click.Choice(["recovered", "overridden"]),
    default=None,
    help=(
        "#1956: state EXPLICITLY when --verdict is not the reviewer's own "
        "fresh self-report. 'recovered' = the reviewer reached this verdict "
        "and said so in prose, but never emitted the machine-readable "
        "REVIEW_VERDICT header — you rescued it from the transcript. "
        "'overridden' = a human is recording a DIFFERENT verdict than what "
        "the reviewer actually produced (or no reviewer verdict exists at "
        "all). Omit this for the normal case: an agent self-reporting its "
        "own session's verdict before exiting — that is recorded as "
        "'agent' automatically. Requires --verdict-reason."
    ),
)


@click.option(
    "--verdict-reason",
    default=None,
    help=(
        "Required with --verdict-source: a short justification for the "
        "recovery/override, so the provenance is auditable rather than "
        "reading identically to an agent-produced verdict (#1956)."
    ),
)


@_CONFIG_OPTION
def report_result(
    assignment_id_opt: str | None,
    status: str,
    verdict: str | None,
    summary: str,
    body_file: str | None,
    body_inline: str | None,
    audit_json_file: str | None,
    force: bool,
    verdict_source: str | None,
    verdict_reason: str | None,
    config_path: Path,
) -> None:
    """``coord report-result --assignment <id> --status <s> [--verdict <v>] --summary <text>``

    The single coordinator-mediated command an interactive Claude
    session may invoke before it exits.  Writes the outcome through the
    :mod:`coord.issue_store` seam (same path the git-floor backstop
    uses), so the GitHub message bus and the local DB see a
    structurally-identical completion regardless of which mechanism
    produced it.
    """
    import os as _os  # noqa: PLC0415

    from coord import issue_store  # noqa: PLC0415
    from coord.client import resolve_board_service  # noqa: PLC0415

    assignment_id = assignment_id_opt or _os.environ.get("COORD_ASSIGNMENT_ID")
    if not assignment_id:
        click.echo(
            "error: --assignment is required (or set $COORD_ASSIGNMENT_ID)",
            err=True,
        )
        sys.exit(2)

    # #886 Phase 2: Milestone Outcome Audit structured verdict. Parsed and
    # validated FIRST — before any board/config resolution — so an obvious
    # malformed-JSON typo fails fast without needing a reachable daemon/config
    # (issue_store._validate_result re-validates server-side too, the one seam
    # every caller funnels through, but this saves the agent a round trip).
    audit_goals: list[dict] | None = None
    audit_bottom_line: str | None = None
    if audit_json_file:
        import json as _json  # noqa: PLC0415

        try:
            audit_payload = _json.loads(Path(audit_json_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            click.echo(
                f"error: could not read/parse --audit-json {audit_json_file!r}: {exc}",
                err=True,
            )
            sys.exit(2)
        if not isinstance(audit_payload, dict) or not isinstance(
            audit_payload.get("goals"), list
        ):
            click.echo(
                "error: --audit-json must be a JSON object with a 'goals' list, "
                'e.g. {"bottom_line": "...", "goals": [{"goal": "...", '
                '"metric_before": "...", "metric_after": "...", "verdict": '
                '"met|partial|gap", "evidence": "..."}]}',
                err=True,
            )
            sys.exit(2)
        audit_goals = audit_payload["goals"]
        audit_bottom_line = audit_payload.get("bottom_line") or None
        for _goal in audit_goals:
            _verdict = _goal.get("verdict") if isinstance(_goal, dict) else None
            if _verdict not in ("met", "partial", "gap"):
                click.echo(
                    f"error: --audit-json goal {_goal!r} has invalid verdict "
                    f"{_verdict!r} (expected one of 'met'/'partial'/'gap')",
                    err=True,
                )
                sys.exit(2)

    repo_github: str | None = None
    repo_name: str | None = None
    machine_name: str | None = None
    issue_number: int | None = None
    branch: str | None = None

    svc = resolve_board_service()
    prefetch_failed = False
    if svc is not None:
        # Thin client (#590): no local DB/config — resolve the assignment's
        # identity from the daemon, then let issue_store.post_result route the
        # write back to the daemon's shared DB.  This is what lets a remote
        # interactive session self-report instead of the old "do NOT run
        # report-result" workaround.
        #
        # #1336: this prefetch is an *enrichment*, not a prerequisite — the
        # verdict write MUST NOT be discarded because a read was slow.  It
        # uses the point endpoint (GET /assignment/{id}; falls back to the
        # full /board payload only for a pre-#1336 daemon), gets the write
        # timeout (it rides a write operation), and on ANY failure we warn and
        # proceed with the POST: the daemon owns the DB and resolves the
        # identity fields server-side.
        from coord.client import (  # noqa: PLC0415
            _WRITE_TIMEOUT,
            fetch_assignment,
            fetch_board_payload,
        )

        row = None
        try:
            row = fetch_assignment(svc, assignment_id, timeout=_WRITE_TIMEOUT)
            if row is None:
                # 404: unknown id — or a daemon that predates the point
                # endpoint (an unmatched route is also a 404).  One
                # compatibility fallback through the collection payload.
                payload = fetch_board_payload(svc, timeout=_WRITE_TIMEOUT)
                row = next(
                    (
                        a
                        for a in payload.get("assignments", [])
                        if a.get("assignment_id") == assignment_id
                    ),
                    None,
                )
        except Exception as exc:  # noqa: BLE001
            prefetch_failed = True
            click.echo(
                f"warning: identity prefetch from {svc.url} failed ({exc}).\n"
                "  This is a slow/failed BOARD READ, not a write failure — "
                "proceeding to record the verdict; the board service resolves "
                "the assignment's repo/machine/issue itself (#1336).",
                err=True,
            )
        if row is not None:
            repo_github = row.get("repo_github")
            repo_name = row.get("repo_name")
            machine_name = row.get("machine_name")
            issue_number = row.get("issue_number")
            branch = row.get("branch")
    else:
        from coord.state import build_board, load_dispatched  # noqa: PLC0415

        cfg = _load_config(config_path)

        # Look up the assignment metadata.  Prefer the dispatched ledger
        # because it always has repo_github, then fall back to the live
        # board for in-flight rows that haven't been queried elsewhere.
        record = next(
            (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
            None,
        )
        if record is not None:
            repo_github = record.get("repo_github")
            repo_name = record.get("repo_name")
            machine_name = record.get("machine_name")
            issue_number = record.get("issue_number")

        board = build_board()
        assignment_obj = board.find_by_id(assignment_id)
        if assignment_obj is not None:
            repo_name = repo_name or assignment_obj.repo_name
            machine_name = machine_name or assignment_obj.machine_name
            issue_number = issue_number or assignment_obj.issue_number
            branch = assignment_obj.branch
            if repo_github is None:
                repo_cfg = cfg.repo(assignment_obj.repo_name)
                if repo_cfg is not None:
                    repo_github = repo_cfg.github

        # Final fallback: if a config repo matches the recorded repo_name,
        # use its github slug.
        if repo_github is None and repo_name is not None:
            repo_cfg = cfg.repo(repo_name)
            if repo_cfg is not None:
                repo_github = repo_cfg.github

    if not (repo_github and repo_name and machine_name and issue_number):
        if svc is not None and prefetch_failed:
            # #1336 invariant 4: a failed READ must never discard the WRITE.
            # Post the record with blank identity — the daemon fills it in
            # from its own assignments row (`_enrich_result_identity`).
            click.echo(
                "  identity unresolved locally — the board service will "
                "resolve it from its own DB.",
                err=True,
            )
        else:
            click.echo(
                f"error: could not resolve assignment {assignment_id!r} from "
                "board/dispatched ledger; pass --assignment with a known id "
                "or run from the originating coordinator machine.",
                err=True,
            )
            sys.exit(1)

    findings_body: str | None = None
    if body_file:
        try:
            findings_body = Path(body_file).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            click.echo(
                f"warning: could not read --body-file {body_file!r}: {exc}",
                err=True,
            )
    if findings_body is None and body_inline and body_inline.strip():
        findings_body = body_inline.strip()

    # #580: a request-changes verdict MUST carry the reviewer's findings.
    # Recording it with only a one-line --summary silently discards the
    # objections, so the iteration-N+1 fix agent gets dispatched with nothing
    # to fix. Require the body (file or inline) and fail loudly otherwise.
    if verdict == "request-changes" and not findings_body:
        click.echo(
            "error: --verdict request-changes requires the review body — pass "
            "--body-file <path> (or --body \"<text>\") with your full findings "
            "(every blocking item, file:line). The one-line --summary is not "
            "enough; it's what the fix worker is briefed with.\n"
            "  Write your findings to a file and re-run, e.g.:\n"
            f"  coord report-result --assignment {assignment_id} --status done "
            "--verdict request-changes --summary <one-line> "
            f"--body-file /tmp/review-{assignment_id}.md",
            err=True,
        )
        sys.exit(2)

    # #1956: fast client-side feedback for the same invariant
    # issue_store._validate_result enforces server-side — a relayed verdict
    # with no stated reason is indistinguishable from an agent's own, so
    # refuse it here before any network/board resolution happens.
    if verdict_source is not None and verdict is None:
        click.echo(
            "error: --verdict-source only makes sense alongside --verdict "
            "(it describes the provenance of the verdict being recorded).",
            err=True,
        )
        sys.exit(2)
    if verdict_source is not None and not (verdict_reason and verdict_reason.strip()):
        click.echo(
            f"error: --verdict-source {verdict_source!r} requires "
            "--verdict-reason — a relayed verdict must carry a reason so "
            "it's auditable, not silently read as agent-produced (#1956).",
            err=True,
        )
        sys.exit(2)

    # #949: push the worktree's commits BEFORE recording 'done' so completed
    # interactive work is never stranded.  report-result is the "done" path for
    # interactive fix/merge/work sessions; when a session declares done here
    # (rather than through finalize_interactive_exit, which is bypassed on a
    # detached/reattached tmux), nothing else pushes: the stale-session reaper
    # skips the already-'done' row, and the worker itself may have been told not
    # to push (e.g. a repo CLAUDE.md).  Without this, the commit sits unpushed in
    # ~/.coord/worktrees/<aid> and the next test/review agent tests stale code —
    # the confirmed root cause of #407 and #782.  Best-effort, non-force: it
    # fast-forwards real work and safely no-ops on a diverged/rebased branch (the
    # merge agent owns force-pushes).  Review sessions have no worktree, so the
    # .exists() guard skips them.
    if status == "done":
        from coord.interactive import _git_push  # noqa: PLC0415
        from coord.state import COORD_DIR  # noqa: PLC0415

        _wt = COORD_DIR / "worktrees" / assignment_id
        if _wt.exists():
            _pushed, _perr = _git_push(_wt)
            if _pushed:
                click.echo(f"  pushed {branch or 'HEAD'} to origin (#949)")
            else:
                click.echo(
                    f"  ⚠ WARNING: could not push commits for {assignment_id} "
                    f"to origin: {_perr}\n"
                    f"    work is committed but UNPUSHED in {_wt}\n"
                    f"    push manually:  git -C {_wt} push origin HEAD",
                    err=True,
                )

    record_obj = issue_store.ResultRecord(
        assignment_id=assignment_id,
        machine_name=machine_name or "",
        repo_name=repo_name or "",
        repo_github=repo_github or "",
        # 0 = "unresolved" (only reachable on the prefetch-failed thin-client
        # path above) — the daemon's identity enrichment treats falsy fields
        # as blanks to fill from its own assignments row.
        issue_number=int(issue_number) if issue_number else 0,
        status=status,  # type: ignore[arg-type]
        verdict=verdict,  # type: ignore[arg-type]
        summary=summary,
        branch=branch,
        findings_body=findings_body,
        audit_goals=audit_goals,
        audit_bottom_line=audit_bottom_line,
        allow_overwrite_findings=force,
        verdict_source=verdict_source,
        verdict_source_reason=verdict_reason,
    )
    try:
        outcome = issue_store.post_result(record_obj)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    except RuntimeError as exc:
        # #886/#990: the verdict write couldn't be durably confirmed (retries
        # exhausted, or a readback mismatch) — surface this loudly instead of
        # reporting success while the merge-gate-critical review_verdict (or
        # the audit run_number versioning invariant) column never actually
        # landed. Re-running the identical command is the recovery path.
        # (A #650 clobber-guard refusal is reported below via
        # `outcome.findings_written`, not as an exception — the verdict
        # itself still lands cleanly in that case.)
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    click.echo(
        f"result recorded: status={outcome.status} event={outcome.event} "
        f"posted_to_github={outcome.posted}"
    )
    if not outcome.findings_written:
        click.echo(
            "  note: review findings were NOT overwritten (#650 clobber "
            "guard) — non-empty findings already existed for this "
            "assignment and the stored verdict already matches; the "
            "previously captured findings are preserved. Re-run with "
            "--force if this overwrite was intentional."
        )
    if outcome.error:
        click.echo(f"  github post warning: {outcome.error}", err=True)


@click.command(
    "set-review-findings",
    help=(
        "Write review findings to the DB for a completed review assignment (#587). "
        "Used by the TUI rework dialog so the fix worker is briefed with the "
        "reviewer's feedback even when the review ran as a human-attended "
        "claude-pty session (which produces no parseable log)."
    ),
)


@click.argument("assignment_id")
@click.option(
    "--findings",
    required=True,
    help=(
        "The reviewer's findings, in plain text or markdown. Written as the "
        "REVIEW_BODY so `_load_review_findings` can serve it from the DB "
        "cache on the next `coord assign --fix-of` dispatch."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Confirm overwriting already-captured, non-empty review findings "
        "for this assignment (#650) — the TUI rework dialog's write is "
        "otherwise refused when the row already carries a different "
        "findings body, so a re-run of the dialog can't silently stomp a "
        "good capture with a placeholder."
    ),
)


@_CONFIG_OPTION
def set_review_findings(
    assignment_id: str,
    findings: str,
    force: bool,
    config_path: Path,
) -> None:
    """``coord set-review-findings <id> --findings <text>``

    Persist review findings for a human-attended (claude-pty) review whose
    verdict was already recorded via ``coord report-result --verdict
    request-changes``.  The DB cache written here is the first source
    ``_load_review_findings`` checks, so the subsequent ``coord assign
    --fix-of`` dispatch will read it and brief the fix worker correctly
    instead of emitting the "(No structured findings were captured)" fallback.

    #650: refuses to replace already-captured, non-empty findings with a
    different body unless ``--force`` is passed — a single assignment backs
    exactly one review, so a second, differing write here is a re-run of the
    dialog, never a legitimate new review.
    """
    from coord.state import update_assignment_review_findings  # noqa: PLC0415

    findings_text = findings.strip()
    if not findings_text:
        click.echo("error: --findings must not be empty", err=True)
        sys.exit(2)

    written = update_assignment_review_findings(
        assignment_id,
        verdict="request-changes",
        body=findings_text,
        allow_overwrite=force,
    )
    if not written:
        click.echo(
            f"refused: assignment {assignment_id} already has different, "
            "non-empty review findings recorded (#650 clobber guard) — "
            "re-run with --force to overwrite them.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"findings recorded for {assignment_id}")


@click.command("fix-briefing")
@click.argument("aid")
@_CONFIG_OPTION
def fix_briefing_cmd(aid: str, config_path: Path) -> None:
    """Print the briefing a `--fix-of <aid>` fix worker would receive — the
    per-issue context block + the resolved findings / test-failure story (#603).

    coord-tui shells out to this to preview the fix in the fail→fix / rework
    confirm dialog so the operator sees exactly what the worker is briefed with
    before launching.  Output is the briefing text ONLY (stdout).  AID is either
    a request-changes REVIEW id or a test-failed WORK id (mirrors --fix-of).
    """
    from types import SimpleNamespace

    from coord.auto_loop import _build_fix_briefing, _load_review_findings
    from coord.board_service import read_board
    from coord.state import COORD_DIR as _CTX_COORD_DIR, issue_context_block

    cfg = _load_config(config_path)
    board = read_board()
    target = board.find_by_id(aid)
    if target is None:
        click.echo(f"error: no assignment {aid} on the board.", err=True)
        sys.exit(2)

    # Mirror the --fix-of fork (cli.py): a test-failed WORK id fixes itself
    # (findings = test_reason); a request-changes REVIEW id fixes its linked work.
    fix_from_test_fail = (
        target.type != "review" and getattr(target, "test_state", None) == "failed"
    )
    if fix_from_test_fail:
        work = target
    elif target.type == "review":
        work = (
            board.find_by_id(target.review_of_assignment_id)
            if target.review_of_assignment_id else None
        )
    else:
        click.echo(
            f"error: {aid} is not a fixable target "
            f"(type={target.type!r}, test_state={getattr(target, 'test_state', None)!r}).",
            err=True,
        )
        sys.exit(2)
    if work is None or not work.branch:
        click.echo("error: no linked work assignment with a branch to fix.", err=True)
        sys.exit(2)

    repo_cfg = cfg.repo(work.repo_name)
    repo_github = repo_cfg.github if repo_cfg else work.repo_name
    # #3322: shared with every other fix door — the max round already spent
    # on this (repo, issue, branch), not just `work`'s own counter.
    from coord.auto_loop import next_fix_iteration as _next_fix_iter  # noqa: PLC0415

    next_iteration = _next_fix_iter(board, work)
    max_iter = cfg.pipeline.max_review_iterations
    if fix_from_test_fail:
        # #1337: the board wire carries a bounded PREVIEW of test_reason; the
        # briefing quotes it verbatim — read full text via the detail endpoint.
        from coord.state import load_assignment_test_reason as _load_tr  # noqa: PLC0415

        story = (
            _load_tr(work.assignment_id or "")
            or getattr(work, "test_reason", None)
            or ""
        ).strip()
        findings_body = (
            "The manual smoke test FAILED. The operator reported:\n\n"
            f"> {story}\n\nReproduce the failure, fix the root cause, and "
            "re-validate before pushing."
            if story else
            "The manual smoke test FAILED (no reason text was recorded). Pull the "
            "branch, reproduce the failure the operator hit, and fix the root "
            "cause before pushing."
        )
    else:
        _log = _CTX_COORD_DIR / "logs" / f"{aid}.log"
        try:
            findings = _load_review_findings(
                target, str(_log) if _log.exists() else None, None,
                repo_github=repo_github,
            )
        except Exception:  # noqa: BLE001
            findings = None
        findings_body = (
            findings.body.strip()
            if findings and (getattr(findings, "body", "") or "").strip()
            else (
                f"(No structured findings were captured for review {aid}.) "
                f"The review verdict was {target.review_verdict or 'request-changes'!r}. "
                "Read the reviewer's feedback and address every blocking item "
                "before pushing."
            )
        )
    fix_briefing = _build_fix_briefing(
        work, SimpleNamespace(body=findings_body), next_iteration, max_iter
    )
    ctx = issue_context_block(work.repo_name, work.issue_number)
    click.echo(ctx + fix_briefing, nl=False)


def _count_diff_changed_lines(diff_text: str) -> int:
    """Count content +/- lines in a unified diff, excluding the ``+++``/``---``
    file-header lines (which start with the same characters but carry no
    content). Used by ``review-reaffirm`` to size the delta against the
    ``reviews.reaffirm_max_diff_lines`` sanity bound.

    The header exclusion is **positional**, not a bare prefix test: a header
    only ever appears outside a hunk body (before the file's first ``@@``),
    so once a ``@@`` hunk header is seen every ``+``/``-`` line counts until
    the next ``diff --git`` resets the state. A naive ``startswith("---")``
    silently drops genuine content whose own source text begins with
    ``--``/``++`` — a removed Markdown/YAML ``---`` separator renders as
    ``----``, an added ``++counter`` renders as ``+++counter`` — which would
    *undercount* the delta and let a diff that should be hard-refused slip
    under the bound. Where the input isn't recognizable as a unified diff
    (no ``@@`` at all) this errs toward over-counting, which fails closed.
    """
    n = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if not in_hunk and (
            line.startswith("--- ") or line.startswith("+++ ")
            or line in ("---", "+++")
        ):
            continue  # unified-diff file header (never inside a hunk body)
        if line.startswith("+") or line.startswith("-"):
            n += 1
    return n


@click.command(
    "review-reaffirm",
    help=(
        "Re-point a stale-but-content-changed approved review to the branch's "
        "current head, with an audited reason (#1488) — the sanctioned "
        "alternative to a full re-review or leaving the merge queue for a "
        "mechanical conflict-resolution delta."
    ),
)
@click.argument("work_assignment_id")
@click.option(
    "--reason",
    required=True,
    help=(
        "Why this delta is safe to reaffirm without a full re-review "
        "(e.g. 'conflict resolution: merged #1461 + #1472 filters, suite "
        "green'). Written to the audit trail alongside the SHA anchors."
    ),
)
@click.option(
    "--yes", is_flag=True, default=False,
    help="Skip the interactive confirmation prompt (for scripting).",
)
@_CONFIG_OPTION
def review_reaffirm(
    work_assignment_id: str, reason: str, yes: bool, config_path: Path,
) -> None:
    """``coord review-reaffirm <work_assignment_id> --reason "..."``

    #1488: a content-changing rebase (typically a conflict resolution during
    `coord merge`) correctly voids `has_approved_review`'s approval — but
    until now the only sanctioned way past that was a full re-review
    dispatch, or leaving the merge queue entirely via `gh pr merge` (which
    left no board-visible record at all). This re-points the approving
    review row's `review_head_sha`/`review_patch_id` to the branch's current
    head instead, after showing the exact delta being waved through and
    requiring confirmation — refusing outright (no override flag) when the
    delta exceeds `reviews.reaffirm_max_diff_lines`, so this stays an escape
    hatch for mechanical resolutions, never a review bypass.

    Reuses the exact same eligibility checks `dispatch_scoped_reviews_for_queue`
    (#1476) uses to decide a *scoped re-review* is safe to dispatch —
    `coord.merge_queue.has_approved_review`, `find_scoped_review_candidate`
    **and** the `only_conflict_fix_since_review` guardrail — so this command
    takes the human-in-the-loop path through that same gap instead of
    dispatching another `claude -p` review. It splits that guardrail's two
    failure modes: work/fix rounds dispatched after the approval are refused
    outright, while a delta no coord-tracked conflict-fix can account for
    (a hand-run rebase) warns loudly and is stamped as unattributed in the
    audit row.
    """
    from coord import github_ops  # noqa: PLC0415
    from coord import merge_queue as mq  # noqa: PLC0415
    from coord.board_service import read_board  # noqa: PLC0415
    from coord.state import record_review_reaffirm  # noqa: PLC0415

    if not reason.strip():
        click.echo("error: --reason must not be empty", err=True)
        sys.exit(2)

    cfg = _load_config(config_path)
    board = read_board()
    work = board.find_by_id(work_assignment_id)
    if work is None:
        click.echo(f"error: assignment {work_assignment_id!r} not found on the board", err=True)
        sys.exit(1)
    if not work.branch:
        click.echo(f"error: assignment {work_assignment_id!r} has no branch recorded", err=True)
        sys.exit(1)

    repo = cfg.repo(work.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {work.repo_name!r}", err=True)
        sys.exit(1)

    # Reuse a live merge-queue entry when one exists (the common case — the
    # operator got here because `coord merge` just refused with
    # review_required); synthesize a throwaway one otherwise. Either way
    # branch_head_sha/branch_patch_id are never trusted from load_queue()
    # (they're transient, recomputed by `process()` on every run, never
    # persisted) — always refetched live below so the gate is evaluated
    # against the branch's ACTUAL current head, not a stale in-memory value.
    items = mq.load_queue()
    entry = next(
        (e for e in items if e.repo_github == repo.github and e.branch == work.branch),
        None,
    )
    if entry is None:
        entry = mq.QueuedMerge(
            assignment_id=work.assignment_id,
            repo_name=work.repo_name,
            repo_github=repo.github,
            branch=work.branch,
            target_branch=repo.default_branch,
            issue_number=work.issue_number,
            issue_title=work.issue_title or "",
        )

    entry.branch_head_sha = github_ops.get_branch_sha(entry.repo_github, entry.branch)
    if entry.branch_head_sha is None:
        click.echo(
            f"error: could not resolve the current HEAD sha for branch "
            f"{entry.branch!r} (gh api failure) — refusing to reaffirm "
            f"without confirming the branch's actual current state",
            err=True,
        )
        sys.exit(1)
    entry.branch_patch_id = None  # force a fresh backfill below, never trust a stale value

    if mq.has_approved_review(entry, board, github_ops):
        click.echo(
            "nothing to reaffirm — an approved review already covers the "
            "branch's current head (either fresh, or a content-identical "
            "rebase already carried forward by #1475)."
        )
        return

    prior_review = mq.find_scoped_review_candidate(entry, board, github_ops)
    if prior_review is None:
        click.echo(
            "error: no reaffirmable approval found — either this branch was "
            "never reviewed+approved, or the delta since its last approval "
            "can't be confirmed (missing patch-id data). A full re-review is "
            f"required: coord review {work_assignment_id}",
            err=True,
        )
        sys.exit(1)

    # #1488 review round 1: `find_scoped_review_candidate` alone does NOT
    # establish that the delta is a mechanical rebase — its "voided ONLY by a
    # content-changing rebase" guarantee comes from the caller pairing it with
    # `only_conflict_fix_since_review` (exactly what the automated scoped
    # dispatcher does at coord/review.py's "guardrail: another commit
    # intervened" check). Without it, a bounce round carrying a genuine second
    # batch of new logic looks identical to a conflict resolution here, and
    # would ride through on one y/n keystroke.
    #
    # The guardrail's two failure modes are NOT equivalent, so they're split:
    #   * intervening work/fix rounds dispatched after the approval ⇒ new
    #     logic the approval provably never saw ⇒ hard refuse, no override.
    #   * no coord-tracked conflict-fix explains the delta (the operator
    #     rebased by hand — the single most common way to land here, and the
    #     exact gap this escape hatch exists to fill) ⇒ unattributable but not
    #     evidence of new logic ⇒ loud warning above the confirm prompt, and
    #     the fact is stamped into the audit row so the trail records whether
    #     coord could attribute the delta or the human vouched for it alone.
    intervening = mq.intervening_work_since_review(entry, board, prior_review)
    if intervening:
        listed = ", ".join(
            f"{a.assignment_id} ({a.type})" for a in intervening[:5]
        )
        click.echo(
            f"error: {len(intervening)} work/fix assignment(s) were dispatched "
            f"AFTER review {prior_review.assignment_id} approved this branch: "
            f"{listed} — the delta is new logic that approval never saw, not a "
            f"mechanical conflict resolution. Reaffirmation is refused (no "
            f"override flag). Dispatch a full re-review instead: "
            f"coord review {work_assignment_id}",
            err=True,
        )
        sys.exit(1)

    conflict_fix_only = mq.only_conflict_fix_since_review(entry, board, prior_review)

    old_sha = prior_review.review_head_sha
    new_sha = entry.branch_head_sha
    diff_text = github_ops.get_compare_diff(entry.repo_github, old_sha, new_sha)
    if diff_text is None:
        click.echo(
            f"error: could not fetch the diff between the approved sha "
            f"{old_sha!r} and the current head {new_sha!r} (gh api compare "
            f"failed) — refusing to reaffirm without being able to show "
            f"what's being waved through",
            err=True,
        )
        sys.exit(1)

    diff_lines = _count_diff_changed_lines(diff_text)
    max_lines = cfg.reviews.reaffirm_max_diff_lines
    if max_lines > 0 and diff_lines > max_lines:
        click.echo(
            f"error: delta is {diff_lines} changed lines, exceeding "
            f"reviews.reaffirm_max_diff_lines ({max_lines}) — this is an "
            f"escape hatch for mechanical conflict resolutions, not a review "
            f"bypass. Dispatch a full re-review instead: "
            f"coord review {work_assignment_id}",
            err=True,
        )
        sys.exit(1)

    click.echo(
        f"Reaffirming review {prior_review.assignment_id} for "
        f"{work.repo_name}#{work.issue_number} ({work.branch})"
    )
    click.echo(f"  approved sha:  {old_sha}")
    click.echo(f"  current head:  {new_sha}")
    click.echo(f"  delta:         {diff_lines} changed lines")
    click.echo(f"  reason:        {reason}")
    click.echo(
        "  attribution:   "
        + (
            "a completed conflict-fix accounts for this delta"
            if conflict_fix_only
            else "UNATTRIBUTED — no coord-tracked conflict-fix explains it"
        )
    )
    click.echo()
    click.echo(github_ops.truncate_diff_text(diff_text))
    click.echo()
    if not conflict_fix_only:
        click.echo(
            "WARNING: coord has no completed conflict-fix on record for this "
            "branch since the approval, so it cannot confirm the delta above "
            "is only a mechanical rebase resolution. Nothing intervened on the "
            "board (that would have been refused outright) — but you are "
            "vouching for this diff yourself. Read every line before "
            "confirming.",
            err=True,
        )
        click.echo()

    if not yes and not click.confirm(
        "Reaffirm this approval to cover the current head?"
    ):
        click.echo("aborted — approval NOT reaffirmed.")
        sys.exit(1)

    new_patch_id = github_ops.get_branch_patch_id(
        entry.repo_github, entry.target_branch, entry.branch
    )
    try:
        record_review_reaffirm(
            review_assignment_id=prior_review.assignment_id,
            new_head_sha=new_sha,
            new_patch_id=new_patch_id,
            reason=reason,
            conflict_fix_only=conflict_fix_only,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: reaffirm write failed: {e}", err=True)
        sys.exit(1)

    click.echo(
        f"Reaffirmed: review {prior_review.assignment_id} now covers "
        f"{new_sha[:12]} — {work.repo_name}#{work.issue_number} is unblocked "
        f"for merge."
    )
