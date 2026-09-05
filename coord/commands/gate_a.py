"""``coord gate-a`` — record (or read) the human sign-off on a milestone's
Gate-A contract (#2063).

The sibling gate is enforced; this one was not. ``coord test --passed|--fail``
writes a verdict the pipeline holds review on
(``PipelineConfig.test_precedes_review``), whereas Gate A said only "merging
the Gate-A PR is sign-off" — a convention anything able to merge a PR
satisfies, including a coordinator session, silently, on CI green. It failed
on two consecutive coord-portal milestones (ms-1/PR #18, ms-2/PR #35): both
times the mocks were merged unseen, and the operator raised it themselves.

This command is the verdict half of the fix. The refusal half lives in
:func:`coord.milestone_dispatch.issue_oracle_ready` — deliberately at the
point where the contract is **consumed**, not at the merge, because the
Gate-A PR is merged with ``gh pr merge`` outside coord entirely.

Usage::

    coord gate-a acme-portal 17               # read the current verdict
    coord gate-a --approved acme-portal 17
    coord gate-a --changes acme-portal 17 --note "status vocabulary is wrong"

The verdict is keyed to the **content hash** of
``tests/acceptance/ms-NN/contract.md`` on the repo's default branch, so a
later ``coord acceptance mock ... --amend`` invalidates it automatically:
approving v1 must not silently approve v2.
"""

from __future__ import annotations

import getpass
import logging
import sys
from pathlib import Path

import click

from coord import gate_a as gate_a_mod
from coord.commands._common import _CONFIG_OPTION, _load_config

log = logging.getLogger(__name__)


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry in some containers
        return "unknown"


def _pending_amend_lines(repo_cfg, tracking_issue: int) -> list[str]:
    """#3065: also surface any unmerged Gate-A (`type="mock-author"`)
    branch for this tracking issue — the blind spot `evaluate()` cannot
    see, since it only ever compares the recorded approval against the
    contract on the default branch. A branch that's dispatched, reviewed,
    even approved, but not yet merged is invisible to that comparison, so
    an operator reading a clean "approved" here would have no way to know
    one exists.

    Best-effort and silent on failure (no board daemon reachable, no `gh`
    on PATH, a transient GitHub error): this is pure enrichment on top of
    the verdict `evaluate()` already computed, never a reason to fail the
    read.
    """
    try:
        from coord import board_service  # noqa: PLC0415
        from coord import github_ops  # noqa: PLC0415

        board = board_service.read_board()
        rows = list(board.active) + list(board.completed)
        pending = gate_a_mod.find_pending_amends(
            repo_name=repo_cfg.name,
            tracking_issue=tracking_issue,
            all_assignments=rows,
            is_merged=lambda branch: github_ops.pr_is_merged(
                repo_cfg.github, branch
            ),
        )
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        # #3065 review: silent-but-logged, not silent-and-invisible — a
        # real bug in this path (vs. a transient no-daemon/no-`gh`
        # condition) should leave a trace somewhere, even though it must
        # never fail the read itself.
        log.debug("pending-amend enrichment failed for #%s", tracking_issue, exc_info=True)
        return []
    return gate_a_mod.summarise_pending_amends(pending)


def _interactive_gap_lines(
    repo_cfg, config, milestone_number: int, contract_text: str
) -> list[str]:
    """#3131 review: surface interactive-control contract gaps on every
    read, the same neighborhood `_pending_amend_lines` surfaces pending
    amends in — without this, `gate_a.interactive_contract_gaps` /
    `summarise_interactive_gaps` were reachable from nowhere but their own
    tests, so a human running `coord gate-a --approved` on a milestone with
    an interactive mock would see nothing about an unpinned control: exactly
    the coord-portal#307 "control exists but goes nowhere" gap this module
    exists to catch. Returns `[]` (silently) whenever the repo's acceptance
    driver has no browser-viewable mock glob, or no mock in the bundle has
    any `:target` control — both true for every milestone today, since
    `INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED` stays off — so this is a
    zero-behavior-change addition until that flag flips.

    Same best-effort, silent-on-failure, purely-additive posture as
    `_pending_amend_lines`: a transient GitHub error here must never fail
    the read, only skip the enrichment.
    """
    try:
        from coord import mock_author  # noqa: PLC0415

        glob, _reason = mock_author.resolve_viewable_mock_glob(
            config.acceptance, repo_cfg.name
        )
        if glob is None:
            return []
        bundle = mock_author.collect_mock_bundle_files(
            repo_cfg.github, milestone_number, repo_cfg.default_branch, glob
        )
        mocks = {name: html for name, html in bundle.items() if name != "contract.md"}
        if not mocks:
            return []
        gaps = gate_a_mod.interactive_contract_gaps(mocks, contract_text)
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        log.debug(
            "interactive-contract-gap enrichment failed for ms-%s",
            milestone_number,
            exc_info=True,
        )
        return []
    return gate_a_mod.summarise_interactive_gaps(gaps)


def _fetch_contract(repo_cfg, config, milestone_number: int) -> str | None:
    """*milestone_number*'s Gate-A contract text, trying every acceptance
    search root *config* declares for *repo_cfg* (#2896) — a bare milestone
    number doesn't say whether it's a directory-discovered driver's slice
    (shared repo-root tree) or an entrypoint-linked driver's relocated one,
    so this checks each candidate :func:`coord.acceptance.
    gate_a_contract_candidates` returns until one exists."""
    from coord import github_ops  # noqa: PLC0415
    from coord.acceptance import gate_a_contract_candidates  # noqa: PLC0415

    for path in gate_a_contract_candidates(config, repo_cfg.name, milestone_number):
        try:
            return github_ops.get_repo_file(
                repo_cfg.github, path, branch=repo_cfg.default_branch,
            )
        except RuntimeError:
            continue
    return None


