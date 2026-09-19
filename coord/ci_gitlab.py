"""GitLab CI backend for :mod:`coord.ci_store` (#1897, Phase 1 of the Forge
Independence program, #239).

Talks to GitLab's Pipelines/Jobs REST API (``/api/v4``) via ``httpx`` — no
new SDK. Selected with ``ci_store: {type: gitlab, host: ..., token_env:
...}`` in ``coordinator.yml`` (see :class:`coord.config.CiStoreConfig`);
``token_env`` names an environment variable holding a GitLab personal/
project access token (``read_api`` scope is enough to read; a token able to
retry pipelines needs write access too). The token itself is never accepted
in ``coordinator.yml`` — same reasoning as the existing ``github``/``gh``
backend, which relies on the ambient ``gh`` auth rather than a config value.

Concept mapping (mirrors :mod:`coord.ci_github`'s ``gh pr checks`` mapping):

======================== =================================================
``CiStore`` concept       GitLab equivalent
======================== =================================================
``CheckRun``              a pipeline **job**
``list_checks_for_pr``    jobs of the pipelines behind an MR's HEAD
                           (``GET .../merge_requests/:iid/pipelines`` then
                           ``GET .../pipelines/:id/jobs`` per pipeline)
``rerun_for_pr``          retry every pipeline behind the MR
                           (``POST .../pipelines/:id/retry`` — GitLab's own
                           retry endpoint only ever retries failed/canceled
                           jobs, never re-runs jobs that already passed, so
                           this and ``rerun_failed_for_pr`` below end up
                           calling the identical endpoint)
``rerun_failed_for_pr``   retry only the pipelines with a currently-failing
                           job (same endpoint, narrower pipeline-id set)
``is_available``          token configured AND a cheap reachability probe
                           succeeds (never raises)
======================== =================================================

Status mapping (do NOT widen ``_PASSING_CONCLUSIONS``, #1897's own warning):
see :func:`_map_status` and the table above it. Only GitLab's ``success``
and ``skipped`` map to a conclusion in
:data:`coord.ci_store._PASSING_CONCLUSIONS`; every other known status, and
any status this module has never seen, maps to something that blocks.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from coord.ci_store import CheckRun, JobRun, RunSummary, failed_checks
from coord.forge_availability import record_ci_check_fetch

DEFAULT_HOST = "gitlab.com"
DEFAULT_TOKEN_ENV = "GITLAB_TOKEN"
_REQUEST_TIMEOUT = 15.0

# ── status mapping (#1897) ──────────────────────────────────────────────────
#
# GitLab job statuses (https://docs.gitlab.com/ee/api/jobs.html and the
# pipeline status enum): created, pending, running, failed, success,
# canceled, skipped, manual, waiting_for_resource, preparing, scheduled.
#
# `coord.ci_store._PASSING_CONCLUSIONS` is a fail-closed ALLOW-list
# (success/skipped/neutral, #1525) — this table maps INTO that vocabulary,
# never widens it:
#
# - `success`  -> completed/"success"   (passing)
# - `skipped`  -> completed/"skipped"   (passing — matches GitHub's own
#                 `skipped` treatment: a job the pipeline config chose not
#                 to run at all isn't a code failure)
# - `failed`   -> completed/"failure"   (blocking)
# - `canceled` -> completed/"cancelled" (blocking — same spelling `_gh`'s
#                 own "cancel" bucket maps to, so both backends agree)
# - `manual`   -> completed/"action_required" (blocking — #1897's own
#                 warning: "a job awaiting manual trigger has produced no
#                 verdict"; GitHub's own "action_required" conclusion is
#                 already outside `_PASSING_CONCLUSIONS`, so reusing that
#                 word keeps the two backends' non-passing vocabularies
#                 aligned instead of inventing a GitLab-only string)
# - `created`  -> completed/"action_required" (blocking — same reasoning:
#                 a job GitLab hasn't even scheduled yet has produced no
#                 verdict either, and treating it as perpetually "pending"
#                 would let it sit forever without ever registering as a
#                 block a caller who only inspects `failed_checks()` sees)
# - `pending`/`running`/`waiting_for_resource`/`preparing`/`scheduled`
#              -> in_progress (genuinely in-flight — GitHub's own
#                 "queued"/"in_progress" analogue)
#
# Anything else (a future GitLab status this module has never seen) maps to
# completed + conclusion=<the raw status string> — which is, by
# construction, never in `_PASSING_CONCLUSIONS`, so it blocks exactly like
# every other unrecognised conclusion. This is the literal analogue of
# #1525's rule for `coord.ci_github`'s synthetic `"unknown"` conclusion.
_GITLAB_COMPLETED_CONCLUSIONS: dict[str, str] = {
    "success": "success",
    "skipped": "skipped",
    "failed": "failure",
    "canceled": "cancelled",
    "manual": "action_required",
    "created": "action_required",
}

_GITLAB_IN_FLIGHT_STATUSES = frozenset({
    "pending", "running", "waiting_for_resource", "preparing", "scheduled",
})


def _map_status(gitlab_status: str) -> tuple[str, str | None]:
    """Map a GitLab job ``status`` string to a ``(CheckRun.status,
    CheckRun.conclusion)`` pair — see the module docstring's table."""
    if gitlab_status in _GITLAB_COMPLETED_CONCLUSIONS:
        return "completed", _GITLAB_COMPLETED_CONCLUSIONS[gitlab_status]
    if gitlab_status in _GITLAB_IN_FLIGHT_STATUSES:
        return "in_progress", None
    return "completed", gitlab_status


