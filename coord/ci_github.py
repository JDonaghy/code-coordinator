"""GitHub Actions backend for :mod:`coord.ci_store`.

Fetches via :func:`coord.github_ops.get_pr_checks` (``gh pr checks <number>
--repo <slug> --json …`` — #1483: the single ``gh`` sink lives in
``github_ops``, not here) and maps the response to
:class:`coord.ci_store.CheckRun`.  Results are cached per-(repo, number) for
``cache_ttl`` seconds so the merge gate (which may iterate over many PRs)
doesn't hammer ``gh`` — the cost of a stale read in the gate path is at most
one wasted retry, and the user will re-run ``coord merge`` anyway.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from coord import github_ops
from coord.ci_store import CheckRun, CIFailureDetail, JobRun, JobStep, failed_checks
from coord.forge_availability import record_ci_check_fetch


def _parse_ts(raw: str | None) -> float | None:
    """Parse an ISO-8601 timestamp from gh (e.g. ``2026-05-24T12:34:56Z``).

    Returns ``None`` for empty / unparseable input — gh emits an empty string
    when the field is unknown rather than omitting the JSON key.
    """
    if not raw:
        return None
    try:
        # gh emits Zulu; datetime.fromisoformat accepts the +00:00 form.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# #1564: `gh pr checks --json` has no `conclusion` field, and its `state`
# field is a per-check *verdict* (SUCCESS/FAILURE/SKIPPED/...), not a
# lifecycle phase — feeding `state` through a QUEUED/IN_PROGRESS/COMPLETED
# normaliser made every check fall through to the "unknown → in_progress"
# branch forever, so `failed_checks()` (which only looks at `status ==
# "completed"` checks) never evaluated anything and the gate blocked every
# merge unconditionally. `bucket` is gh's own normalisation of `state` into
# pass / fail / pending / skipping / cancel and is exactly the lifecycle +
# verdict split CheckRun wants: `pending` is the only in-flight bucket,
# everything else is a completed verdict.
_BUCKET_CONCLUSIONS: dict[str, str] = {
    "pass": "success",
    "fail": "failure",
    "skipping": "skipped",
    "cancel": "cancelled",
}


# #1851: an Actions check `link` is either a *run* URL
# (".../actions/runs/{run_id}") or a *job* URL
# (".../actions/runs/{run_id}/job/{job_id}") depending on whether the check
# has a job breakdown yet. `gh run rerun` takes the *run* id specifically —
# taking the last path segment (the pre-#1851 behaviour of the `run_id` field
# below) silently grabs the job id off a job URL instead. Nothing before
# #1851 ever read `CheckRun.run_id` (see the Phase 1 header in
# `coord/ci_store.py`), so fixing the extraction here has no back-compat
# concern.
_RUN_ID_RE = re.compile(r"/runs/(\d+)")


# #3114: bound on the log excerpt threaded into a ci-fix briefing — "last
# ~200 lines or ~8KB, whichever is smaller" per the issue's fix shape. A
# fetched job log can run to many MB; a briefing is a chat message, not a
# log viewer, and a worker that needs more than this can still read the
# full log itself (see `build_ci_failure_detail`'s `run_url`).
CI_FIX_LOG_MAX_LINES = 200
CI_FIX_LOG_MAX_BYTES = 8192


def _bound_log_excerpt(text: str) -> tuple[str, bool]:
    """Truncate *text* to the last :data:`CI_FIX_LOG_MAX_LINES` lines AND
    :data:`CI_FIX_LOG_MAX_BYTES` bytes, whichever is smaller (#3114).

    Returns ``(excerpt, truncated)`` — *truncated* is ``True`` iff either
    bound actually cut something, so the caller can make the cut visible in
    the briefing text rather than silently handing over a partial log.
    """
    lines = text.splitlines()
    truncated = False
    if len(lines) > CI_FIX_LOG_MAX_LINES:
        lines = lines[-CI_FIX_LOG_MAX_LINES:]
        truncated = True
    excerpt = "\n".join(lines)
    encoded = excerpt.encode("utf-8", errors="replace")
    if len(encoded) > CI_FIX_LOG_MAX_BYTES:
        encoded = encoded[-CI_FIX_LOG_MAX_BYTES:]
        excerpt = encoded.decode("utf-8", errors="replace")
        truncated = True
    return excerpt, truncated


def _failing_step(job: JobRun) -> JobStep | None:
    """First step of *job* whose conclusion isn't affirmatively benign —
    mirrors :class:`coord.ci_store.CheckRun`'s own success/skipped/neutral
    allow-list (#1525's fail-closed posture), one level down.

    Deliberately includes ``"neutral"`` in the benign set, unlike
    :func:`coord.ci_store.is_verdictless_job`'s own allow-list
    (``None``/``"success"``/``"skipped"`` — no ``"neutral"``). The two ask
    different questions: ``is_verdictless_job`` decides whether a whole
    JOB'S failure counts as infra noise that shouldn't cost a retry-budget
    spend, whereas this decides which single STEP to point a human/worker
    at as THE failing one — and GitHub itself doesn't treat a "neutral"
    step conclusion as a failure, so it shouldn't be singled out as one
    here even on a job that ``is_verdictless_job`` would still count as
    verdicted. Kept separate on purpose; not a bug to reconcile.
    """
    return next(
        (s for s in job.steps if s.conclusion not in (None, "success", "skipped", "neutral")),
        None,
    )


def build_ci_failure_detail(
    ci_store: object, repo: str, pr_number: int,
) -> CIFailureDetail | None:
    """Best-effort structured detail behind a CONFIRMED CI failure (#3114):
    the failing job name, failing step name, a bounded log excerpt, and the
    run URL — the detail a ci-fix briefing used to lack entirely (see the
    issue's evidence: a worker had to spend 82 turns rediscovering a
    one-line fix the coordinator already had ``list_jobs_for_run`` data for).

    Only ever called at CI-fix dispatch time (``coord.commands.merge.
    _dispatch_ci_fixes``) for an entry ``coord.ci_fix.dispatch_precheck``
    has already confirmed is otherwise dispatch-eligible, and only once per
    distinct ``branch_head_sha`` — never on the polling path, the same
    scoping ``coord.merge_queue._ci_infra_reason`` already established for
    :meth:`~coord.ci_store.CiStore.list_jobs_for_run`. A repeat ``coord
    merge`` tick against the SAME still-failing SHA (dispatch declined for
    a reason unrelated to CI — see ``dispatch_precheck``'s docstring) reuses
    the cached result instead of calling this again — see
    ``QueuedMerge.ci_fix_detail_sha``/``ci_fix_detail_json``. *ci_store* is
    duck-typed (not annotated as :class:`coord.ci_store.CiStore` to avoid an
    import cycle) — anything exposing ``list_checks_for_pr``/
    ``list_jobs_for_run`` works, matching how ``_ci_infra_reason`` treats it.

    Fails soft throughout, same false-negative bias as
    :func:`coord.merge_queue._ci_infra_reason`: returns ``None`` when there
    is no completed failing check, no job matched the failing check, or ANY
    read along the way raises (a throttled/rate-limited ``gh``, a malformed
    job id, a missing method on a duck-typed stub, ...). The caller falls
    back to the plain ``checks_summary`` text exactly as if this function
    didn't exist — this is enrichment, never a dispatch precondition.

    Best-effort in a second sense too: when more than one check is
    simultaneously failing, this describes only ONE of them (``next((c for
    c in failed if c.run_id), failed[0])`` below — the first with a
    readable run id, or else the first failing check period). The plain
    ``checks_summary``/``ev.message`` one-liner the caller always falls
    back to (``coord.merge_queue.process``'s ``", ".join(...)``) can name
    several failing checks at once; this function's "## CI failure detail"
    section may therefore describe a different job/step than the ones that
    summary line enumerates. Not incorrect — just incomplete when the
    failure isn't isolated to a single check.
    """
    try:
        checks = ci_store.list_checks_for_pr(repo, pr_number)
        failed = failed_checks(checks)
        if not failed:
            return None
        check = next((c for c in failed if c.run_id), failed[0])
        job: JobRun | None = None
        if check.run_id:
            jobs = ci_store.list_jobs_for_run(repo, check.run_id) or []
            job = next((j for j in jobs if j.name == check.name), None)
            if job is None and len(jobs) == 1:
                job = jobs[0]
        step = _failing_step(job) if job is not None else None
        log_excerpt = ""
        truncated = False
        if job is not None and job.job_id:
            raw_log = github_ops.get_job_log(repo, job.job_id)
            log_excerpt, truncated = _bound_log_excerpt(raw_log)
        run_url = check.url or (
            f"https://github.com/{repo}/actions/runs/{check.run_id}"
            if check.run_id else ""
        )
        return CIFailureDetail(
            check_name=check.name,
            job_name=job.name if job is not None else "",
            step_name=step.name if step is not None else "",
            log_excerpt=log_excerpt,
            run_url=run_url,
            truncated=truncated,
        )
    except Exception:  # noqa: BLE001 — best-effort enrichment (#3114), same
        # posture as `_ci_infra_reason`'s classification-only catch: a
        # throttled/rate-limited/malformed read here must degrade to "no
        # detail available", never block or delay the actual dispatch.
        return None


def _run_id_from_link(link: str) -> str:
    """Extract the numeric Actions *run* id from a `gh pr checks` `link` URL.

    Returns ``""`` when *link* is empty or doesn't match the expected Actions
    URL shape (e.g. a third-party check with no GitHub Actions run behind
    it) — callers treat an empty run id as "not rerunnable".
    """
    match = _RUN_ID_RE.search(link or "")
    return match.group(1) if match else ""


def _normalize_bucket(bucket: str) -> str:
    """Lowercase/None-safe normalisation of gh's ``bucket`` field, shared by
    :func:`_status_from_bucket` and :func:`_conclusion_from_bucket` so the two
    don't each repeat the same ``(bucket or "").lower()`` guard."""
    return (bucket or "").lower()


def _status_from_bucket(bucket: str) -> str:
    """Map gh's ``bucket`` to the CheckRun lifecycle enum ("in_progress" or
    "completed" — gh's own `--json bucket` doc lists no other pending-like
    value, so anything other than "pending" is treated as decided)."""
    return "in_progress" if _normalize_bucket(bucket) == "pending" else "completed"


def _conclusion_from_bucket(bucket: str) -> str | None:
    """Map gh's ``bucket`` to a CheckRun conclusion.

    "pending" has no conclusion yet (status is in-flight, see
    :func:`_status_from_bucket`). Anything that isn't one of gh's
    documented buckets (pass/fail/pending/skipping/cancel — e.g. a future
    bucket value this code has never seen) maps to "unknown" rather than
    being silently treated as passing, mirroring #1525's fail-closed
    synthetic-unreadable-check conclusion.
    """
    b = _normalize_bucket(bucket)
    if b == "pending":
        return None
    return _BUCKET_CONCLUSIONS.get(b, "unknown")


@dataclass
class GitHubCi:
    """Shell out to ``gh pr checks`` and cache results briefly."""

    cache_ttl: float = 10.0
    _cache: dict[tuple[str, int], tuple[float, list[CheckRun]]] = field(default_factory=dict)
    # #1904: repo -> (fetched_at, expects_checks). Keyed by repo alone (not
    # (repo, number) like `_cache` above) — whether a repo declares Actions
    # workflows doesn't vary per PR, so every PR in the same repo shares one
    # cached answer instead of paying a `gh api .../actions/workflows` round
    # trip each.
    _workflow_cache: dict[str, tuple[float, bool]] = field(default_factory=dict)
    # #1892: (repo, run_id) -> (fetched_at, jobs). Only ever populated by
    # `list_jobs_for_run`, which callers invoke exclusively on the CI-failure
    # classification path (see that method's docstring) — this cache exists
    # so a board build that re-evaluates the SAME still-failing PR every tick
    # (`plan()` -> `_entry_gate_status`, or a `process()` retry loop) doesn't
    # re-issue the extra `gh api .../jobs` call every time; it shares
    # `cache_ttl` with `_cache` above rather than a second knob.
    _jobs_cache: dict[tuple[str, str], tuple[float, list[JobRun]]] = field(
        default_factory=dict
    )
    # #2388: repo -> (fetched_at, required context names or None). Keyed by
    # repo alone, same reasoning as `_workflow_cache` — required contexts are
    # a branch-protection property of the repo's default branch, not of any
    # particular PR.
    _required_contexts_cache: dict[str, tuple[float, frozenset[str] | None]] = field(
        default_factory=dict
    )

    @property
    def is_available(self) -> bool:
        # gh is a hard dependency of the project (see CLAUDE.md). The
        # subprocess check is cheap but unnecessary; assume True when this
        # backend is constructed and let the actual ``gh pr checks`` call
        # surface the failure if gh is missing.
        return True

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        """Checks the merge gate evaluates — narrowed to what GitHub's own
        branch protection actually REQUIRES, when that's determinable
        (#2388/#2446).

        A repo can report far more `gh pr checks` entries than GitHub's
        required-status-check list (this repo: 9 reported, 5 required), and
        every merge-gate predicate downstream of this method
        (`failed_checks`, `in_flight_checks`, `checks_are_stale`) otherwise
        waits on advisory jobs GitHub itself doesn't — a hung/flaky ADVISORY
        check (e.g. `Acceptance (web)`, or an unconditional Playwright/
        Chromium install with no timeout) then blocks `coord merge`
        indefinitely even though the PR is already `MERGEABLE`. Never
        filters (returns everything :meth:`list_all_checks_for_pr` does)
        when the required list can't be determined — #1525's bias: unknown
        reads as "wait on everything reported", not as a free pass to stop
        waiting on something that might matter.

        #2446: this is deliberately the ONLY narrowed view — see
        :meth:`list_all_checks_for_pr` for the full, unfiltered set that
        feeds `coord merge --plan`'s CI summary, so a regressed advisory
        check stays visible to an operator even though it can no longer gate
        a merge attempt.
        """
        checks = self._all_checks(repo, number)
        required = self._required_contexts(repo)
        if required:
            return [c for c in checks if c.name in required]
        return checks

    def list_all_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        """Every check `gh pr checks` reports, required or advisory (#2446).

        Purely a visibility view — `coord merge --plan`/the TUI's CI badges
        read this so a regressed ADVISORY check (one GitHub's branch
        protection doesn't require, and :meth:`list_checks_for_pr` therefore
        no longer waits on) is still something an operator can see, per
        #2446's suggested fix: "still visible ... but should never gate a
        merge attempt." Shares :meth:`list_checks_for_pr`'s cached fetch —
        one `gh pr checks` subprocess call backs both views, never two.
        """
        return self._all_checks(repo, number)

    def _all_checks(self, repo: str, number: int) -> list[CheckRun]:
        key = (repo, number)
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        checks = self._fetch(repo, number)
        self._cache[key] = (now, checks)
        return checks

    def expects_checks(self, repo: str, number: int) -> bool:
        """True when *repo* declares at least one GitHub Actions workflow (#1904).

        *number* is accepted only to satisfy :class:`coord.ci_store.CiStore`'s
        shape — workflow *definitions* are repo-wide, not per-PR, so the
        cache (see ``_workflow_cache`` above) is keyed on *repo* alone.

        Fails closed (returns ``True``) on any read failure — an
        unreadable/erroring ``gh api .../actions/workflows`` call means "we
        don't know if this repo has CI", and #1525's rule is that unknown
        must read as "checks were expected", not as the free pass that let
        an empty ``checks`` list merge untested code in the first place.
        """
        now = time.time()
        cached = self._workflow_cache.get(repo)
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        result = self._fetch_expects_checks(repo)
        self._workflow_cache[repo] = (now, result)
        return result

    def _fetch_expects_checks(self, repo: str) -> bool:
        try:
            count = github_ops.get_repo_workflow_count(repo)
        except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError, ValueError):
            return True  # fail closed — see expects_checks' docstring
        return count > 0

    def invalidate(self, repo: str | None = None, number: int | None = None) -> None:
        """Drop cached entries — pass nothing to clear everything."""
        if repo is None and number is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            if repo is not None and key[0] != repo:
                continue
            if number is not None and key[1] != number:
                continue
            del self._cache[key]

    def rerun_for_pr(self, repo: str, number: int) -> bool:
        """Re-run every distinct Actions run behind *repo*#*number*'s current
        checks via ``gh run rerun`` (#1851).

        A PR's checks can span more than one workflow run — `test.yml` and
        `cargo-test.yml` in this repo, for instance — so this reruns every
        distinct ``run_id`` among the PR's checks, not just the first. Best
        effort throughout: a check with no readable run id (an empty ``link``,
        or a third-party check with no Actions run behind it at all) is
        skipped rather than failing the whole call, and any individual
        ``gh run rerun`` failure is logged into the return value rather than
        raised — this is the remedy side of a fail-closed *reading*
        (:func:`coord.ci_store.checks_are_stale`), not itself required to be
        fail-closed: a rerun that only partially succeeds still helps, but
        the caller must not be told it fully worked.

        Returns ``True`` only when at least one run id was found *and* every
        rerun call it issued exited zero. Returns ``False`` when there was
        nothing rerunnable (no checks, or none with a readable run id) or any
        rerun call failed. Invalidates this PR's checks cache entry AND
        (#1892) any cached :meth:`list_jobs_for_run` entry for the run ids
        being rerun on any success, so neither the next
        :meth:`list_checks_for_pr` nor the next :meth:`list_jobs_for_run`
        can hand back a briefly-cached pre-rerun snapshot — `gh run rerun`
        reruns the SAME Actions run id, so a stale `_jobs_cache` hit within
        `cache_ttl` could otherwise launder a real failure into
        "infrastructure" by pairing a fresh check re-read with stale
        job/step detail from before the rerun.
        """
        checks = self.list_checks_for_pr(repo, number)
        run_ids = sorted({c.run_id for c in checks if c.run_id})
        if not run_ids:
            return False
        all_ok = True
        any_ok = False
        for run_id in run_ids:
            # #1483: route through the single `gh` sink instead of shelling
            # out here directly.
            if github_ops.rerun_workflow_run(repo, run_id):
                any_ok = True
            else:
                all_ok = False
        if any_ok:
            self.invalidate(repo, number)
            for run_id in run_ids:
                self._jobs_cache.pop((repo, str(run_id)), None)
        return all_ok and any_ok

    def rerun_failed_for_pr(self, repo: str, number: int) -> bool:
        """Re-run only the FAILING job(s) behind *repo*#*number*'s current
        checks via ``gh run rerun <id> --failed`` (#2252).

        Same shape as :meth:`rerun_for_pr` above, narrowed to the run ids
        behind :func:`coord.ci_store.failed_checks` rather than every run id
        among the PR's checks — a passing check's run is left untouched, so
        the caller's #2252 flake re-check can't accidentally spend CI
        minutes re-proving a check that already reported green, and the
        green evidence itself (timestamps, logs) survives intact for the
        board to keep showing.

        Same best-effort / return-value / cache-invalidation contract as
        `rerun_for_pr`: ``True`` only when at least one run id was found
        among the currently-failing checks *and* every ``gh run rerun
        --failed`` call issued exited zero.
        """
        checks = self.list_checks_for_pr(repo, number)
        run_ids = sorted({c.run_id for c in failed_checks(checks) if c.run_id})
        if not run_ids:
            return False
        all_ok = True
        any_ok = False
        for run_id in run_ids:
            if github_ops.rerun_workflow_run_failed(repo, run_id):
                any_ok = True
            else:
                all_ok = False
        if any_ok:
            self.invalidate(repo, number)
            for run_id in run_ids:
                self._jobs_cache.pop((repo, str(run_id)), None)
        return all_ok and any_ok

    def list_jobs_for_run(self, repo: str, run_id: str) -> list["JobRun"]:
        """Job/step detail for Actions run *run_id* on *repo* via ``gh api``
        (#1892) — backs :func:`coord.ci_store.is_verdictless_job`.

        Deliberately best-effort in a way :meth:`list_checks_for_pr` is NOT
        (#1525's fail-closed posture applies to the merge *gate*; this is a
        narrower, purely-advisory retry-accounting read — see
        :class:`coord.ci_store.CiStore`'s own docstring for the same
        distinction drawn for ``list_jobs_for_run``). Any read failure —
        missing ``gh``, timeout, malformed JSON, an expired/nonexistent run
        id — returns ``[]`` rather than raising or synthesizing a failing
        placeholder; the caller's classifier already treats "no job data" as
        "not verdictless" (the safe false-negative direction), so there is
        nothing this method needs to signal beyond an empty result.
        """
        key = (repo, str(run_id))
        now = time.time()
        cached = self._jobs_cache.get(key)
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        jobs = self._fetch_jobs(repo, run_id)
        self._jobs_cache[key] = (now, jobs)
        return jobs

    def _fetch_jobs(self, repo: str, run_id: str) -> list["JobRun"]:
        try:
            raw = github_ops.get_run_jobs(repo, run_id)
        except (RuntimeError, ValueError):
            # `github_ops.get_run_jobs` only ever raises `RuntimeError`
            # (`GhError`, itself a `RuntimeError` subclass, already wraps
            # gh-not-found/timeout/OSError inside `_gh`/`_gh_json`) or
            # `ValueError` — no need to duplicate the lower-level exception
            # types here.
            return []
        if not isinstance(raw, list):
            return []
        jobs: list[JobRun] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            steps = [
                JobStep(
                    name=str(step.get("name", "")),
                    conclusion=step.get("conclusion"),
                )
                for step in (entry.get("steps") or [])
                if isinstance(step, dict)
            ]
            jobs.append(
                JobRun(
                    name=str(entry.get("name", "")),
                    conclusion=entry.get("conclusion"),
                    runner_name=str(entry.get("runner_name") or ""),
                    steps=steps,
                    job_id=str(entry.get("id", "") or ""),
                )
            )
        return jobs

    # ── Internal ────────────────────────────────────────────────────────────

    def _fetch(self, repo: str, number: int) -> list[CheckRun]:
        # #1896 Phase 0: this is the "every CiStore.list_checks_for_pr
        # outcome" seam the forge-availability program asks for — reachable/
        # unreachable, plus the check-level conclusion distribution.
        # `github_ops.get_pr_checks` does NOT go through `_gh()` — it shells
        # out to `gh pr checks` directly (a non-zero exit there can still
        # carry usable JSON on stdout, which `_gh()`'s raise-on-nonzero
        # contract can't express, see `get_pr_checks`'s own docstring) — so
        # this call is the *only* forge-availability observation this read
        # produces. It stands alone, not "layered on top" of anything.
        # Only fires on a real cache miss — a cached `list_checks_for_pr`
        # hit never reaches here, so this costs nothing extra either.
        _t0 = time.monotonic()
        try:
            raw = github_ops.get_pr_checks(repo, number)
        except github_ops.GhTooOldForJsonChecks as e:
            # #1564 Addendum 2: caught *ahead of* the generic RuntimeError
            # branch below — a `gh` too old to support `pr checks --json` at
            # all is a known, fixable host misconfiguration (upgrade gh on
            # whichever host runs the merge gate), not an auth/network flake.
            # `str(e)` already carries the actionable host + version-floor
            # message built by `github_ops._gh_too_old_message`; surfacing it
            # through a distinctly-named synthetic check (rather than folding
            # it into `_unreadable_check`'s generic wording) means an operator
            # reading the merge refusal never has to guess which of the two
            # this was.
            record_ci_check_fetch(repo, number, outcome="unreachable",
                                   duration_s=time.monotonic() - _t0, detail="gh too old")
            return [_gh_too_old_check(repo, number, str(e))]
        except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError, ValueError) as e:
            # #1525: a `gh pr checks` read that outright failed (gh missing,
            # timeout, non-zero exit with no stdout, unparseable JSON) used
            # to return `[]` here — indistinguishable from "this PR genuinely
            # has no checks configured", which the merge gate treats as
            # clear to merge. That silent fail-open is the mechanism that let
            # PR #1521 merge 11 minutes after `test (3.12)` recorded FAILURE:
            # a transient read failure at exactly the wrong moment read as
            # "no failing checks" instead of "unknown". Return a synthetic
            # failing check instead so the gate blocks and says why; a caller
            # that genuinely wants "unknown" treated as clear must pass
            # `force_merge=True` explicitly.
            record_ci_check_fetch(repo, number, outcome="unreachable",
                                   duration_s=time.monotonic() - _t0, detail=str(e))
            return [_unreadable_check(repo, number, str(e))]
        duration = time.monotonic() - _t0
        if not isinstance(raw, list):
            record_ci_check_fetch(repo, number, outcome="unreachable",
                                   duration_s=duration, detail="non-list JSON")
            return [_unreadable_check(repo, number, "gh pr checks returned non-list JSON")]
        # #2446: `_all_checks` (this method's only caller) is the single
        # unfiltered fetch shared by both `list_checks_for_pr` (narrowed to
        # required contexts) and `list_all_checks_for_pr` (everything) — the
        # required-contexts narrowing used to happen here, which meant an
        # advisory check's regression was invisible everywhere, not just to
        # the merge gate. See both methods' docstrings.
        checks = [
            CheckRun(
                name=str(entry.get("name", "")),
                status=_status_from_bucket(str(entry.get("bucket", ""))),
                conclusion=_conclusion_from_bucket(str(entry.get("bucket", ""))),
                url=str(entry.get("link", "")),
                run_id=_run_id_from_link(str(entry.get("link", ""))),
                started_at=_parse_ts(entry.get("startedAt")),
                completed_at=_parse_ts(entry.get("completedAt")),
            )
            for entry in raw
            if isinstance(entry, dict)
        ]
        conclusions = Counter(c.conclusion or "pending" for c in checks)
        record_ci_check_fetch(repo, number, outcome="ok", duration_s=duration,
                               conclusions=dict(conclusions))
        return checks

    def _required_contexts(self, repo: str) -> frozenset[str] | None:
        now = time.time()
        cached = self._required_contexts_cache.get(repo)
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        contexts = github_ops.get_required_status_check_contexts(repo)
        result = frozenset(contexts) if contexts else None
        self._required_contexts_cache[repo] = (now, result)
        return result


def _unreadable_check(repo: str, number: int, detail: str) -> CheckRun:
    """Synthetic :class:`CheckRun` standing in for "could not read CI" (#1525).

    ``conclusion="unknown"`` is not in :data:`coord.ci_store._PASSING_CONCLUSIONS`,
    so ``failed_checks`` picks this up like any other hard failure — the
    merge gate blocks and the reason (surfaced via ``CheckRun.name``) tells
    the operator this was a read failure, not a real CI failure.
    """
    return CheckRun(
        name=f"coord: could not read CI status for {repo}#{number} ({detail})",
        status="completed",
        conclusion="unknown",
        url="",
        run_id="",
        started_at=None,
        completed_at=None,
    )


def _gh_too_old_check(repo: str, number: int, detail: str) -> CheckRun:
    """Synthetic :class:`CheckRun` for "gh is too old to support `pr checks
    --json` at all" (#1564 Addendum 2) — deliberately worded and named
    differently from :func:`_unreadable_check` so the merge gate's refusal
    is unambiguous about *which* of the two this is: a known, fixable host
    misconfiguration (wrong gh version on the host running the gate), not a
    generic/transient read failure (auth, network, rate-limit). ``detail``
    is :class:`coord.github_ops.GhTooOldForJsonChecks`'s message, which
    already names the offending host and the required gh version.

    Still ``conclusion="unknown"`` (not in
    :data:`coord.ci_store._PASSING_CONCLUSIONS`) so the gate still fails
    closed and blocks the merge — #1525's fail-closed rule is not weakened,
    only the diagnosis attached to the block is sharper.
    """
    return CheckRun(
        name=f"coord: gh is too old to read CI status for {repo}#{number} ({detail})",
        status="completed",
        conclusion="unknown",
        url="",
        run_id="",
        started_at=None,
        completed_at=None,
    )