@click.command(
    "gate-a",
    help=(
        "Record the human sign-off on a milestone's Gate-A contract, or read "
        "the current verdict. Mirrors `coord test --passed|--fail`: nothing "
        "downstream of Gate A dispatches until a verdict exists for the "
        "contract's CURRENT content (#2063)."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--approved",
    "verdict",
    flag_value=gate_a_mod.VERDICT_APPROVED,
    help="A human read the rendered mock(s) + contract.md and signs off.",
)
@click.option(
    "--changes",
    "verdict",
    flag_value=gate_a_mod.VERDICT_CHANGES,
    help=(
        "A human read it and wants the contract changed — amend it with "
        "`coord acceptance mock <repo> <issue> --amend \"...\"`."
    ),
)
@click.option(
    "--note",
    default="",
    help="What you want changed (or any context worth keeping with the verdict).",
)
@_CONFIG_OPTION
def gate_a(
    repo: str,
    tracking_issue: int,
    verdict: str | None,
    note: str,
    config_path: Path,
) -> None:
    from coord import state  # noqa: PLC0415
    from coord import github_ops  # noqa: PLC0415
    from coord.audit import record_audit  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue)
    except RuntimeError as e:
        click.echo(f"error: could not fetch #{tracking_issue}: {e}", err=True)
        sys.exit(1)

    milestone_number = (issue_data.get("milestone") or {}).get("number")
    if milestone_number is None:
        click.echo(
            f"error: #{tracking_issue} has no milestone — Gate A is a "
            "milestone-level gate, so there is nothing to sign off on.",
            err=True,
        )
        sys.exit(2)
    milestone_number = int(milestone_number)

    contract_text = _fetch_contract(repo_cfg, cfg, milestone_number)
    if contract_text is None:
        from coord.acceptance import gate_a_contract_candidates  # noqa: PLC0415

        candidates = gate_a_contract_candidates(cfg, repo_cfg.name, milestone_number)
        named = " or ".join(repr(p) for p in candidates)
        click.echo(
            f"error: {named} does not exist "
            f"on {repo_cfg.github}@{repo_cfg.default_branch} yet — there is no "
            "contract to approve. Run `coord acceptance mock "
            f"{repo} {tracking_issue}` first, and merge its PR.",
            err=True,
        )
        sys.exit(1)

    sha = gate_a_mod.contract_digest(contract_text)

    # Read-only mode: no verdict flag => report where this milestone stands.
    if verdict is None:
        stored = state.get_gate_a_approval(
            repo_name=repo_cfg.name, milestone_number=milestone_number
        )
        decision = gate_a_mod.evaluate(
            repo_name=repo_cfg.name,
            milestone_number=milestone_number,
            contract_text=contract_text,
            approval=stored,
        )
        click.echo(
            f"Gate A ms-{milestone_number} ({repo}#{tracking_issue}): "
            f"{gate_a_mod.summarise(decision)}"
        )
        if decision.approval is not None and decision.approval.note:
            click.echo(f"  note: {decision.approval.note}")
        for line in _pending_amend_lines(repo_cfg, tracking_issue):
            click.echo(line)
        for line in _interactive_gap_lines(
            repo_cfg, cfg, milestone_number, contract_text
        ):
            click.echo(line)
        if not decision.ok:
            click.echo("")
            click.echo(decision.reason or "")
            sys.exit(1)
        return

    record = gate_a_mod.make_record(
        repo_name=repo_cfg.name,
        milestone_number=milestone_number,
        verdict=verdict,
        contract_sha=sha,
        tracking_issue=tracking_issue,
        note=note,
        actor=_actor(),
    )
    try:
        state.save_gate_a_approval(record.to_dict())
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: could not record Gate A verdict: {e}", err=True)
        sys.exit(1)

    record_audit(
        tier="business",
        category="gate",
        event_type=f"gate_a_{verdict}",
        actor=record.actor,
        summary=(
            f"Gate A {verdict} for {repo} ms-{milestone_number} "
            f"(contract {gate_a_mod.short_digest(sha)})"
            + (f": {note}" if note else "")
        ),
        repo=repo_cfg.name,
        issue=tracking_issue,
        details={
            "milestone_number": milestone_number,
            "contract_sha": sha,
            "verdict": verdict,
            "note": note,
        },
    )

    if verdict == gate_a_mod.VERDICT_APPROVED:
        click.echo(
            f"Gate A approved for {repo} ms-{milestone_number} "
            f"(contract {gate_a_mod.short_digest(sha)}) — this milestone's "
            "issues may now dispatch."
        )
        click.echo(
            "  An `--amend` to the contract invalidates this; re-approve after one."
        )
    else:
        click.echo(
            f"Gate A changes requested for {repo} ms-{milestone_number} "
            f"(contract {gate_a_mod.short_digest(sha)}) — dispatch stays refused."
        )
        click.echo(
            f'  Next: coord acceptance mock {repo} {tracking_issue} --amend "'
            f'{note or "<what to change>"}"'
        )
