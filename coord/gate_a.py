"""Gate A human sign-off (#2063) — the recorded verdict on a milestone's
black-box contract.

``docs/ORACLE_LOOP.md`` Phase 0 says a human signs off on the Gate-A mock +
``contract.md`` before anything downstream is built against it. Until #2063
that was a *convention*: "merging the Gate-A PR is sign-off". Anything that
can merge a PR satisfied it — including a coordinator session, silently, on
CI green — and nothing downstream ever checked. It failed twice on
consecutive coord-portal milestones (ms-1 / PR #18, ms-2 / PR #35).

This module is the durable half of the fix, mirroring the sibling gate that
*is* enforced (``coord test --passed|--fail`` → ``PipelineConfig.
test_precedes_review``):

- :class:`GateAApproval` — one board-recorded verdict per
  ``(repo_name, milestone_number)``, persisted by
  :func:`coord.state.save_gate_a_approval` under the ``gate_a_approvals``
  ``board_meta`` key (same seam and shape as ``milestone_gates``, #1929).
- :func:`contract_digest` — the verdict is keyed to the **content** of
  ``tests/acceptance/ms-NN/contract.md``, so ``coord acceptance mock
  --amend`` automatically invalidates a prior approval. Approving v1 must
  not silently approve v2 — that is the same failure mode this issue is
  about, one level up.
- :func:`evaluate` — the pure "may work dispatch against this contract"
  decision consumed by
  :func:`coord.milestone_dispatch.issue_oracle_ready` (#1138), which is
  where the refusal actually bites. Enforcing at the *consumer* rather than
  at the merge is deliberate: the Gate-A PR is merged with ``gh pr merge``,
  outside coord entirely, so no coord-side check ever sees it.
- :func:`park_marker` / :func:`parse_park_marker` — the machine-readable
  tag carried in the refusal prose so ``coord drive-queue``'s tick can
  **park** (re-checked every tick, #1891/#1892) rather than **block** (a
  terminal state nothing re-evaluates, #2040) on an unapproved contract.
  This is explicitly an operator-fixable condition with a one-command
  remedy; a queue entry that went terminal here would stay dead after the
  human approved.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

__all__ = [
    "VERDICT_APPROVED",
    "VERDICT_CHANGES",
    "VERDICTS",
    "STATE_APPROVED",
    "STATE_MISSING",
    "STATE_STALE",
    "STATE_CHANGES",
    "STATE_EXEMPT",
    "GateAApproval",
    "GateADecision",
    "contract_digest",
    "short_digest",
    "evaluate",
    "summarise",
    "make_record",
    "approval_fingerprint",
    "NO_VERDICT",
    "park_marker",
    "parse_park_marker",
    "is_gate_a_refusal_reason",
    "PendingAmend",
    "find_pending_amends",
    "summarise_pending_amends",
]

#: The two verdicts an operator can record, mirroring ``coord test
#: --passed|--fail``. ``changes`` is not merely "not approved": it is a
#: recorded *rejection*, so the refusal message can say "you asked for
#: changes — re-run `coord acceptance mock --amend`" rather than the far
#: less useful "nobody has looked yet".
VERDICT_APPROVED = "approved"
VERDICT_CHANGES = "changes"
VERDICTS: frozenset[str] = frozenset({VERDICT_APPROVED, VERDICT_CHANGES})

#: :attr:`GateADecision.state` values.
STATE_APPROVED = "approved"  # a human signed off on exactly this contract
STATE_MISSING = "missing"  # contract exists, nobody has recorded anything
STATE_STALE = "stale"  # approved, but the contract changed since (an --amend)
STATE_CHANGES = "changes"  # a human read it and asked for changes
STATE_EXEMPT = "exempt"  # the milestone declared it needs no human eye

_SCHEMA = 1


def contract_digest(text: str | bytes) -> str:
    """Stable content hash of a ``contract.md``.

    Line endings are normalised and trailing whitespace on the document is
    stripped before hashing, so a CRLF checkout or an editor that adds a
    final newline does not silently invalidate an approval — but any change
    to the *pinned surface* (button text, ``data-testid`` hooks, status
    vocabulary) does. That asymmetry is the whole point: those strings
    become assertions in a sealed suite the worker may never edit, so an
    ``--amend`` that rewords one of them must force a fresh look.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    normalised = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def short_digest(sha: str | None) -> str:
    """First 12 hex chars of *sha* — what humans see in messages/UI."""
    return (sha or "")[:12] or "?"