def _parse_ts(raw: str | None) -> float | None:
    """Parse an ISO-8601 timestamp as GitLab emits it (e.g.
    ``2026-05-24T12:34:56.789Z``). Returns ``None`` for empty/unparseable
    input, mirroring :func:`coord.ci_github._parse_ts`."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _project_path(repo: str) -> str:
    """URL-encode *repo* (``group/subgroup/project``) as a GitLab project
    id per the API's ``:id`` convention — a namespaced path with every
    ``/`` percent-encoded."""
    return urllib.parse.quote(repo, safe="")


@dataclass
class GitLabCi:
    """Talk to GitLab's Pipelines/Jobs API and cache results briefly.

    Mirrors :class:`coord.ci_github.GitHubCi`'s shape (per-(repo, number)
    check cache with ``cache_ttl``, best-effort rerun, fail-closed synthetic
    checks on a read failure) so the merge gate treats both backends
    identically wherever the mapping allows.
    """

    host: str = DEFAULT_HOST
    token_env: str = DEFAULT_TOKEN_ENV
    cache_ttl: float = 10.0
    timeout: float = _REQUEST_TIMEOUT
    _cache: dict[tuple[str, int], tuple[float, list[CheckRun]]] = field(
        default_factory=dict, repr=False
    )
    # repo -> (fetched_at, expects_checks) — mirrors GitHubCi's
    # `_workflow_cache`: whether a project has ANY pipelines at all is a
    # repo-wide property, not a per-MR one.
    _pipelines_cache: dict[str, tuple[float, bool]] = field(default_factory=dict, repr=False)
    # Cached separately from `_cache` (a different TTL clock than the check
    # data) since `is_available` is read far more often, in far hotter loops
    # (see its own docstring), than any single PR's checks.
    _availability_cache: tuple[float, bool] | None = field(default=None, init=False, repr=False)

    @property
    def _token(self) -> str:
        return os.environ.get(self.token_env, "") or ""

    def _api_base(self) -> str:
        return f"https://{self.host}/api/v4"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._api_base(),
            headers={"PRIVATE-TOKEN": self._token},
            timeout=self.timeout,
        )

    @property
    def is_available(self) -> bool:
        """True when a token is configured AND a cheap reachability probe
        to *host* succeeds (#1897).

        Cached for ``cache_ttl`` seconds: this property is read on nearly
        every merge-gate evaluation (see ``coord.merge_queue``'s several
        ``ci_store.is_available`` guards, each potentially iterating many
        pending PRs per tick) — an uncached network round trip here would
        turn a cheap in-memory check into a per-PR network call. Never
        raises: any exception talking to GitLab (DNS, TLS, timeout, a 5xx)
        reads as unavailable, same as a missing token — the merge gate then
        skips the CI block entirely for this backend, exactly as it does
        when ``ci_store: {type: none}``.
        """
        if not self._token:
            return False
        now = time.time()
        cached = self._availability_cache
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        available = self._probe_reachable()
        self._availability_cache = (now, available)
        return available

    def _probe_reachable(self) -> bool:
        try:
            with self._client() as client:
                resp = client.get("/version")
        except httpx.HTTPError:
            return False
        return resp.status_code < 500

    # ── reads ────────────────────────────────────────────────────────────

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        """Jobs of every pipeline behind MR ``!``\\ *number* in *repo*.

        GitLab has no direct analogue of GitHub's branch-protection
        "required status checks" narrowing (:meth:`coord.ci_github.
        GitHubCi.list_checks_for_pr`) — every backend's ``CiStore`` contract
        only requires the two views to *exist*, not to differ, so this
        returns the same set :meth:`list_all_checks_for_pr` does.
        """
        return self._all_checks(repo, number)

    def list_all_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        """Every job GitLab reports for *repo*'s MR *number* (#2446's
        unfiltered view) — see :meth:`list_checks_for_pr`'s docstring for
        why this backend has nothing narrower to offer yet."""
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

    def _fetch(self, repo: str, number: int) -> list[CheckRun]:
        _t0 = time.monotonic()
        token = self._token
        if not token:
            record_ci_check_fetch(
                repo, number, outcome="unreachable", duration_s=0.0,
                detail=f"{self.token_env} not set",
            )
            return [_unconfigured_check(repo, number, self.token_env)]
        project = _project_path(repo)
        try:
            checks: list[CheckRun] = []
            with self._client() as client:
                resp = client.get(f"/projects/{project}/merge_requests/{number}/pipelines")
                resp.raise_for_status()
                pipelines = resp.json()
                if not isinstance(pipelines, list):
                    raise ValueError("non-list pipelines JSON")
                for pipeline in pipelines:
                    if not isinstance(pipeline, dict):
                        continue
                    pipeline_id = pipeline.get("id")
                    if pipeline_id is None:
                        continue
                    jobs_resp = client.get(
                        f"/projects/{project}/pipelines/{pipeline_id}/jobs",
                        params={"per_page": 100},
                    )
                    jobs_resp.raise_for_status()
                    jobs = jobs_resp.json()
                    if not isinstance(jobs, list):
                        continue
                    for job in jobs:
                        if isinstance(job, dict):
                            checks.append(_job_to_check(job, pipeline_id))
        except (httpx.HTTPError, ValueError) as e:
            # #1525's rule applied to GitLab: a read that outright failed
            # (network down, bad token, malformed JSON) must never silently
            # read as "no checks" — that is exactly the fail-open shape
            # that let PR #1521 merge over a red check. Return a synthetic
            # failing check instead.
            record_ci_check_fetch(
                repo, number, outcome="unreachable",
                duration_s=time.monotonic() - _t0, detail=str(e),
            )
            return [_unreadable_check(repo, number, str(e))]
        duration = time.monotonic() - _t0
        conclusions = Counter(c.conclusion or "pending" for c in checks)
        record_ci_check_fetch(repo, number, outcome="ok", duration_s=duration,
                               conclusions=dict(conclusions))
        return checks

    def expects_checks(self, repo: str, number: int) -> bool:
        """True when *repo* has at least one recorded GitLab pipeline
        (#1904's "was CI ever configured for this repo" question).

        *number* is accepted only to satisfy :class:`coord.ci_store.
        CiStore`'s shape — like :meth:`coord.ci_github.GitHubCi.
        expects_checks`, the answer is repo-wide, not per-MR, and cached as
        such. Fails closed (returns ``True``) on any read failure or a
        missing token — unknown must read as "checks were expected", never
        as the free pass that let an empty ``checks`` list merge untested
        code in the first place (#1525).
        """
        now = time.time()
        cached = self._pipelines_cache.get(repo)
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        result = self._fetch_expects_checks(repo)
        self._pipelines_cache[repo] = (now, result)
        return result

    def _fetch_expects_checks(self, repo: str) -> bool:
        token = self._token
        if not token:
            return True  # fail closed — see expects_checks' docstring
        try:
            with self._client() as client:
                resp = client.get(
                    f"/projects/{_project_path(repo)}/pipelines",
                    params={"per_page": 1},
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            return True  # fail closed
        return bool(isinstance(data, list) and len(data) > 0)

    def list_jobs_for_run(self, repo: str, run_id: str) -> list[JobRun]:
        """Best-effort job list for GitLab pipeline *run_id* (#1897/#1892).

        GitLab's Jobs API has no per-step breakdown the way GitHub Actions
        does (see :class:`coord.ci_store.JobRun`/``JobStep``) — every
        ``JobRun`` returned here has ``steps=[]``. Callers (``coord.
        merge_queue``'s ``is_verdictless_job`` classification) already treat
        "no job/step data" as the safe false-negative default ("not
        verdictless"), so this is a faithful best-effort answer, not a
        stub — see that function's own docstring for the bias. Any read
        failure (missing token, network error, malformed JSON, an expired
        pipeline id) returns ``[]`` rather than raising.
        """
        token = self._token
        if not token:
            return []
        try:
            with self._client() as client:
                resp = client.get(
                    f"/projects/{_project_path(repo)}/pipelines/{run_id}/jobs",
                    params={"per_page": 100},
                )
                resp.raise_for_status()
                jobs = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        if not isinstance(jobs, list):
            return []
        out: list[JobRun] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            _, conclusion = _map_status(str(job.get("status", "")))
            out.append(JobRun(
                name=str(job.get("name", "")), conclusion=conclusion,
                runner_name="", steps=[],
            ))
        return out

    def list_runs_for_branch(
        self, repo: str, branch: str, *, event: str | None = None, limit: int = 20,
    ) -> list[RunSummary]:
        """The last *limit* pipelines run against *branch* on *repo* (#3405),
        newest first, via GitLab's project pipelines list API.

        *event* maps to GitLab's pipeline ``source`` field (``"push"``,
        ``"web"``, ``"schedule"``, ``"merge_request_event"``, ...) — the
        closest GitLab concept to GitHub Actions' ``event``. Best-effort like
        :meth:`list_jobs_for_run`: any read failure (missing token, network
        error, malformed JSON) degrades to ``[]`` rather than raising, since
        this is a visibility read, not a gate.
        """
        token = self._token
        if not token:
            return []
        params: dict[str, object] = {
            "ref": branch, "per_page": limit, "order_by": "id", "sort": "desc",
        }
        if event:
            params["source"] = event
        try:
            with self._client() as client:
                resp = client.get(
                    f"/projects/{_project_path(repo)}/pipelines", params=params,
                )
                resp.raise_for_status()
                pipelines = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        if not isinstance(pipelines, list):
            return []
        runs: list[RunSummary] = []
        for pipeline in pipelines:
            if not isinstance(pipeline, dict):
                continue
            status, conclusion = _map_status(str(pipeline.get("status", "")))
            runs.append(
                RunSummary(
                    run_id=str(pipeline.get("id", "") or ""),
                    name=str(pipeline.get("name") or pipeline.get("source") or ""),
                    status=status,
                    conclusion=conclusion,
                    event=str(pipeline.get("source") or ""),
                    branch=str(pipeline.get("ref") or branch),
                    url=str(pipeline.get("web_url") or ""),
                    created_at=_parse_ts(pipeline.get("created_at")),
                )
            )
        return runs

    # ── writes ───────────────────────────────────────────────────────────

    def rerun_for_pr(self, repo: str, number: int) -> bool:
        """Retry every pipeline behind *repo*'s MR *number* (#1897/#1851).

        See the module docstring: GitLab's own pipeline-retry endpoint only
        ever retries failed/canceled jobs, so this and
        :meth:`rerun_failed_for_pr` differ only in which pipeline ids they
        target — every pipeline with any check here, vs only pipelines with
        a currently-failing check there.
        """
        return self._retry_pipelines(repo, number, only_failed=False)

    def rerun_failed_for_pr(self, repo: str, number: int) -> bool:
        """Retry only the pipelines with a currently-failing job behind
        *repo*'s MR *number* (#1897/#2252)."""
        return self._retry_pipelines(repo, number, only_failed=True)

    def _retry_pipelines(self, repo: str, number: int, *, only_failed: bool) -> bool:
        token = self._token
        if not token:
            return False
        checks = self.list_checks_for_pr(repo, number)
        if only_failed:
            checks = failed_checks(checks)
        pipeline_ids = sorted({c.run_id for c in checks if c.run_id})
        if not pipeline_ids:
            return False
        project = _project_path(repo)
        all_ok = True
        any_ok = False
        try:
            with self._client() as client:
                for pipeline_id in pipeline_ids:
                    try:
                        resp = client.post(
                            f"/projects/{project}/pipelines/{pipeline_id}/retry"
                        )
                    except httpx.HTTPError:
                        all_ok = False
                        continue
                    if resp.status_code < 400:
                        any_ok = True
                    else:
                        all_ok = False
        except httpx.HTTPError:
            return False
        if any_ok:
            self.invalidate(repo, number)
        return all_ok and any_ok

    def invalidate(self, repo: str | None = None, number: int | None = None) -> None:
        """Drop cached check entries — pass nothing to clear everything.

        Mirrors :meth:`coord.ci_github.GitHubCi.invalidate`; consumed by
        :func:`coord.ci_store.wait_for_ci_settle` after a rerun.
        """
        if repo is None and number is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            if repo is not None and key[0] != repo:
                continue
            if number is not None and key[1] != number:
                continue
            del self._cache[key]


def _job_to_check(job: dict, pipeline_id: object) -> CheckRun:
    status, conclusion = _map_status(str(job.get("status", "")))
    return CheckRun(
        name=str(job.get("name", "")),
        status=status,
        conclusion=conclusion,
        url=str(job.get("web_url", "") or ""),
        run_id=str(pipeline_id),
        started_at=_parse_ts(job.get("started_at")),
        completed_at=_parse_ts(job.get("finished_at")),
    )


def _unreadable_check(repo: str, number: int, detail: str) -> CheckRun:
    """Synthetic :class:`CheckRun` standing in for "could not read CI"
    (#1897, mirroring #1525's :func:`coord.ci_github._unreadable_check`).

    ``conclusion="unknown"`` is not in
    :data:`coord.ci_store._PASSING_CONCLUSIONS`, so ``failed_checks`` picks
    this up like any other hard failure. The name deliberately reuses the
    exact "could not read CI status" / "coord: " phrasing
    :func:`coord.ci_store.is_unreadable_check` matches on, so this backend's
    unreadable checks are recognised by that shared classifier too.
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


def _unconfigured_check(repo: str, number: int, token_env: str) -> CheckRun:
    """Synthetic :class:`CheckRun` for "no GitLab token configured" (#1897).

    Defence in depth alongside :attr:`GitLabCi.is_available` (which already
    reads ``False`` when the token is missing, so the merge gate's own
    ``ci_store.is_available`` guards skip the CI block before ever calling
    this) — any direct caller of :meth:`GitLabCi.list_checks_for_pr` that
    bypasses that guard still gets a fail-closed blocking check rather than
    a silent empty list.
    """
    return CheckRun(
        name=f"coord: GitLab CI not configured for {repo}#{number} (${token_env} not set)",
        status="completed",
        conclusion="unknown",
        url="",
        run_id="",
        started_at=None,
        completed_at=None,
    )
