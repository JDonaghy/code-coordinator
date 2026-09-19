"""Black-box tests for coord.ci_gitlab (#1897, Phase 1 of the Forge
Independence program, #239).

Drives :class:`coord.ci_gitlab.GitLabCi` against recorded-shape GitLab
Pipelines/Jobs API responses via ``httpx.MockTransport`` — no real network,
but real ``httpx`` request/response parsing, matching this repo's CLAUDE.md
black-box coverage bar. ``GitLabCi._client`` is monkeypatched per test to
route through the mock transport; nothing else about the production code
path changes.

Covers:
- Status mapping (#1897's own table) — every documented GitLab job status,
  plus an unrecognised future status blocking rather than passing.
- `list_checks_for_pr` / `list_all_checks_for_pr` field mapping + caching.
- `is_available` — missing token, unreachable host, reachable+configured;
  never raises.
- `expects_checks` — fail-closed on missing token / read failure.
- `rerun_for_pr` / `rerun_failed_for_pr` — which pipelines get retried.
- `list_jobs_for_run` — best-effort job list.
- `build_ci_store("gitlab", ...)` and `coordinator.yml` config parsing.
"""

from __future__ import annotations

import httpx
import pytest

from coord.ci_gitlab import GitLabCi, _map_status
from coord.ci_store import CheckRun, _PASSING_CONCLUSIONS, build_ci_store, failed_checks


# ── status mapping ───────────────────────────────────────────────────────────

class TestMapStatus:
    """#1897: every GitLab job status this module knows about, mapped
    explicitly — plus the fail-closed default for anything it doesn't."""

    @pytest.mark.parametrize(
        "gitlab_status,expected_status,expected_conclusion",
        [
            ("success", "completed", "success"),
            ("skipped", "completed", "skipped"),
            ("failed", "completed", "failure"),
            ("canceled", "completed", "cancelled"),
            ("manual", "completed", "action_required"),
            ("created", "completed", "action_required"),
            ("pending", "in_progress", None),
            ("running", "in_progress", None),
        ],
    )
    def test_every_documented_status(
        self, gitlab_status, expected_status, expected_conclusion
    ) -> None:
        status, conclusion = _map_status(gitlab_status)
        assert status == expected_status
        assert conclusion == expected_conclusion

    @pytest.mark.parametrize("gitlab_status", ["success", "skipped"])
    def test_only_success_and_skipped_are_passing(self, gitlab_status) -> None:
        _, conclusion = _map_status(gitlab_status)
        assert conclusion in _PASSING_CONCLUSIONS

    @pytest.mark.parametrize(
        "gitlab_status", ["failed", "canceled", "manual", "created"],
    )
    def test_the_rest_of_the_completed_set_is_blocking(self, gitlab_status) -> None:
        status, conclusion = _map_status(gitlab_status)
        assert status == "completed"
        assert conclusion not in _PASSING_CONCLUSIONS

    def test_unrecognised_future_status_blocks_not_passes(self) -> None:
        # #1897: an unrecognised future GitLab status must map to
        # something `failed_checks` catches, never to something that reads
        # as passing (#1525's fail-closed rule, applied to GitLab).
        status, conclusion = _map_status("some_status_gitlab_invents_later")
        assert status == "completed"
        assert conclusion == "some_status_gitlab_invents_later"
        assert conclusion not in _PASSING_CONCLUSIONS
        check = CheckRun(
            name="build", status=status, conclusion=conclusion, url="",
            run_id="1", started_at=None, completed_at=None,
        )
        assert failed_checks([check]) == [check]


# ── HTTP plumbing helpers ────────────────────────────────────────────────────

def _install_transport(monkeypatch, store: GitLabCi, handler) -> list[httpx.Request]:
    """Route *store*'s HTTP calls through an in-memory `httpx.MockTransport`
    instead of a real socket. Returns the list of requests seen so far
    (mutated in place as calls happen) so tests can assert on call counts /
    URLs without a real network."""
    seen: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _client() -> httpx.Client:
        return httpx.Client(
            base_url=store._api_base(),
            headers={"PRIVATE-TOKEN": store._token},
            transport=httpx.MockTransport(_wrapped),
        )

    monkeypatch.setattr(store, "_client", _client)
    return seen


def _job(name: str, status: str, *, web_url: str = "", started_at=None,
          finished_at=None) -> dict:
    return {
        "name": name, "status": status, "web_url": web_url,
        "started_at": started_at, "finished_at": finished_at,
    }


# ── list_checks_for_pr / list_all_checks_for_pr ─────────────────────────────