@dataclass(frozen=True)
class GateAApproval:
    """One recorded human verdict on a milestone's Gate-A contract.

    Stored as a plain JSON dict (:meth:`to_dict`) in the
    ``gate_a_approvals`` ``board_meta`` list, keyed on
    ``(repo_name, milestone_number)``. ``contract_sha`` is what makes the
    verdict *specific*: :func:`evaluate` compares it against the contract
    live on the default branch and downgrades a mismatch to
    :data:`STATE_STALE`.
    """

    repo_name: str
    milestone_number: int
    verdict: str = VERDICT_APPROVED
    contract_sha: str = ""
    tracking_issue: int | None = None
    note: str = ""
    actor: str = ""
    recorded_at: float = 0.0
    schema: int = _SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "milestone_number": self.milestone_number,
            "verdict": self.verdict,
            "contract_sha": self.contract_sha,
            "tracking_issue": self.tracking_issue,
            "note": self.note,
            "actor": self.actor,
            "recorded_at": self.recorded_at,
            "schema": self.schema,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "GateAApproval | None":
        """Tolerant decode — ``None`` for anything this build can't read.

        Same posture as :meth:`coord.milestone_gate.GateRecord.from_dict`: a
        record written by a newer schema, or a corrupt one, degrades to "no
        approval recorded" (which *refuses*, safely) rather than crashing
        dispatch.
        """
        if not isinstance(raw, dict):
            return None
        if int(raw.get("schema", _SCHEMA) or _SCHEMA) != _SCHEMA:
            return None
        repo_name = raw.get("repo_name")
        if not isinstance(repo_name, str) or not repo_name:
            return None
        milestone = _as_int(raw.get("milestone_number"))
        if milestone is None:
            return None
        verdict = str(raw.get("verdict") or VERDICT_APPROVED)
        if verdict not in VERDICTS:
            return None
        return cls(
            repo_name=repo_name,
            milestone_number=milestone,
            verdict=verdict,
            contract_sha=str(raw.get("contract_sha") or ""),
            tracking_issue=_as_int(raw.get("tracking_issue")),
            note=str(raw.get("note") or ""),
            actor=str(raw.get("actor") or ""),
            recorded_at=float(raw.get("recorded_at") or 0.0),
        )


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class GateADecision:
    """The pure verdict on "may anything be built against this contract".

    ``ok`` is ``True`` iff a human signed off on *exactly* the contract
    currently on the default branch (or the milestone declared itself
    exempt). ``reason`` is ``None`` iff ``ok``.
    """

    state: str = STATE_MISSING
    ok: bool = False
    contract_sha: str = ""
    approval: GateAApproval | None = None
    reason: str | None = None
    #: The manifest's ``gate_a: {exempt: true, reason: "..."}`` text, when
    #: ``state`` is :data:`STATE_EXEMPT` — ``""`` otherwise (including when
    #: exempt but no reason was declared). The Proposed-shape item this
    #: implements calls the opt-out "explicit and declared... reviewable" —
    #: that only holds if the reason an operator wrote down is actually
    #: surfaced somewhere (``coord gate-a``, the TUI), not just parsed and
    #: discarded.
    exempt_reason: str = ""


