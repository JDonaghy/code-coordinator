"""#2035: the auto-release must publish, and must not go green when it didn't.

PKG-7's first fully-automatic release pushed `refs/tags/v0.5.2`, published
nothing, and reported `success`. Two independent defects, and this file has a
layer for each:

* ``TestAutoReleaseInvokesPublish`` parses the real workflow YAML and pins the
  *trigger* fix. A tag pushed with the default ``GITHUB_TOKEN`` cannot start a
  workflow (GitHub's recursion guard), so the release must reach ``publish.yml``
  by calling it, not by hoping a ``push: tags`` event fires. These are
  grep-shaped tests on purpose (cf. tests/test_release_unified_1242.py): you
  cannot run GitHub Actions under pytest, and the regression being guarded —
  someone deleting the ``uses:`` job and going back to "just push the tag" —
  reads as perfectly sensible YAML in isolation.

* ``TestVerifyReleasePublished`` drives ``scripts/verify_release_published.py``
  with an injected fetcher and clock. That script is the *post-condition*, and
  it is the half that outlives the trigger: whatever fires the publish, the run
  must not report success until the version is servable from the PyPI simple
  index. The v0.5.2 incident cost a day precisely because green meant "no step
  exited nonzero" rather than "the release exists".

The two must not be collapsed. Fixing only the trigger leaves the next silent
no-op undetectable; fixing only the post-condition leaves every release red.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
AUTO_RELEASE_YML = WORKFLOW_DIR / "auto-release.yml"
PUBLISH_YML = WORKFLOW_DIR / "publish.yml"
# #2898: coord-tui's workflow left this repo's `.github/workflows/` — it is
# staged inside `tui/` for #2894's move story, fires on its own `v*` push, and
# is no longer reachable from publish.yml at all. See
# tests/test_release_unified_1242.py::TestSplitReleaseChannels.

sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_release_published import (  # noqa: E402 - needs the sys.path line above
    VerificationError,
    format_failure,
    index_has_version,
    index_url_for,
    main,
    package_name_from_pyproject,
    version_from_tag,
    wait_for_github_release,
    wait_for_pypi,
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(workflow: dict) -> dict:
    # YAML 1.1 parses a bare `on:` key as the boolean True.
    on = workflow.get("on", workflow.get(True))
    assert isinstance(on, dict), f"expected a mapping of triggers, got {on!r}"
    return on


def _steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job].get("steps") or []


def _run_text(workflow: dict, job: str) -> str:
    """The shell a job actually executes, with `#` comment lines stripped —
    these tests assert on behaviour, and a comment explaining why something is
    *not* used must not read as a use of it."""
    lines = [
        line
        for step in _steps(workflow, job)
        for line in step.get("run", "").splitlines()
        if not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# layer 1: the trigger — auto-release must CALL publish
# ──────────────────────────────────────────────────────────────────────────


class TestAutoReleaseInvokesPublish:
    def test_auto_release_calls_publish_as_a_reusable_workflow(self) -> None:
        jobs = _load(AUTO_RELEASE_YML)["jobs"]
        callers = [
            (name, job)
            for name, job in jobs.items()
            if isinstance(job.get("uses"), str) and "publish.yml" in job["uses"]
        ]
        assert len(callers) == 1, (
            "auto-release.yml must invoke publish.yml directly. Pushing a `v*` "
            "tag with the default GITHUB_TOKEN does NOT trigger a workflow "
            "(GitHub's recursion guard), which is #2035: v0.5.2 was tagged, "
            f"never published, and reported success. Found callers: {[n for n, _ in callers]}"
        )
        name, job = callers[0]
        assert (job.get("with") or {}).get("tag"), (
            f"job {name!r} must pass the tag it just pushed down to publish.yml"
        )
        assert job.get("secrets") == "inherit", (
            "a reusable workflow sees no secrets unless they are inherited or "
            "passed — without this the PyPI upload has no token"
        )

    def test_the_publish_job_may_create_the_release(self) -> None:
        """A caller cannot grant a reusable workflow more than it holds."""
        workflow = _load(AUTO_RELEASE_YML)
        job = next(j for j in workflow["jobs"].values() if "publish.yml" in str(j.get("uses", "")))
        perms = job.get("permissions") or workflow.get("permissions") or {}
        assert perms.get("contents") == "write", (
            "publish.yml's `release` job needs contents: write to create the "
            "GitHub Release; the calling job caps what it can have, so without "
            "this the release step 403s at the very end of a successful build"
        )

    def test_publish_declares_the_workflow_call_entrypoint(self) -> None:
        triggers = _triggers(_load(PUBLISH_YML))
        assert "workflow_call" in triggers, (
            "publish.yml must be callable, or auto-release.yml has nothing to "
            "invoke and is back to relying on the tag-push event"
        )
        inputs = triggers["workflow_call"]["inputs"]
        assert "tag" in inputs, f"publish.yml declares workflow_call inputs {sorted(inputs)}"
        # The manual path stays: one workflow, two entrypoints, one set of jobs.
        assert triggers["push"]["tags"] == ["v*"]

    def test_every_build_job_checks_out_the_tag_not_the_callers_ref(self) -> None:
        """Under `workflow_call`, `github.ref` is the CALLER's ref — `main`.

        A checkout that trusted the default would build whatever landed on main
        after the tag was cut and stamp it with the tag's version, which is
        exactly the drift #1238's tag-is-the-version design forbids.
        """
        workflow = _load(PUBLISH_YML)
        plan_outputs = workflow["jobs"]["plan"]["outputs"]
        assert "ref" in plan_outputs, "plan must resolve the ref every job checks out"

        for job in ("verify-tag", "build-wheel", "verify-published"):
            checkouts = [
                step
                for step in _steps(workflow, job)
                if str(step.get("uses", "")).startswith("actions/checkout")
            ]
            assert checkouts, f"job {job!r} has no checkout step"
            for step in checkouts:
                assert "plan.outputs.ref" in str((step.get("with") or {}).get("ref", "")), (
                    f"job {job!r} checks out the default ref — under workflow_call "
                    "that is the caller's branch, not the tag being released"
                )

        # #2898: there is no `build-tui` job to pass the ref down to any more.
        # coord-tui builds from its own repo's tag push, where the default
        # checkout ref IS the tag, so the whole class of caller-ref drift this
        # test guards cannot arise on that side.
        assert "build-tui" not in workflow["jobs"], (
            "publish.yml grew a coord-tui build back; #2898 moved that channel "
            "to coord-tui's own repo"
        )

    def test_verify_tag_reads_the_tagged_commit_not_github_sha(self) -> None:
        """`github.sha` under workflow_call is main's tip, which is always an
        ancestor of main — the #1471 guard would pass trivially."""
        text = _run_text(_load(PUBLISH_YML), "verify-tag")
        assert "git rev-parse HEAD" in text
        assert "github.sha" not in text, (
            "verify-tag still reads github.sha; under workflow_call that is the "
            "caller's commit, so the 'tag is on main' guard checks the wrong "
            "object and can never fail"
        )

    def test_the_github_release_is_named_after_the_tag_not_the_ref(self) -> None:
        workflow = _load(PUBLISH_YML)
        steps = [
            step
            for job in workflow["jobs"]
            for step in _steps(workflow, job)
            if str(step.get("uses", "")).startswith("softprops/action-gh-release")
        ]
        assert len(steps) == 1, "#1242: exactly one step may create the Release"
        tag_name = str(steps[0]["with"]["tag_name"])
        assert "plan.outputs.tag" in tag_name, (
            f"tag_name is {tag_name!r}; under workflow_call `github.ref_name` is "
            "the caller's branch, so this would cut a Release called 'main'"
        )

    def test_a_burst_of_merges_cannot_cancel_a_run_mid_publish(self) -> None:
        """`cancel-in-progress` was safe when the only side effect was a tag
        push. Now the publish runs inside this run, and cancelling between the
        PyPI upload and the Release is the half-released state #2035 is about.
        """
        concurrency = _load(AUTO_RELEASE_YML)["concurrency"]
        assert concurrency["cancel-in-progress"] is False, (
            "auto-release.yml cancels in-progress runs, but those runs now "
            "publish to PyPI and cut a GitHub Release — a cancelled one can "
            "leave the release half-done with nothing red to notice"
        )

    def test_coalescing_cannot_lose_an_unreleased_change(self) -> None:
        """Diff from the last release tag, not from the previous tip.

        Merge code, then merge docs while the code run is still going: the code
        run is superseded, and a run that diffs only its own push decides "docs
        only" and the code never ships. Basing the decision on everything since
        the last release makes coalescing able to delay a release but never
        lose one.
        """
        text = _run_text(_load(AUTO_RELEASE_YML), "decide-and-tag")
        assert "git tag --list" in text and "--sort=-v:refname" in text, (
            "the changed-path base is not derived from the last release tag; "
            "with github.event.before as the base, a coalesced run silently "
            "drops the unreleased changes of the runs it superseded"
        )
        assert "already_released_tag" in text, (
            "no handling for 'the tip is already tagged' — that diff is empty, "
            "which next_release_tag.py fails open on, minting a second version "
            "for a byte-identical tree"
        )

    def test_the_run_is_gated_on_the_release_existing(self) -> None:
        workflow = _load(PUBLISH_YML)
        job = workflow["jobs"]["verify-published"]
        # It can only verify what has finished.
        assert {"publish-pypi", "release"} <= set(job["needs"]), (
            f"verify-published waits on {job['needs']} — it must run after both "
            "the upload and the Release, or it proves nothing"
        )
        assert "dry_run" in str(job.get("if", "")), "a dry run publishes nothing to verify"
        text = _run_text(workflow, "verify-published")
        assert "verify_release_published.py" in text
        # The distinction the script's docstring (and #1628) is built on.
        assert "pypi/" not in text.lower() or "json" not in text.lower()

    def test_no_release_job_can_pass_by_doing_nothing(self) -> None:
        """The whole point: the terminal job of a real release is a check that
        the release exists, not merely that no step failed."""
        workflow = _load(PUBLISH_YML)
        jobs = workflow["jobs"]
        depended_on = {n for job in jobs.values() for n in (job.get("needs") or [])}
        terminal = set(jobs) - depended_on
        assert "verify-published" in terminal, (
            "verify-published is not terminal, so something runs after the "
            "post-condition and could report success past it"
        )