class TestGitLabCiListChecksForPr:
    def test_maps_fields_across_one_pipeline(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/merge_requests/42/pipelines"):
                return httpx.Response(200, json=[{"id": 55}])
            if request.url.path.endswith("/pipelines/55/jobs"):
                return httpx.Response(200, json=[
                    _job("test (3.12)", "failed",
                         web_url="https://gitlab.example/j/1",
                         started_at="2026-05-24T12:00:00.000Z",
                         finished_at="2026-05-24T12:05:00.000Z"),
                    _job("lint", "success"),
                    _job("deploy-preview", "pending"),
                ])
            raise AssertionError(f"unexpected request: {request.url}")

        _install_transport(monkeypatch, store, handler)
        checks = store.list_checks_for_pr("acme/api", 42)

        assert len(checks) == 3
        by_name = {c.name: c for c in checks}
        assert by_name["test (3.12)"].status == "completed"
        assert by_name["test (3.12)"].conclusion == "failure"
        assert by_name["test (3.12)"].url == "https://gitlab.example/j/1"
        assert by_name["test (3.12)"].run_id == "55"
        assert isinstance(by_name["test (3.12)"].started_at, float)
        assert by_name["lint"].conclusion == "success"
        assert by_name["deploy-preview"].status == "in_progress"
        assert by_name["deploy-preview"].conclusion is None

    def test_spans_multiple_pipelines(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pipelines"):
                return httpx.Response(200, json=[{"id": 10}, {"id": 11}])
            if request.url.path.endswith("/pipelines/10/jobs"):
                return httpx.Response(200, json=[_job("unit", "success")])
            if request.url.path.endswith("/pipelines/11/jobs"):
                return httpx.Response(200, json=[_job("e2e", "failed")])
            raise AssertionError(f"unexpected request: {request.url}")

        _install_transport(monkeypatch, store, handler)
        checks = store.list_checks_for_pr("acme/api", 7)
        run_ids = {c.run_id for c in checks}
        assert run_ids == {"10", "11"}

    def test_list_all_checks_for_pr_same_as_list_checks_for_pr(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK", cache_ttl=60.0)
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pipelines"):
                return httpx.Response(200, json=[{"id": 1}])
            if request.url.path.endswith("/pipelines/1/jobs"):
                return httpx.Response(200, json=[_job("build", "success")])
            raise AssertionError(f"unexpected request: {request.url}")

        _install_transport(monkeypatch, store, handler)
        a = store.list_checks_for_pr("acme/api", 3)
        b = store.list_all_checks_for_pr("acme/api", 3)
        assert a == b

    def test_cache_hit_makes_no_second_call(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK", cache_ttl=60.0)
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pipelines"):
                return httpx.Response(200, json=[{"id": 1}])
            if request.url.path.endswith("/pipelines/1/jobs"):
                return httpx.Response(200, json=[_job("build", "success")])
            raise AssertionError(f"unexpected request: {request.url}")

        seen = _install_transport(monkeypatch, store, handler)
        store.list_checks_for_pr("acme/api", 3)
        n_after_first = len(seen)
        store.list_checks_for_pr("acme/api", 3)
        assert len(seen) == n_after_first

    def test_missing_token_returns_synthetic_blocking_check_no_network(
        self, monkeypatch
    ) -> None:
        store = GitLabCi(token_env="GL_TOK_MISSING")
        monkeypatch.delenv("GL_TOK_MISSING", raising=False)

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call with no token")

        _install_transport(monkeypatch, store, handler)
        checks = store.list_checks_for_pr("acme/api", 1)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"
        assert checks[0].conclusion not in _PASSING_CONCLUSIONS
        assert "GL_TOK_MISSING" in checks[0].name

    def test_unreachable_host_returns_synthetic_blocking_check(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _install_transport(monkeypatch, store, handler)
        checks = store.list_checks_for_pr("acme/api", 1)
        assert len(checks) == 1
        assert checks[0].status == "completed"
        assert checks[0].conclusion == "unknown"
        assert "could not read CI status" in checks[0].name

    def test_non_list_json_treated_as_unreadable(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not": "a list"})

        _install_transport(monkeypatch, store, handler)
        checks = store.list_checks_for_pr("acme/api", 1)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"


# ── is_available ─────────────────────────────────────────────────────────────

class TestGitLabCiIsAvailable:
    def test_no_token_is_unavailable(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK_UNSET")
        monkeypatch.delenv("GL_TOK_UNSET", raising=False)
        assert store.is_available is False

    def test_reachable_and_configured_is_available(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"version": "17.0"})

        _install_transport(monkeypatch, store, handler)
        assert store.is_available is True

    def test_unreachable_host_is_unavailable_and_never_raises(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        _install_transport(monkeypatch, store, handler)
        assert store.is_available is False  # must not raise

    def test_server_error_is_unavailable(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        _install_transport(monkeypatch, store, handler)
        assert store.is_available is False

    def test_cached_within_ttl(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK", cache_ttl=60.0)
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        seen = _install_transport(monkeypatch, store, handler)
        assert store.is_available is True
        assert store.is_available is True
        assert len(seen) == 1


# ── expects_checks ───────────────────────────────────────────────────────────

class TestGitLabCiExpectsChecks:
    def test_true_when_pipelines_exist(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"id": 1}])

        _install_transport(monkeypatch, store, handler)
        assert store.expects_checks("acme/api", 1) is True

    def test_false_when_no_pipelines_ever_ran(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        _install_transport(monkeypatch, store, handler)
        assert store.expects_checks("acme/api", 1) is False

    def test_fails_closed_with_no_token(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK_UNSET")
        monkeypatch.delenv("GL_TOK_UNSET", raising=False)
        assert store.expects_checks("acme/api", 1) is True

    def test_fails_closed_on_read_error(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        _install_transport(monkeypatch, store, handler)
        assert store.expects_checks("acme/api", 1) is True


# ── rerun_for_pr / rerun_failed_for_pr ───────────────────────────────────────

class TestGitLabCiRerun:
    def test_rerun_for_pr_retries_every_pipeline(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")
        retried: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/pipelines") and request.method == "GET":
                return httpx.Response(200, json=[{"id": 1}, {"id": 2}])
            if path.endswith("/pipelines/1/jobs"):
                return httpx.Response(200, json=[_job("build", "success")])
            if path.endswith("/pipelines/2/jobs"):
                return httpx.Response(200, json=[_job("test", "failed")])
            if path.endswith("/retry") and request.method == "POST":
                retried.append(path)
                return httpx.Response(201, json={"id": 999})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        _install_transport(monkeypatch, store, handler)
        assert store.rerun_for_pr("acme/api", 9) is True
        assert sorted(retried) == [
            "/api/v4/projects/acme/api/pipelines/1/retry",
            "/api/v4/projects/acme/api/pipelines/2/retry",
        ]

    def test_rerun_failed_for_pr_retries_only_failing_pipeline(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")
        retried: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/pipelines") and request.method == "GET":
                return httpx.Response(200, json=[{"id": 1}, {"id": 2}])
            if path.endswith("/pipelines/1/jobs"):
                return httpx.Response(200, json=[_job("build", "success")])
            if path.endswith("/pipelines/2/jobs"):
                return httpx.Response(200, json=[_job("test", "failed")])
            if path.endswith("/retry") and request.method == "POST":
                retried.append(path)
                return httpx.Response(201, json={"id": 999})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        _install_transport(monkeypatch, store, handler)
        assert store.rerun_failed_for_pr("acme/api", 9) is True
        assert retried == ["/api/v4/projects/acme/api/pipelines/2/retry"]

    def test_rerun_returns_false_with_no_checks(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pipelines"):
                return httpx.Response(200, json=[])
            raise AssertionError(f"unexpected request: {request.url}")

        _install_transport(monkeypatch, store, handler)
        assert store.rerun_for_pr("acme/api", 9) is False

    def test_rerun_returns_false_when_retry_call_fails(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/pipelines") and request.method == "GET":
                return httpx.Response(200, json=[{"id": 1}])
            if path.endswith("/pipelines/1/jobs"):
                return httpx.Response(200, json=[_job("build", "failed")])
            if path.endswith("/retry"):
                return httpx.Response(403)
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        _install_transport(monkeypatch, store, handler)
        assert store.rerun_for_pr("acme/api", 9) is False

    def test_rerun_with_no_token_is_false_no_network(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK_UNSET")
        monkeypatch.delenv("GL_TOK_UNSET", raising=False)

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call with no token")

        _install_transport(monkeypatch, store, handler)
        assert store.rerun_for_pr("acme/api", 9) is False


# ── list_jobs_for_run ─────────────────────────────────────────────────────

class TestGitLabCiListJobsForRun:
    def test_maps_jobs_with_empty_steps(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pipelines/55/jobs"):
                return httpx.Response(200, json=[
                    _job("build", "failed"), _job("lint", "success"),
                ])
            raise AssertionError(f"unexpected request: {request.url}")

        _install_transport(monkeypatch, store, handler)
        jobs = store.list_jobs_for_run("acme/api", "55")
        assert len(jobs) == 2
        by_name = {j.name: j for j in jobs}
        assert by_name["build"].conclusion == "failure"
        assert by_name["build"].steps == []
        assert by_name["lint"].conclusion == "success"

    def test_no_token_returns_empty(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK_UNSET")
        monkeypatch.delenv("GL_TOK_UNSET", raising=False)
        assert store.list_jobs_for_run("acme/api", "55") == []

    def test_read_error_returns_empty(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        _install_transport(monkeypatch, store, handler)
        assert store.list_jobs_for_run("acme/api", "55") == []


def _pipeline(pid: int, *, ref: str = "main", source: str = "push",
              status: str = "success", web_url: str = "",
              created_at: str | None = None) -> dict:
    return {
        "id": pid, "ref": ref, "source": source, "status": status,
        "web_url": web_url, "created_at": created_at,
    }


class TestGitLabCiListRunsForBranch:
    """#3405: the branch/event-scoped read — GitLab's own concept of a
    push-to-`main` run is a pipeline with ``ref == branch``, which no
    existing (PR-scoped) `GitLabCi` method could express."""

    def test_maps_pipeline_fields(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pipelines"):
                assert request.url.params["ref"] == "main"
                return httpx.Response(200, json=[
                    _pipeline(123, status="failed", web_url="https://gl/p/123",
                              created_at="2026-09-19T00:00:00.000Z"),
                    _pipeline(124, status="skipped"),
                ])
            raise AssertionError(f"unexpected request: {request.url}")

        _install_transport(monkeypatch, store, handler)
        runs = store.list_runs_for_branch("acme/api", "main")

        assert [r.run_id for r in runs] == ["123", "124"]
        failed_run = runs[0]
        assert failed_run.conclusion == "failure"
        assert failed_run.event == "push"
        assert failed_run.branch == "main"
        assert failed_run.url == "https://gl/p/123"
        assert failed_run.created_at is not None
        assert runs[1].conclusion == "skipped"

    def test_event_forwarded_as_source_param(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")
        seen_params: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(request.url.params)
            return httpx.Response(200, json=[])

        _install_transport(monkeypatch, store, handler)
        store.list_runs_for_branch("acme/api", "main", event="push")
        assert seen_params.get("source") == "push"

    def test_no_token_returns_empty(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK_UNSET")
        monkeypatch.delenv("GL_TOK_UNSET", raising=False)
        assert store.list_runs_for_branch("acme/api", "main") == []

    def test_read_error_returns_empty(self, monkeypatch) -> None:
        store = GitLabCi(token_env="GL_TOK")
        monkeypatch.setenv("GL_TOK", "secret")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        _install_transport(monkeypatch, store, handler)
        assert store.list_runs_for_branch("acme/api", "main") == []


# ── build_ci_store / config ──────────────────────────────────────────────────

class TestBuildCiStoreGitlab:
    def test_gitlab_selects_gitlabci_with_defaults(self) -> None:
        store = build_ci_store("gitlab")
        assert isinstance(store, GitLabCi)
        assert store.host == "gitlab.com"
        assert store.token_env == "GITLAB_TOKEN"

    def test_gitlab_forwards_host_and_token_env(self) -> None:
        store = build_ci_store(
            "gitlab", host="gitlab.example.com", token_env="MY_GL_TOKEN"
        )
        assert isinstance(store, GitLabCi)
        assert store.host == "gitlab.example.com"
        assert store.token_env == "MY_GL_TOKEN"

    def test_github_and_none_unaffected_by_new_kwargs(self) -> None:
        from coord.ci_github import GitHubCi
        from coord.ci_store import NoOpCi

        assert isinstance(
            build_ci_store("github", host="ignored", token_env="ignored"), GitHubCi
        )
        assert isinstance(build_ci_store("none"), NoOpCi)


class TestParseCiStoreGitlab:
    def test_explicit_gitlab_with_host_and_token_env(self) -> None:
        from coord.config import _parse_ci_store

        cfg = _parse_ci_store({
            "type": "gitlab", "host": "gitlab.example.com", "token_env": "MY_TOKEN",
        })
        assert cfg.type == "gitlab"
        assert cfg.host == "gitlab.example.com"
        assert cfg.token_env == "MY_TOKEN"

    def test_gitlab_defaults_host_and_token_env(self) -> None:
        from coord.config import _parse_ci_store

        cfg = _parse_ci_store({"type": "gitlab"})
        assert cfg.type == "gitlab"
        assert cfg.host == "gitlab.com"
        assert cfg.token_env == "GITLAB_TOKEN"

    def test_invalid_host_raises(self) -> None:
        from coord.config import ConfigError, _parse_ci_store

        with pytest.raises(ConfigError):
            _parse_ci_store({"type": "gitlab", "host": ""})

    def test_invalid_token_env_raises(self) -> None:
        from coord.config import ConfigError, _parse_ci_store

        with pytest.raises(ConfigError):
            _parse_ci_store({"type": "gitlab", "token_env": 5})