def make_record(
    *,
    repo_name: str,
    milestone_number: int,
    verdict: str,
    contract_sha: str,
    tracking_issue: int | None = None,
    note: str = "",
    actor: str = "",
    now: float | None = None,
) -> GateAApproval:
    """Build a verdict record, stamping ``recorded_at``.

    ``verdict`` must be one of :data:`VERDICTS`; anything else is a
    programming error, not an operator error (the CLI's mutually-exclusive
    ``--approved``/``--changes`` flags already reject bad input).

    **Open policy question (#2509, flagged for the operator, not resolved by
    this call or any caller yet):** should an "approved" verdict on the
    customer portal auto-call this with ``verdict=VERDICT_APPROVED`` — i.e.
    does a client's portal sign-off record itself — or does an operator
    still confirm separately via the existing CLI/TUI path? Today nothing
    does the former: ``coord.portal_sync._consume_verdicts`` only acts on
    `changes-requested` (auto-amending the contract) and deliberately leaves
    `approved` events unconsumed rather than silently picking an answer here.
    Whichever way this is eventually decided, an auto-recorded call MUST
    pass ``actor="client via portal"`` (or similarly explicit) — never leave
    ``actor`` to default such that a client's decision reads as if some
    coord process made it.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown Gate A verdict {verdict!r}")
    return GateAApproval(
        repo_name=repo_name,
        milestone_number=int(milestone_number),
        verdict=verdict,
        contract_sha=contract_sha,
        tracking_issue=tracking_issue,
        note=note,
        actor=actor,
        recorded_at=time.time() if now is None else now,
    )


# ── the park marker ─────────────────────────────────────────────────────────
#
# The refusal prose is the ONLY channel that survives the process boundary
# between the guard (`coord assign`, exit 5) and the drive-queue tick, which
# reads it back out of the `drive_exited` audit row. `coord.merge_queue`'s
# `CI_PENDING_PREFIX`/`is_ci_pending_reason` pair set the precedent for
# classifying a refusal from its prose; this goes one step further and
# embeds the (repo, milestone) the tick needs to re-check the approval
# cheaply — a local board read, no `gh` call per parked entry per tick.

_PARK_MARKER_RE = re.compile(
    r"\[gate-a-approval repo=([^\s\]]+) ms-(\d+) v=([0-9a-f]+|none)\]"
)

#: The fingerprint stamped into the marker when no verdict exists at all.
NO_VERDICT = "none"


def approval_fingerprint(approval: "GateAApproval | dict | None") -> str:
    """A short, stable fingerprint of the *stored verdict* (not the contract).

    This is what makes the drive-queue un-park predicate exact. "Resume when
    a verdict exists" would loop forever on a ``--changes`` verdict: the
    guard refuses on it too, so the entry would resume, relaunch, refuse and
    re-park every tick. Resuming only when the stored verdict has *changed
    since the park* bounds it at exactly one relaunch per operator action —
    which is the correct amount, because an operator action is precisely
    what might have cleared it.
    """
    record = (
        GateAApproval.from_dict(approval) if isinstance(approval, dict) else approval
    )
    if record is None:
        return NO_VERDICT
    raw = f"{record.verdict}|{record.contract_sha}|{record.recorded_at!r}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def park_marker(
    repo_name: str, milestone_number: int, fingerprint: str = NO_VERDICT
) -> str:
    """The machine-readable tag embedded in a Gate-A refusal reason."""
    return (
        f"[gate-a-approval repo={repo_name} ms-{int(milestone_number)} "
        f"v={fingerprint or NO_VERDICT}]"
    )


def parse_park_marker(text: str | None) -> tuple[str, int, str] | None:
    """``(repo_name, milestone_number, fingerprint)`` from a marked reason.

    ``None`` when *text* is not a Gate-A refusal — which is what makes this
    safe to call over every parked queue entry's ``last_reason``.
    """
    if not text:
        return None
    m = _PARK_MARKER_RE.search(text)
    if m is None:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def is_gate_a_refusal_reason(text: str | None) -> bool:
    """Whether *text* is a Gate-A "not approved" refusal.

    ``coord.drive_queue._reconcile_running`` consults this **before** the
    ``exit_refused`` → ``blocked`` branch: unlike the refusals that branch
    was written for (#1844), this one has a one-command operator remedy and
    self-clears, so it must park (#1891 semantics) rather than land in
    terminal ``blocked``, which nothing re-evaluates (#2040).
    """
    return parse_park_marker(text) is not None


def evaluate(
    *,
    repo_name: str,
    milestone_number: int,
    contract_text: str | None,
    approval: GateAApproval | dict | None,
    exempt: bool = False,
    exempt_reason: str = "",
) -> GateADecision:
    """Pure Gate-A sign-off decision — no I/O, no config, no GitHub.

    *contract_text* is the contract as it exists on the repo's default
    branch (``None`` when it could not be read — treated as "cannot verify",
    which refuses). *approval* is the stored record, if any.

    Returns ``ok=True`` only for :data:`STATE_APPROVED` and
    :data:`STATE_EXEMPT`.
    """
    if exempt:
        return GateADecision(
            state=STATE_EXEMPT,
            ok=True,
            contract_sha=contract_digest(contract_text) if contract_text else "",
            approval=None,
            exempt_reason=exempt_reason,
        )

    record = (
        GateAApproval.from_dict(approval) if isinstance(approval, dict) else approval
    )
    marker = park_marker(
        repo_name, milestone_number, approval_fingerprint(record)
    )
    ms = f"ms-{milestone_number}"
    contract_path = f"tests/acceptance/{ms}/contract.md"

    if contract_text is None:
        return GateADecision(
            state=STATE_MISSING,
            ok=False,
            reason=(
                f"Gate A sign-off cannot be verified for {ms}: {contract_path!r} "
                f"could not be read from {repo_name}'s default branch, so there "
                "is nothing to key an approval to. Re-run `coord acceptance mock "
                f"{repo_name} <tracking_issue>` (docs/ORACLE_LOOP.md). {marker}"
            ),
        )

    sha = contract_digest(contract_text)

    remedy = (
        f"Read the rendered mock(s) + {contract_path} on the merged Gate-A PR, "
        f"then record the verdict: `coord gate-a --approved {repo_name} "
        "<tracking_issue>` (or `--changes` with a `--note`). Genuinely needs no "
        f"human eye? Declare it: `gate_a: {{exempt: true, reason: ...}}` in "
        f"tests/acceptance/{ms}/manifest.yml."
    )

    if record is None:
        return GateADecision(
            state=STATE_MISSING,
            ok=False,
            contract_sha=sha,
            reason=(
                f"Gate A has no recorded human sign-off for {ms} "
                f"(contract {short_digest(sha)}). Merging the Gate-A PR is not "
                "sign-off — nothing downstream may be built against a surface "
                f"nobody approved (#2063). {remedy} {marker}"
            ),
        )

    if record.verdict == VERDICT_CHANGES:
        note = f" Note: {record.note}" if record.note else ""
        if record.contract_sha and record.contract_sha != sha:
            # Changes were requested against an OLDER contract and the
            # contract has since moved — the amend plausibly addressed them,
            # but "plausibly" is exactly what this gate refuses to accept.
            return GateADecision(
                state=STATE_STALE,
                ok=False,
                contract_sha=sha,
                approval=record,
                reason=(
                    f"Gate A changes were requested for {ms} against contract "
                    f"{short_digest(record.contract_sha)}, and the contract has "
                    f"since changed to {short_digest(sha)} — the amend still "
                    f"needs a fresh look before anything is built on it.{note} "
                    f"{remedy} {marker}"
                ),
            )
        return GateADecision(
            state=STATE_CHANGES,
            ok=False,
            contract_sha=sha,
            approval=record,
            reason=(
                f"Gate A was reviewed for {ms} and a human asked for changes to "
                f"contract {short_digest(sha)}.{note} Amend it with `coord "
                f"acceptance mock {repo_name} <tracking_issue> --amend "
                '"<what to change>"`, then re-record the verdict with `coord '
                f"gate-a --approved {repo_name} <tracking_issue>`. {marker}"
            ),
        )

    if record.contract_sha != sha:
        return GateADecision(
            state=STATE_STALE,
            ok=False,
            contract_sha=sha,
            approval=record,
            reason=(
                f"Gate A approval for {ms} is stale: a human approved contract "
                f"{short_digest(record.contract_sha)}, but the contract on the "
                f"default branch is now {short_digest(sha)} (an `--amend` landed "
                "since). Approving v1 does not approve v2 — that is the same "
                f"failure this gate exists to prevent (#2063). {remedy} {marker}"
            ),
        )

    return GateADecision(
        state=STATE_APPROVED, ok=True, contract_sha=sha, approval=record
    )


def summarise(decision: GateADecision) -> str:
    """One-line human summary of *decision*, for ``coord gate-a`` output."""
    if decision.state == STATE_EXEMPT:
        if decision.exempt_reason:
            return (
                "exempt — this milestone declared it needs no human "
                f"sign-off ({decision.exempt_reason})"
            )
        return "exempt — this milestone declared it needs no human sign-off"
    if decision.state == STATE_APPROVED:
        who = decision.approval.actor if decision.approval else ""
        by = f" by {who}" if who else ""
        return f"approved{by} (contract {short_digest(decision.contract_sha)})"
    if decision.state == STATE_STALE:
        return f"stale — contract is now {short_digest(decision.contract_sha)}"
    if decision.state == STATE_CHANGES:
        return "changes requested"
    return "not approved — nobody has recorded a verdict"


def decisions_by_milestone(
    records: Iterable[Any],
) -> dict[tuple[str, int], GateAApproval]:
    """Index raw stored dicts by ``(repo_name, milestone_number)``."""
    out: dict[tuple[str, int], GateAApproval] = {}
    for raw in records:
        rec = GateAApproval.from_dict(raw)
        if rec is not None:
            out[(rec.repo_name, rec.milestone_number)] = rec
    return out


# ── #3065: unmerged Gate-A branches — the blind spot before the merge ──────
#
# `evaluate()` above is, and must stay, correct for what it was built for
# (#2063): it compares the recorded approval against the contract on the
# repo's DEFAULT BRANCH, so an `--amend` that has already merged correctly
# downgrades a stale approval to STATE_STALE. What it structurally cannot
# see is the window *before* that merge — a `type="mock-author"` branch
# that is dispatched, reviewed (maybe even approved), sitting in the merge
# queue, but not yet on the default branch. `coord gate-a`'s read path
# printed a clean "approved" for ten hours while exactly that sat waiting,
# because nothing it read ever looked past the default branch.
#
# This is deliberately a separate, read-only, best-effort layer bolted onto
# the read path in `coord/commands/gate_a.py` — NOT a new `GateADecision`
# state and NOT a change to `evaluate()`'s verdict semantics. The approval
# still means exactly what it always meant ("a human signed off on the
# contract that is/was on the default branch"); this only adds "...and by
# the way, here is a branch that approval does NOT cover."


@dataclass(frozen=True)
class PendingAmend:
    """One unmerged ``type="mock-author"`` branch found on the board for a
    milestone's tracking issue, paired with its review verdict (if any).

    ``review_verdict`` is ``None`` when no review row exists yet for this
    branch, or one exists but has not (yet) produced a parseable verdict —
    both read as "pending" to an operator; this module makes no attempt to
    tell them apart (that distinction already lives in ``coord gates``,
    #1956).
    """

    branch: str
    assignment_id: str | None
    review_verdict: str | None  # None | "approve" | "request-changes"


def find_pending_amends(
    *,
    repo_name: str,
    tracking_issue: int,
    all_assignments: Iterable[Any],
    is_merged: Callable[[str], bool],
) -> list[PendingAmend]:
    """Unmerged Gate-A (``type="mock-author"``) branches for this milestone's
    tracking issue, oldest first.

    *all_assignments* should be every board row worth scanning — active AND
    completed (a mock-author worker that finished its session moves to
    ``board.completed`` long before its branch merges, so completed rows
    are exactly the ones this exists to catch). Rows are duck-typed
    (``type``, ``repo_name``, ``issue_number``, ``branch``,
    ``assignment_id``, ``review_of_assignment_id``, ``review_verdict``,
    ``dispatched_at``) so real ``coord.models.Assignment`` rows and bare
    test stand-ins both work.

    *is_merged* is injected — normally
    ``lambda b: coord.github_ops.pr_is_merged(repo_cfg.github, b)`` — so
    this function itself makes no GitHub/network call and stays trivially
    testable. Any branch *is_merged* reports ``True`` for is dropped: a
    merged amend is exactly the case ``evaluate()`` already handles
    (:data:`STATE_STALE`), not this blind spot.

    Multiple unmerged branches for the same tracking issue (several amend
    rounds in flight, or history the board never cleaned up) are all
    returned — this is a "here is what's happening" read, not a
    single-answer decision, so it should not silently pick one and hide
    the rest.
    """
    rows = list(all_assignments)
    mock_rows = [
        a
        for a in rows
        if getattr(a, "type", None) == "mock-author"
        and getattr(a, "repo_name", None) == repo_name
        and getattr(a, "issue_number", None) == tracking_issue
        and getattr(a, "branch", None)
    ]

    # #3065 review: a fix-round worker (`auto_loop.py`'s `_dispatch_fix`,
    # ~line 1210) gets a brand-new `assignment_id` while reusing the SAME
    # `branch` (`branch=work.branch`), linking back via
    # `review_of_assignment_id=work.assignment_id`. So after a normal
    # request-changes -> fix -> re-review -> approve cycle, two
    # `mock-author` rows share one branch: the stale original (tied to the
    # superseded request-changes review) and the fix round (tied to the
    # current review). Per branch, keep the NEWEST-dispatched row — the
    # same "sort `dispatched_at` descending, take index 0" convention
    # `coord/diagnose.py`'s `_latest()` uses to resolve a fix-round chain —
    # not the oldest, or the stale original wins and its long-superseded
    # review verdict gets reported as current.
    by_branch: dict[str, Any] = {}
    for a in sorted(mock_rows, key=lambda a: getattr(a, "dispatched_at", None) or 0.0, reverse=True):
        by_branch.setdefault(a.branch, a)

    out: list[PendingAmend] = []
    for a in sorted(by_branch.values(), key=lambda a: getattr(a, "dispatched_at", None) or 0.0):
        branch = a.branch
        if is_merged(branch):
            continue
        # Prefer the verdict already stamped directly onto this (latest)
        # row's own `review_verdict` field: `coord/state.py`'s
        # `record_work_review_verdict()` stamps the winning terminal
        # verdict onto the *current* work-like row the moment the pipeline
        # advances (`WORK_LIKE_TYPES` includes `mock-author`), so this is
        # the same value `coord gates` and the merge gate already trust.
        # Fall back to scanning standalone `review` rows keyed to this
        # row's own `assignment_id` only when that field isn't populated
        # (duck-typed test stand-ins, or a review still in flight).
        review_verdict: str | None = getattr(a, "review_verdict", None)
        if review_verdict is None:
            reviews = [
                r
                for r in rows
                if getattr(r, "type", None) == "review"
                and getattr(r, "review_of_assignment_id", None) == a.assignment_id
            ]
            if reviews:
                reviews.sort(key=lambda r: getattr(r, "dispatched_at", None) or 0.0)
                review_verdict = getattr(reviews[-1], "review_verdict", None)
        out.append(
            PendingAmend(
                branch=branch,
                assignment_id=getattr(a, "assignment_id", None),
                review_verdict=review_verdict,
            )
        )
    return out


def summarise_pending_amends(pending: Iterable[PendingAmend]) -> list[str]:
    """Human-readable lines for ``coord gate-a``'s read path, one pair of
    lines per :class:`PendingAmend` — empty list when there is nothing
    pending. Purely additive to whatever :func:`summarise` already printed;
    never changes exit code or verdict."""
    lines: list[str] = []
    for p in pending:
        # #253: `review_verdict` speaks the *reviewer's* vocabulary
        # ("approve"/"request-changes"), not Gate A's own verdict
        # vocabulary (`VERDICT_APPROVED` == "approved") — these are two
        # different gates recording two different things.
        if p.review_verdict == "approve":
            what = "an approved --amend is waiting to merge"
        elif p.review_verdict == "request-changes":
            what = "an --amend is waiting on changes before it can merge"
        else:
            what = "an --amend is in flight, review not yet complete"
        verdict_word = p.review_verdict or "pending"
        lines.append(f"  ! {what}: {p.branch} (review: {verdict_word})")
        lines.append(
            "    the approval above covers the contract on main, NOT that branch"
        )
    return lines
