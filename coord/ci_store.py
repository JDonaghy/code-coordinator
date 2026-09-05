"""CiStore abstraction over CI check status.

Phase 1 (#240) of the CiStore abstraction: a thin Protocol over ``gh pr checks``
so the merge gate can hard-block on failed checks and the TUI can surface what
broke. Rerun, polling, and non-GitHub backends are deferred to later phases.

Phase 2 (#1851) adds the rerun half: :meth:`CiStore.rerun_for_pr` and the
:func:`checks_are_stale` predicate. A **green** check can itself be stale —
GitHub re-runs ``pull_request`` workflows on head ``synchronize``, never on
base movement, so a passing check proves the composite passed against the
base *as of the last head push*, not as of now. Polling and non-GitHub
backends remain deferred.

Phase 3 (#1892) adds a **RED** check's own analogue of the same question:
did this failure say anything about the code at all? :meth:`CiStore.
list_jobs_for_run` and :func:`is_verdictless_job` distinguish "never assigned
a runner" / "died before checkout" (a statement about the CI *platform*) from
a genuine test failure (a statement about the *code*) — used exclusively by
:mod:`coord.merge_queue`'s drive-retry accounting, never by the merge gate
itself (:func:`failed_checks` and ``_PASSING_CONCLUSIONS`` are unchanged: a
verdictless check still blocks the merge, it just doesn't cost a retry).

The split between :class:`CiStore` (Protocol) and the concrete backends
(:class:`coord.ci_github.GitHubCi`, :class:`NoOpCi`) means tests can pass a
stub through ``ci_store=`` without touching subprocess at all.
"""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class CiCheckSummary:
    """Structured rollup of a PR's CI checks — the board-wire analogue of the
    TUI's Rust ``CiCheckSummary`` (``tui/src/app/types.rs``).

    Populated server-side by :func:`summarize_counts` and attached to
    :class:`coord.merge_queue.PlannedMerge` so the TUI can render its "2✓ 1✗"
    badges straight from the ``/board`` payload instead of shelling out to
    ``gh pr checks`` itself (#1344).
    """

    passed: int
    failed: int
    running: int
    failed_names: list[str]
    first_failed_url: str | None


@dataclass
class JobStep:
    """One step of a GitHub Actions job (#1892).

    ``conclusion`` mirrors :class:`CheckRun`'s field: ``None`` while the step
    hasn't finished, otherwise success/failure/cancelled/skipped/... — the
    same vocabulary GitHub uses for check-run conclusions, one level down.
    """

    name: str
    conclusion: str | None


@dataclass
class JobRun:
    """One job of a GitHub Actions run — the step-level detail a
    :class:`CheckRun` doesn't carry (#1892).

    Populated only on the CI-failure classification path (see
    :func:`is_verdictless_job`): a :class:`CheckRun` names a *check*
    (workflow name, e.g. ``test (3.12)``), and this is the matching *job*
    (same name, fetched via ``gh api repos/{repo}/actions/runs/{id}/jobs``)
    with its steps. ``runner_name`` is empty when GitHub never assigned this
    job a runner at all — the "cancelled at the queue timeout" signature.

    ``job_id`` (#3114) is the numeric Actions job id — distinct from both
    the check's own id and the run id it belongs to — needed to fetch this
    job's own log text (``gh api repos/{repo}/actions/jobs/{id}/logs``, see
    :func:`coord.github_ops.get_job_log`). Defaults to ``""`` so every
    pre-#3114 ``JobRun(...)`` construction (all keyword-based; see
    :meth:`coord.ci_github.GitHubCi._fetch_jobs`) keeps working unchanged.
    """

    name: str
    conclusion: str | None
    runner_name: str
    steps: list[JobStep] = field(default_factory=list)
    job_id: str = ""


@dataclass
class CIFailureDetail:
    """Structured detail behind a CONFIRMED CI failure (#3114) — the failing
    job/step/log-excerpt a ci-fix briefing can use in place of a bare
    ``checks_summary`` one-liner.

    Built by :func:`coord.ci_github.build_ci_failure_detail`, which is
    best-effort throughout: any field below can come back empty when the
    underlying data wasn't available (no job matched the check, the log
    fetch failed/was throttled, ...) — a caller must treat an all-empty
    instance the same as "no detail", not as evidence of anything.
    """

    check_name: str
    job_name: str = ""
    step_name: str = ""
    log_excerpt: str = ""
    run_url: str = ""
    truncated: bool = False