# ──────────────────────────────────────────────────────────────────────────
# layer 2: the post-condition script
# ──────────────────────────────────────────────────────────────────────────

PACKAGE = "code-coordinator"
INDEX_URL = index_url_for(PACKAGE)


def _index_html(*versions: str) -> str:
    rows = "\n".join(
        f'<a href="/x/code_coordinator-{v}-py3-none-any.whl#sha256=deadbeef">'
        f"code_coordinator-{v}-py3-none-any.whl</a><br/>\n"
        f'<a href="/x/code_coordinator-{v}.tar.gz#sha256=deadbeef">'
        f"code_coordinator-{v}.tar.gz</a><br/>"
        for v in versions
    )
    return f"<!DOCTYPE html><html><body>{rows}</body></html>"


class _Clock:
    """A monotonic clock that only advances when someone sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestVersionParsing:
    @pytest.mark.parametrize(
        ("tag", "expected"),
        [("v0.5.2", "0.5.2"), ("V1.0.0", "1.0.0"), ("0.5.2", "0.5.2"), (" v2.3.4 ", "2.3.4")],
    )
    def test_version_from_tag(self, tag: str, expected: str) -> None:
        assert version_from_tag(tag) == expected

    def test_empty_tag_is_an_error_not_an_empty_match(self) -> None:
        with pytest.raises(VerificationError):
            version_from_tag("   ")

    def test_index_url_is_pep503_normalised(self) -> None:
        assert index_url_for("Code_Coordinator") == "https://pypi.org/simple/code-coordinator/"

    def test_index_has_version(self) -> None:
        html = _index_html("0.5.0", "0.5.1")
        assert index_has_version(html, PACKAGE, "0.5.1")
        assert not index_has_version(html, PACKAGE, "0.5.2")

    def test_a_version_of_another_project_does_not_count(self) -> None:
        html = '<a href="/x/other_thing-0.5.2-py3-none-any.whl">other_thing-0.5.2-py3-none-any.whl</a>'
        assert not index_has_version(html, PACKAGE, "0.5.2")

    def test_a_yanked_release_is_not_published(self) -> None:
        """pip will not resolve to a yanked file, so calling it released is a
        lie the fleet would then be measured against."""
        html = (
            '<a href="/x/code_coordinator-0.5.2-py3-none-any.whl" data-yanked="broken">'
            "code_coordinator-0.5.2-py3-none-any.whl</a>"
        )
        assert not index_has_version(html, PACKAGE, "0.5.2")

    def test_package_name_comes_from_pyproject(self) -> None:
        assert package_name_from_pyproject() == PACKAGE


class TestWaitForPypi:
    def test_present_immediately(self) -> None:
        clock = _Clock()
        outcome = wait_for_pypi(
            PACKAGE,
            "0.5.2",
            fetch=lambda url: _index_html("0.5.1", "0.5.2"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert outcome.ok
        assert outcome.attempts == 1
        assert clock.slept == []

    def test_appears_after_cdn_propagation(self) -> None:
        """A single immediate probe would flap: PyPI's CDN takes seconds to
        minutes to serve a fresh upload."""
        clock = _Clock()
        bodies = [_index_html("0.5.1"), _index_html("0.5.1"), _index_html("0.5.1", "0.5.2")]

        def fetch(url: str) -> str:
            assert url == INDEX_URL
            return bodies.pop(0)

        outcome = wait_for_pypi(
            PACKAGE,
            "0.5.2",
            timeout=100,
            interval=10,
            fetch=fetch,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert outcome.ok
        assert outcome.attempts == 3
        assert clock.slept == [10, 10]

    def test_the_v0_5_2_incident_fails_the_job(self) -> None:
        """The exact observed state: tag on the remote, 0.5.1 newest on PyPI."""
        clock = _Clock()
        outcome = wait_for_pypi(
            PACKAGE,
            "0.5.2",
            timeout=60,
            interval=15,
            fetch=lambda url: _index_html("0.5.0", "0.5.1"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert not outcome.ok
        # It must say what was actually there, not just "missing".
        assert "0.5.2" in outcome.detail
        assert "0.5.1" in outcome.detail
        assert outcome.waited <= 60

    def test_an_unknown_project_is_missing_not_a_crash(self) -> None:
        clock = _Clock()
        outcome = wait_for_pypi(
            PACKAGE,
            "0.5.2",
            timeout=0,
            fetch=lambda url: None,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert not outcome.ok
        assert "404" in outcome.detail

    def test_a_transient_fetch_error_is_retried_not_a_verdict(self) -> None:
        """A 503 from the CDN must not be reported as 'the release is missing';
        it must be retried, or the post-condition becomes its own flake."""
        clock = _Clock()
        calls: list[int] = []

        def fetch(url: str) -> str:
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connection reset")
            return _index_html("0.5.2")

        outcome = wait_for_pypi(
            PACKAGE,
            "0.5.2",
            timeout=100,
            interval=5,
            fetch=fetch,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert outcome.ok
        assert outcome.attempts == 2

    def test_timeout_zero_still_probes_once(self) -> None:
        clock = _Clock()
        outcome = wait_for_pypi(
            PACKAGE,
            "0.5.2",
            timeout=0,
            fetch=lambda url: _index_html("0.5.2"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert outcome.ok and outcome.attempts == 1


class TestWaitForGithubRelease:
    def test_release_exists(self) -> None:
        clock = _Clock()
        outcome = wait_for_github_release(
            "owner/repo",
            "v0.5.2",
            fetch=lambda url: json.dumps({"tag_name": "v0.5.2", "assets": [{}, {}]}),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert outcome.ok
        assert "2 asset" in outcome.detail

    def test_missing_release_is_reported_with_the_tag(self) -> None:
        clock = _Clock()
        outcome = wait_for_github_release(
            "owner/repo",
            "v0.5.2",
            timeout=0,
            fetch=lambda url: None,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert not outcome.ok
        assert "v0.5.2" in outcome.detail

    def test_a_draft_release_does_not_count(self) -> None:
        clock = _Clock()
        outcome = wait_for_github_release(
            "owner/repo",
            "v0.5.2",
            timeout=0,
            fetch=lambda url: json.dumps({"tag_name": "v0.5.2", "draft": True}),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert not outcome.ok
        assert "draft" in outcome.detail

    def test_it_asks_the_by_tag_endpoint(self) -> None:
        seen: list[str] = []
        clock = _Clock()
        wait_for_github_release(
            "owner/repo",
            "v0.5.2",
            timeout=0,
            fetch=lambda url: seen.append(url) or json.dumps({"assets": []}),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            log=lambda _m: None,
        )
        assert seen == ["https://api.github.com/repos/owner/repo/releases/tags/v0.5.2"]


class TestFailureMessage:
    def test_it_names_the_missing_artifact_and_the_consequence(self) -> None:
        message = format_failure("v0.5.2", ["PyPI simple index: 0.5.2 not there"])
        assert "v0.5.2" in message
        assert "PyPI simple index" in message
        # The operator must not read a red here as "retry the deploy".
        assert "#2035" in message


class TestMainExitStatus:
    def test_exit_1_when_nothing_was_published(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "--tag",
                "v0.5.2",
                "--package",
                PACKAGE,
                "--index-url",
                "http://127.0.0.1:1/simple",
                "--timeout",
                "0",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "::error::" in err
        # One line, or the Actions annotation UI swallows it.
        assert "\n" not in err.split("::error::", 1)[1].split("\n", 1)[0].strip("\n")

    def test_an_empty_tag_is_rejected_before_any_network_call(self) -> None:
        with pytest.raises(VerificationError):
            main(["--tag", "  "])