def ci_failure_detail_to_json(detail: "CIFailureDetail | None") -> str:
    """Serialize *detail* for :class:`coord.merge_queue.QueuedMerge`'s
    ``ci_fix_detail_json`` cache column (#3114 review fix).

    ``None`` (a fetch was attempted and genuinely found nothing — not to be
    confused with "never attempted", which the column represents as SQL
    NULL / Python ``None`` at the ``QueuedMerge`` level) encodes to the JSON
    literal ``"null"`` so :func:`ci_failure_detail_from_json` can tell the
    two apart: a stored ``"null"`` string means "fetched, no detail",
    whereas the column itself being unset means "never fetched for this
    SHA".
    """
    if detail is None:
        return "null"
    return json.dumps(dataclasses.asdict(detail))


def ci_failure_detail_from_json(raw: str | None) -> "CIFailureDetail | None":
    """Inverse of :func:`ci_failure_detail_to_json`. Fails soft: malformed or
    unexpected JSON (a hand-edited DB row, a future/older schema) decodes to
    ``None`` — "no cached detail" — rather than raising, matching
    :func:`coord.ci_github.build_ci_failure_detail`'s own best-effort
    posture.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if data is None:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return CIFailureDetail(**data)
    except TypeError:
        return None


@dataclass
class CheckRun:
    """A single CI check run on a PR.

    ``status`` is the lifecycle phase: queued / in_progress / completed.
    ``conclusion`` is only meaningful when ``status == "completed"`` and is
    normally one of success / failure / cancelled / skipped / neutral /
    timed_out / action_required / stale — but GitHub can and does add new
    conclusions over time, and :class:`coord.ci_github.GitHubCi` synthesizes
    the conclusion ``"unknown"`` when it couldn't read a PR's checks at all
    (#1525). ``failed_checks`` below is an **allow-list**: a completed check
    passes only when its conclusion is affirmatively known-benign
    (``success`` / ``skipped`` / ``neutral``); anything else — including a
    conclusion this module has never seen — blocks the merge gate. ``status
    != "completed"`` is in-flight, handled separately by
    :func:`in_flight_checks`.
    """

    name: str
    status: str
    conclusion: str | None
    url: str
    run_id: str
    started_at: float | None
    completed_at: float | None


@runtime_checkable
class CiStore(Protocol):
    """View of CI checks for a PR, plus (#1851) the one write operation this
    abstraction supports: re-running them.

    ``rerun_for_pr`` is deliberately the *only* mutating method — everything
    else stays read-only exactly as Phase 1 (#240) left it, so every existing
    stub-based test (a plain object/dataclass implementing
    ``list_checks_for_pr``/``is_available`` with no ``rerun_for_pr`` at all)
    keeps passing unmodified: nothing here reads ``rerun_for_pr`` off a
    ``CiStore`` except the #1851 revalidate path, which only ever runs behind
    the ``coord merge --revalidate`` flag.
    """

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]: ...

    def list_all_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        """Every check reported for *repo*/*number*, required or advisory
        (#2446) — the unfiltered counterpart to :meth:`list_checks_for_pr`,
        which narrows to what branch protection actually requires so the
        merge gate never blocks on a merely-advisory check (see that
        method's docstring on :class:`coord.ci_github.GitHubCi`).

        Purely a visibility view: `coord merge --plan`'s CI summary reads
        this so a regressed advisory check is still visible to an operator
        even though it can no longer gate a merge attempt — matching what
        GitHub's own merge button already allows.

        Optional/duck-typed like :meth:`rerun_failed_for_pr` — call sites
        use ``getattr(ci_store, "list_all_checks_for_pr", None)`` and fall
        back to :meth:`list_checks_for_pr` for a stand-in (a test stub, or
        :class:`coord.gate_snapshot.GateSnapshot` when its own snapshot
        wasn't refreshed with the unfiltered view) that predates this
        capability — read as "no separate advisory view available", not an
        error.
        """
        ...

    @property
    def is_available(self) -> bool: ...

    def expects_checks(self, repo: str, number: int) -> bool:
        """True when *repo*/*number* should have reported at least one check.

        #1904: ``checks == []`` is genuinely ambiguous — "no CI is
        configured for this repo" (merging is correct) and "CI exists but
        was never triggered" (a throttled webhook, a wedged run, a
        path-filtered-out workflow — merging is wrong) both produce it, and
        every gate predicate (:func:`failed_checks`, :func:`in_flight_checks`,
        :func:`checks_are_stale`) is a filter that reads an empty list as
        "nothing wrong". This is the one method that answers "which of the
        two is this" *without* looking at any particular PR's checks — it
        asks whether the backend believes CI exists for this repo at all.
        Callers (``coord.merge_queue``'s ``checks_absent`` gate) only
        consult this when ``list_checks_for_pr`` has already come back
        empty; a non-empty check list settles the question on its own.

        :class:`NoOpCi` answers ``False`` unconditionally — it is the
        supported "this repo has no CI" opt-out (``ci_store: {type:
        none}``), so nothing it reports should ever read as "checks
        absent". A backend that can't determine this at all should default
        to ``True`` (fail closed, mirroring #1525's "unknown reads as
        blocking" posture) rather than silently reopening the hole this
        method exists to close.
        """
        ...

    def rerun_for_pr(self, repo: str, number: int) -> bool:
        """Re-run *repo*#*number*'s CI workflows. Returns whether it worked.

        Cheap remedy for a CI result staled by base movement (#1851): a CI
        re-run on GitHub-hosted runners costs minutes, not a routed Test-stage
        agent dispatch. Never called unattended — see
        :mod:`coord.revalidate`'s module docstring and
        ``docs/DRIVE_QUEUE.md`` for why ``--revalidate`` is opt-in and
        auto-drain must never trigger work on its own schedule.

        #1892: this same method is ALSO the auto-rerun remedy for a
        verdictless CI failure — see :mod:`coord.merge_queue`'s
        ``_ci_infra_reason``/``MAX_CI_INFRA_RERUNS``. That call site runs
        unattended (unlike ``--revalidate``), which is safe specifically
        *because* the trigger is narrow (every failing check carries no
        verdict about the code) and bounded (capped, then parked for a
        human) — it is not a general license for auto-drain to rerun CI.
        """
        ...

    def list_jobs_for_run(self, repo: str, run_id: str) -> list[JobRun]:
        """Job/step detail for Actions run *run_id* on *repo* (#1892).

        The one piece of data :class:`CheckRun` doesn't carry and the CI
        gate (``failed_checks``/``_PASSING_CONCLUSIONS``) never needed: which
        step (if any) actually ran before a check failed. Exists solely to
        back :func:`is_verdictless_job` — the drive's retry-accounting
        question "did this failure say anything about the code?", never the
        merge gate itself.

        Callers MUST only invoke this after a check has already been found
        failing (:func:`failed_checks` non-empty) — never on the passing or
        pending path, and never from a request-time board read (see
        ``coord.gate_snapshot``'s Invariant 1: the read path performs no
        third-party I/O). Best-effort: a backend that can't answer this
        returns ``[]``, which :func:`is_verdictless_job` always reads as "no
        job data, therefore not verdictless" — the same false-negative bias
        as an unmatched job (see that function's docstring).
        """
        ...

    def rerun_failed_for_pr(self, repo: str, number: int) -> bool:
        """Re-run ONLY the currently-failing job(s) behind *repo*#*number*'s
        checks (#2252). Returns whether it worked.

        Narrower than :meth:`rerun_for_pr`, which reruns every distinct
        Actions run id behind ALL of a PR's checks — passing ones included.
        Re-running the whole run would also re-trigger jobs that already
        reported green, discarding that first-pass evidence for nothing:
        #2252 asks "is the RED one flaky?", not "run the suite again", and
        wants the answer without paying for (or risking a second read of)
        checks that already settled.

        This is the mechanism behind :mod:`coord.merge_queue`'s one-shot
        flake re-check (``MAX_CI_FLAKY_RERUNS`` — deliberately ONE re-run,
        not a retry loop: two independent observations is the whole ask).
        Call sites duck-type via ``getattr(ci, "rerun_failed_for_pr",
        None)`` so a `CiStore` stand-in that predates this capability (most
        duck-typed test stubs, :class:`coord.gate_snapshot.GateSnapshot`)
        simply doesn't offer it — read as "could not trigger a re-run",
        the same fail-safe fallback an actual `gh` failure produces (#2252:
        "if the re-run cannot be triggered, fall back to today's
        behaviour").
        """
        ...


class NoOpCi:
    """Always-available fallback that returns no checks and reruns nothing.

    Used when the user opts out of CI gating with ``ci_store: { type: none }``
    or when no backend is configured.  ``is_available`` is ``False`` so callers
    can distinguish "no CI configured" from "CI says all clear".
    """

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return []

    def list_all_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        """No-op: CI gating is disabled entirely, so there is nothing to
        show either the gate or an operator (#2446)."""
        return []

    @property
    def is_available(self) -> bool:
        return False

    def expects_checks(self, repo: str, number: int) -> bool:
        """Always ``False`` — CI gating is opted out entirely (#1904), so an
        empty check list is never "checks absent", it's "no CI here"."""
        return False

    def rerun_for_pr(self, repo: str, number: int) -> bool:
        """No-op: CI gating is disabled entirely, so there is nothing to
        re-run and nothing to report as stale (#1851)."""
        return False

    def list_jobs_for_run(self, repo: str, run_id: str) -> list[JobRun]:
        """No-op: CI gating is disabled entirely, so there is no job/step
        detail to fetch (#1892)."""
        return []

    def rerun_failed_for_pr(self, repo: str, number: int) -> bool:
        """No-op: CI gating is disabled entirely, so there is nothing to
        re-run (#2252)."""
        return False


# ── Classification helpers ──────────────────────────────────────────────────

# #1525: allow-list of conclusions known to be benign, not a deny-list of
# conclusions known to be bad. Before this, `_FAILED_CONCLUSIONS` enumerated
# {"failure", "cancelled", "timed_out", "action_required"} and anything NOT
# in that set — a `"stale"` conclusion, a future GitHub conclusion this code
# had never seen, or the synthetic `"unknown"` conclusion GitHubCi emits when
# a `gh pr checks` read fails — silently read as "not failing", i.e. passing.
# That is exactly the fail-open shape that let PR #1521 merge over a red
# `test (3.12)` run: an unrecognised or unreadable conclusion must default to
# BLOCKING, never to passing.
_PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})


def _is_failing_conclusion(conclusion: str | None) -> bool:
    return conclusion not in _PASSING_CONCLUSIONS


def failed_checks(checks: list[CheckRun]) -> list[CheckRun]:
    """Return completed checks whose conclusion is not affirmatively passing.

    Only evaluates ``status == "completed"`` checks — an in-flight check has
    ``conclusion is None`` and is handled by :func:`in_flight_checks`
    instead, not counted as failed here.
    """
    return [
        c for c in checks
        if c.status == "completed" and _is_failing_conclusion(c.conclusion)
    ]


def in_flight_checks(checks: list[CheckRun]) -> list[CheckRun]:
    """Return checks that are queued or running (not yet completed)."""
    return [c for c in checks if c.status != "completed"]


# ── Post-rerun settle wait (#1925) ──────────────────────────────────────────
#
# `coord merge --revalidate`'s CI arm (`_apply_ci_revalidation` in
# `coord/commands/merge.py`) triggers `rerun_for_pr` for an entry whose CI
# checks are green but stale (predate the current base), then used to hand
# the entry straight to `merge_queue.process()`'s gate a moment later.
# `rerun_for_pr` invalidates the check cache, so that very next
# `list_checks_for_pr` read can land in the few-second window before GitHub
# has created ANY check-run record for the new workflow run: `gh pr checks`
# itself errors ("no checks reported on the ... branch"), which
# `coord.ci_github.GitHubCi._fetch` (correctly, per #1525) turns into a
# synthetic `conclusion="unknown"` check — and the fail-closed allow-list
# blocks on that exactly as it should for a GENUINE unreadable status. The
# bug was evaluating the gate a heartbeat after triggering the very re-run
# that caused this transient, self-inflicted reading — reliably, every time,
# since a freshly-triggered run cannot possibly have registered yet.
#
# `wait_for_ci_settle` closes that window: after triggering, poll (bounded)
# until the new run has both registered on GitHub AND finished, so the gate
# evaluates a real result — a genuine pass or a genuine failure — instead of
# the registration gap. This is what lets a healthy branch merge in the same
# `--revalidate` invocation instead of needing a human to come back once CI
# settles.
CI_RERUN_POLL_INTERVAL_SECONDS = 10.0

# ~3 minutes was the observed real-world settle time for the two 2026-08-07
# reproductions (#1925). This gives roughly a 2x margin without making a
# healthy `--revalidate` run hang needlessly long once CI is genuinely done.
CI_RERUN_MAX_WAIT_SECONDS = 360.0


def is_unreadable_check(check) -> bool:
    """True for the #1525 synthetic "could not read CI status" / "gh too
    old" stand-ins (``coord.ci_github._unreadable_check`` /
    ``_gh_too_old_check``) — the read itself failed, so this says nothing
    about the code yet. Duck-typed on ``.conclusion``/``.name`` rather than
    importing ``coord.ci_github`` — this module must stay backend-agnostic,
    and every stub/fake ``CheckRun``-alike in the test suite already has
    both attributes.

    Public (#2347) — :mod:`coord.merge_queue` reuses this directly to
    classify a ``checks_failed`` block whose failures are ALL this stand-in
    as a bare check-list *fetch* failure (``CI_UNREADABLE_PREFIX``), distinct
    from both a real "still running" and a real "ran and failed" verdict; see
    that module's docstring for the classification this feeds.

    Deliberately narrower than "any ``coord: ``-named synthetic check with
    ``conclusion == "unknown"``": :class:`coord.gate_snapshot.GateSnapshot`
    (the board *read* path's ``CiStore`` — :mod:`coord.merge_queue`'s
    ``_entry_gate_status`` can be, and in production is, called with one —
    see ``coord.serve_app``'s ``/board`` handler) emits its OWN such
    stand-in, ``_stale_check`` (``"coord: gate snapshot stale (...)"``), for
    a completely different local condition — the daemon's own refresh loop
    fell behind, not a GitHub read failure — and #2347's classification must
    not relabel that as "GitHub unreachable". Both `coord.ci_github` stand-ins
    share the phrase "read CI status" in their name; `_stale_check` does not,
    so requiring it is what keeps the two apart without this module having
    to import `coord.gate_snapshot` (which itself imports `coord.ci_store` —
    the reverse import would cycle) or duck-type on anything less stable
    than the prose these three call sites already commit to.
    """
    name = str(getattr(check, "name", ""))
    return (
        getattr(check, "conclusion", None) == "unknown"
        and name.startswith("coord: ")
        and "read CI status" in name
    )


@dataclass
class CiSettleResult:
    """Outcome of :func:`wait_for_ci_settle` (#1925).

    ``settled`` is the only thing callers need to branch on: ``True`` means
    *checks* is a genuine, resolved result (real pass or real fail) safe to
    hand to the merge gate. ``False`` means the wait budget ran out first —
    ``registering`` then distinguishes *why*: ``True`` is the self-inflicted
    "the re-run we just triggered still hasn't registered/resolved" case
    (never a real CI verdict), ``False`` means real checks are in flight and
    have simply not finished yet (an honest, already-correctly-classified
    ``checks_pending``).
    """

    settled: bool
    checks: list[CheckRun]
    waited_seconds: float
    registering: bool = False


def wait_for_ci_settle(
    ci_store: "CiStore",
    repo: str,
    number: int,
    *,
    timeout: float = CI_RERUN_MAX_WAIT_SECONDS,
    poll_interval: float = CI_RERUN_POLL_INTERVAL_SECONDS,
    echo=None,
    sleep=None,
    clock=None,
) -> CiSettleResult:
    """Bounded poll for a just-triggered CI re-run to register and finish.

    Only ever called right after a successful :meth:`CiStore.rerun_for_pr`
    (#1925) — see this section's header comment for the exact bug this
    closes. Every poll invalidates the backend's cache first (best-effort;
    only :class:`coord.ci_github.GitHubCi` implements ``invalidate``, so a
    stub without it is left alone) so each read is a fresh one, not a cached
    pre-rerun snapshot.

    A "registering" read — no checks at all yet, or every check present is
    one of the #1525 synthetic unreadable stand-ins (see
    :func:`is_unreadable_check`) — never counts as settled, however many
    times it's observed; only real, resolved checks do. Genuinely in-flight
    real checks (a run that registered and is now actually executing) also
    keep polling — that's the ordinary "wait for CI to finish" case this
    function exists to cover, not just the registration gap.

    Returns as soon as *checks* is non-empty, none of it is the synthetic
    unreadable stand-in, and none of it is still in-flight — i.e. a genuine
    resolved result, pass or fail alike; the caller (and ultimately
    ``merge_queue.process()``) decides what that result means. Gives up once
    *timeout* elapses, returning whatever was last observed with
    ``settled=False``.
    """
    echo = echo or (lambda msg: None)
    sleep = sleep if sleep is not None else time.sleep
    clock = clock if clock is not None else time.monotonic

    start = clock()
    checks: list[CheckRun] = []
    announced = False
    while True:
        invalidate = getattr(ci_store, "invalidate", None)
        if callable(invalidate):
            try:
                invalidate(repo, number)
            except Exception:  # noqa: BLE001 — best-effort cache-bust only
                pass
        checks = ci_store.list_checks_for_pr(repo, number)
        registering = not checks or all(is_unreadable_check(c) for c in checks)
        if not registering and not in_flight_checks(checks):
            return CiSettleResult(
                settled=True, checks=checks, waited_seconds=clock() - start,
            )
        elapsed = clock() - start
        if elapsed >= timeout:
            return CiSettleResult(
                settled=False, checks=checks, waited_seconds=elapsed,
                registering=registering,
            )
        if not announced:
            echo(
                f"  --revalidate: waiting for the CI re-run on {repo}#{number} "
                "to register and settle before evaluating the gate (#1925)..."
            )
            announced = True
        sleep(poll_interval)


# #1892: two real signatures — recorded from JDonaghy/claude-coordinator run
# 31117792472 and JDonaghy/vimcode run 31119463000, both 2026-08-06 — for a
# CI failure that says nothing about the code:
#
# 1. Never assigned a runner: cancelled at the queue timeout, `runner_name`
#    empty, `steps` empty. GitHub does create a job record for this (unlike
#    a run that never even reaches job-scheduling), but with zero steps.
# 2. Got a runner, died before checkout: exactly one step, named literally
#    "Set up job", with a non-passing conclusion — nothing past it ran, so
#    no repo code executed either.
#
# Deliberately narrow — see this module's `_PASSING_CONCLUSIONS` comment for
# the identical lesson learned the hard way (#1525) about allow-lists vs.
# catch-alls, and the issue's own hazard note: a classifier that is too eager
# becomes a way to launder real failures into "infrastructure". Prefer false
# negatives — a platform failure misread as real costs one manual unblock; a
# real failure misread as platform noise costs a bad merge. So both
# `check is None`/`job is None` (no job data — including a fetch failure;
# see `CiStore.list_jobs_for_run`'s docstring) and any shape that isn't
# EXACTLY one of the two above read as "carries a verdict", never as
# verdictless.
_SET_UP_JOB_STEP_NAME = "Set up job"


def is_verdictless_job(check: CheckRun, job: JobRun | None) -> bool:
    """True when *check* failed for a reason that says nothing about the
    code — see the two signatures documented above (#1892).

    Only meaningful for a check :func:`failed_checks` already selected
    (``status == "completed"`` and a non-passing conclusion); a check that
    is still in flight, or one this function is asked about with no
    matching *job* record, always reads ``False`` — "carries a verdict",
    the safe default per the false-negative bias above.

    This is a **narrower** question than the merge gate's own — it does not
    change whether the check counts as failed (:func:`failed_checks` is
    untouched), only whether the failure is evidence about the *code*. Used
    exclusively by the drive's retry accounting (:mod:`coord.merge_queue`'s
    ``_ci_infra_reason``), never by the gate itself.
    """
    if check.status != "completed" or job is None:
        return False
    if check.conclusion == "cancelled":
        return len(job.steps) == 0
    failed_steps = [
        s for s in job.steps if s.conclusion not in (None, "success", "skipped")
    ]
    return len(failed_steps) == 1 and failed_steps[0].name == _SET_UP_JOB_STEP_NAME


def checks_are_stale(checks: list[CheckRun], base_commit_time: float | None) -> bool:
    """True when a **green** *checks* result predates *base_commit_time* (#1851).

    GitHub attaches ``pull_request`` check runs to the PR's *head* SHA and
    re-runs them on head ``synchronize`` — never on base movement — so a
    check that started before the base's newest commit landed never saw that
    commit. ``started_at`` (not ``completed_at``) is the comparison point:
    what matters is what the base looked like when the run *began*, not when
    it finished.

    Callers should apply :func:`failed_checks`/:func:`in_flight_checks`
    first — this function assumes *checks* is the all-passing remainder and
    doesn't re-derive that itself, so it never contradicts "CI failed"/"CI
    running" with a third, competing reading of the same checks. An empty
    *checks* list (nothing to compare) reads as not-stale; the caller's own
    "no checks" handling covers that case — see :meth:`CiStore.expects_checks`
    and ``coord.merge_queue``'s ``checks_absent`` gate (#1904), which now
    implements exactly that handling at all three call sites.

    Fails closed toward **stale** — mirroring
    :func:`coord.merge_queue._base_move_is_inert`'s documented bias ("a false
    'fresh' merges untested code; a false 'stale' only costs a re-run") —
    whenever the comparison can't be made with confidence: *base_commit_time*
    unreadable/``None``, or any check missing ``started_at``. Clock and
    ordering skew between GitHub's check timestamps and its branch-commit
    timestamps are real; this predicate is not exact, and errs toward the
    cheap re-run rather than the silent stale-green pass.
    """
    if not checks:
        return False
    if base_commit_time is None:
        return True
    return any(c.started_at is None or c.started_at < base_commit_time for c in checks)


def build_ci_store(
    ci_store_type: str, *, host: str | None = None, token_env: str | None = None
) -> CiStore:
    """Construct the CiStore backend named by ``ci_store_type``.

    Centralised here so callers (merge gate, TUI fetcher, tests) don't need to
    branch on the config value themselves. Unknown types fall back to NoOpCi
    so a typo in coordinator.yml doesn't crash the merge command.

    ``host``/``token_env`` (#1897) are only consumed by the ``gitlab``
    backend — every other backend ignores them, so existing callers that
    only ever pass ``ci_store_type`` keep working unmodified. Falsy values
    (``None`` or ``""``) are treated as "use the backend's own default"
    rather than forwarded as an explicit override.
    """
    if ci_store_type == "github":
        from coord.ci_github import GitHubCi  # noqa: PLC0415
        return GitHubCi()
    if ci_store_type == "gitlab":
        from coord.ci_gitlab import GitLabCi  # noqa: PLC0415
        kwargs: dict[str, str] = {}
        if host:
            kwargs["host"] = host
        if token_env:
            kwargs["token_env"] = token_env
        return GitLabCi(**kwargs)
    return NoOpCi()


def summarize(checks: list[CheckRun]) -> str:
    """One-line summary: ``2✓ 1✗`` or ``no checks``.

    Used by the TUI under the Merge stage row and by the CLI when reporting
    why a merge was refused.
    """
    if not checks:
        return "no checks"
    passed = sum(1 for c in checks if c.conclusion == "success")
    failed = len(failed_checks(checks))
    running = len(in_flight_checks(checks))
    parts: list[str] = []
    if passed:
        parts.append(f"{passed}✓")
    if failed:
        parts.append(f"{failed}✗")
    if running:
        parts.append(f"{running}⋯")
    return " ".join(parts) if parts else "no checks"


def summarize_counts(checks: list[CheckRun]) -> CiCheckSummary:
    """Structured rollup of *checks*, mirroring the classification the TUI's
    (now-deleted) ``fetch_ci_check_summary`` used to compute client-side:

    - not yet ``completed`` → running
    - completed + conclusion NOT in ``_PASSING_CONCLUSIONS`` → failed (name +
      first URL captured); this is an allow-list (#1525), so an unrecognised
      or synthetic ``"unknown"`` conclusion counts as failed
    - completed + conclusion in ``_PASSING_CONCLUSIONS`` (success / skipped /
      neutral) → passed

    Used to populate :class:`coord.merge_queue.PlannedMerge.ci_summary` so the
    `/board` payload carries everything the TUI renders as CI badges (#1344).
    """
    # `checks` items are `CheckRun` in production but tests commonly pass
    # lighter duck-typed fakes (see `failed_checks`/`in_flight_checks` above,
    # which only ever touch `.status`/`.conclusion`) — `getattr` with a
    # default keeps this function tolerant of fakes that omit `.url`.
    passed = failed = running = 0
    failed_names: list[str] = []
    first_failed_url: str | None = None
    for c in checks:
        if c.status != "completed":
            running += 1
            continue
        if _is_failing_conclusion(c.conclusion):
            failed += 1
            failed_names.append(c.name)
            url = getattr(c, "url", "") or ""
            if first_failed_url is None and url:
                first_failed_url = url
        else:
            passed += 1
    return CiCheckSummary(
        passed=passed,
        failed=failed,
        running=running,
        failed_names=failed_names,
        first_failed_url=first_failed_url,
    )
