"""#1242 (PKG-6) then #2898: what "one tag, one Release" means per channel.

Two layers, matching the two ways this can regress:

* ``TestSplitReleaseChannels`` parses the real workflow YAML — not a fixture —
  and pins the *structural* invariants, first #1242's and now #2898's.

  #1242's problem: `publish.yml` and `release-tui.yml` both lived in THIS
  repo, both fired on `v*`, and both called ``softprops/action-gh-release``
  for the same tag.  That action upserts by ``tag_name``, so whichever run's
  step landed first created the Release and the other appended to it — which
  meant whether the Release carried generated notes was decided by a race
  between two independent runs, and neither attached the wheel at all.  Its
  fix made release-tui.yml a reusable workflow publish.yml called.

  #2898 (phase 3 of #2894) removes the shared Release instead of sequencing
  access to it: coord-tui gets its own repo, its own ``v*`` tag namespace and
  its own Releases.  So the invariant generalises from "exactly one
  release-creating step in the system" to **exactly one per channel**, and a
  new one appears: tagging ``v*`` HERE must produce a wheel and NO coord-tui
  binaries, and `verify-assets` must not fail for their absence (#2898's
  acceptance criterion 1).

  coord-tui's workflow is staged at ``tui/.github/workflows/release-tui.yml``
  — inside ``tui/``, NOT the repo root's ``.github/workflows/`` — so GitHub
  never reads it here (it is inert, and cannot race publish.yml) and #2894's
  move story carries it across with the crate.  These tests read it from that
  staged path on purpose: an inert file is exactly the kind that rots.

  This is a grep-shaped test on purpose (cf. tests/test_ci_acceptance_gate_1950.py):
  you cannot run GitHub Actions in pytest, and the failure mode being guarded
  is someone re-fusing the two channels, which reads as perfectly sensible
  YAML in isolation.

* ``TestVerifyReleaseWheel`` drives ``scripts/verify_release_wheel.py``
  against synthetic dists.  That script is the CI-observable half of "the
  wheel is stamped with the tag": it fails the publish job when setuptools-scm
  resolved a ``.devN+g<sha>`` fallback instead of the tag (the shallow-clone
  failure mode #1238's ``fallback_version`` deliberately makes non-fatal for
  dev checkouts), when the ``[server]`` extra is missing from the built
  metadata (PKG-1/#1237 — every agent host installs it), or when the wheel
  was built without the React bundle.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PUBLISH_YML = WORKFLOW_DIR / "publish.yml"
# #2898: staged inside `tui/`, not the repo root's `.github/workflows/`.
# GitHub only reads workflows from the root, so this one is inert here and
# travels with the crate when #2894's move story runs.
RELEASE_TUI_YML = REPO_ROOT / "tui" / ".github" / "workflows" / "release-tui.yml"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_release_wheel.py"

sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_release_wheel import (  # noqa: E402  - needs the sys.path line above
    VerificationError,
    verify,
)


def _load(path: Path) -> dict:
    # YAML 1.1 (SafeLoader) parses a bare `on:` key as the boolean True, so
    # triggers live under `True`, not `"on"`. `_triggers` normalises that.
    return yaml.safe_load(path.read_text())


def _triggers(workflow: dict) -> dict:
    on = workflow.get("on", workflow.get(True))
    assert isinstance(on, dict), f"expected a mapping of triggers, got {on!r}"
    return on


def _all_steps(workflow: dict) -> list[tuple[str, dict]]:
    """Every ``(job_name, step)`` pair in *workflow*."""
    out: list[tuple[str, dict]] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            out.append((job_name, step))
    return out


def _steps_using(workflow: dict, action_prefix: str) -> list[tuple[str, dict]]:
    return [
        (job, step)
        for job, step in _all_steps(workflow)
        if isinstance(step.get("uses"), str) and step["uses"].startswith(action_prefix)
    ]


def _transitive_needs(workflow: dict, job_name: str) -> set[str]:
    """Every job *job_name* waits on, directly or through another job."""
    jobs = workflow["jobs"]
    seen: set[str] = set()
    stack = list(jobs[job_name].get("needs") or [])
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(jobs.get(name, {}).get("needs") or [])
    return seen


# ──────────────────────────────────────────────────────────────────────────
# structural: one tag, one release
# ──────────────────────────────────────────────────────────────────────────


class TestSplitReleaseChannels:
    def test_publish_owns_this_repos_v_tag_and_no_longer_calls_the_tui_workflow(self) -> None:
        """#2898 criterion 1, structural half: nothing in this repo's release
        run builds coord-tui any more."""
        publish = _load(PUBLISH_YML)
        assert _triggers(publish)["push"]["tags"] == ["v*"]

        callers = [
            name
            for name, job in publish["jobs"].items()
            if isinstance(job.get("uses"), str) and "release-tui.yml" in job["uses"]
        ]
        assert callers == [], (
            f"publish.yml still calls release-tui.yml as a reusable workflow "
            f"({callers}). Post-#2898 that workflow belongs to coord-tui's repo "
            "and fires on ITS tag push; a `uses:` here would either fail to "
            "resolve or resurrect the fused channel."
        )

    def test_the_tui_workflow_fires_on_its_own_tag_push(self) -> None:
        """#2898 undoes #1242's `workflow_call`. The race that arrangement
        existed to stop needed TWO workflows sharing ONE Release; two repos
        with two tag namespaces have nothing to race over — and a caller in
        another repo could not hand this one a tag from its own history
        anyway."""
        tui_triggers = _triggers(_load(RELEASE_TUI_YML))
        assert tui_triggers.get("push", {}).get("tags") == ["v*"], (
            "release-tui.yml must fire on its own `v*` push in coord-tui's "
            f"repo; triggers are {sorted(tui_triggers)}"
        )
        assert "workflow_call" not in tui_triggers, (
            "release-tui.yml is still a reusable workflow (#1242's "
            "arrangement). Nothing can call it any more — publish.yml lives "
            "in a different repo — so it would simply never run."
        )
        assert "workflow_dispatch" in tui_triggers, (
            "the standalone dry run must survive the split: it is the only way "
            "to exercise build+stamp+verify without cutting a real Release"
        )

    def test_the_tui_workflow_is_staged_outside_this_repos_workflow_dir(self) -> None:
        """It must NOT be readable by GitHub here — a `v*` tag in this repo
        would otherwise trigger it, rebuilding the very binaries #2898 removed
        from this channel and racing publish.yml for this repo's Release."""
        assert RELEASE_TUI_YML.exists(), f"{RELEASE_TUI_YML} is missing"
        assert not (WORKFLOW_DIR / "release-tui.yml").exists(), (
            "release-tui.yml is back in the repo root's .github/workflows/, "
            "where GitHub WILL run it on this repo's `v*` tags"
        )
        live = sorted(p.name for p in WORKFLOW_DIR.glob("*.yml"))
        assert "release-tui.yml" not in live, live

    def test_exactly_one_step_creates_a_github_release_per_channel(self) -> None:
        """#1242's invariant, generalised. Each channel needs exactly one
        release-creating step: two in one repo is the upsert race, zero means
        the channel publishes nothing at all — and for coord-tui that would
        leave `coord tui update`'s resolution source permanently empty."""
        for label, path in (("publish.yml", PUBLISH_YML), ("release-tui.yml", RELEASE_TUI_YML)):
            steps = _steps_using(_load(path), "softprops/action-gh-release")
            assert len(steps) == 1, (
                f"{label} has {len(steps)} release-creating step(s) "
                f"({[j for j, _ in steps]}); each channel needs exactly one"
            )
            _, step = steps[0]
            assert step["with"].get("generate_release_notes") is True, label

    def test_this_repos_release_carries_the_wheel_and_no_binaries(self) -> None:
        """#2898 criterion 1: a wheel, no coord-tui binaries, and no failure
        for their absence."""
        workflow = _load(PUBLISH_YML)
        release_job_name = next(
            name
            for name, job in workflow["jobs"].items()
            if _steps_using({"jobs": {name: job}}, "softprops/action-gh-release")
        )
        waits_on = _transitive_needs(workflow, release_job_name)
        assert {"build-wheel", "verify-assets"} <= waits_on, sorted(waits_on)
        assert "build-tui" not in waits_on, (
            "the release job still waits on a coord-tui build that this "
            "workflow no longer runs — it would block forever"
        )

        check = "\n".join(
            step.get("run", "")
            for step in workflow["jobs"]["verify-assets"]["steps"]
            if step.get("run")
        )
        assert "*.whl" in check, "verify-assets never collects the wheel as an asset"
        assert "coord-tui" not in check, (
            "verify-assets still requires `coord-tui-*` assets. Nothing in "
            "this run builds one, so EVERY release from this repo would fail "
            "as incomplete — the exact regression #2898's criterion 1 names."
        )

    def test_the_tui_channel_checks_its_own_asset_completeness(self) -> None:
        """The completeness bar publish.yml used to enforce for the fused
        channel does not evaporate — it moves with the binaries. Windows stays
        deliberately absent (`best_effort: true` in the matrix), so a
        Windows-only hiccup cannot withhold an otherwise-complete release."""
        jobs = _load(RELEASE_TUI_YML)["jobs"]
        release_job = next(
            job for job in jobs.values()
            if _steps_using({"jobs": {"j": job}}, "softprops/action-gh-release")
        )
        check = "\n".join(s.get("run", "") for s in release_job["steps"] if s.get("run"))
        for target in ("x86_64-linux", "x86_64-macos", "aarch64-macos"):
            assert target in check, (
                f"release-tui.yml does not assert coord-tui-{target} is present; "
                "a silently binary-less release is what PKG-3's acceptance bar "
                "forbids, and it is now the only place that check can live"
            )
        assert "x86_64-windows" not in check, (
            "Windows is best_effort — requiring it would let one flaky leg "
            "withhold the whole release"
        )

    def test_the_tui_channel_reinstates_the_tag_is_on_main_guard(self) -> None:
        """#1471's guard moved to publish.yml's `verify-tag` in #1242 because
        publish.yml owned the trigger. Now that the tag push reaches
        release-tui.yml directly in a repo where publish.yml does not exist,
        the guard has to come back with it — a Release cut from a tag whose
        commit branch protection rejected advertises code nobody can
        reproduce."""
        jobs = _load(RELEASE_TUI_YML)["jobs"]
        runs = "\n".join(
            step.get("run", "")
            for job in jobs.values()
            for step in (job.get("steps") or [])
            if step.get("run")
        )
        assert "merge-base --is-ancestor" in runs, (
            "release-tui.yml publishes on its own tag push with no "
            "tag-is-on-main guard anywhere in it"
        )

    def test_dry_run_builds_and_checks_everything_but_publishes_nothing(self) -> None:
        """The acceptance criterion's "(or dry-run)": a maintainer must be able
        to prove the asset set builds and agrees on a version *without* an
        irreversible PyPI upload — this workflow's own changes cannot
        otherwise be tested before they run for real."""
        workflow = _load(PUBLISH_YML)
        jobs = workflow["jobs"]

        assert "dry_run_tag" in _triggers(workflow)["workflow_dispatch"]["inputs"]

        for name in ("verify-tag", "publish-pypi", "release"):
            condition = jobs[name].get("if", "")
            assert "dry_run" in str(condition), (
                f"job {name!r} has no dry-run guard (if: {condition!r}) — a dry "
                "run would publish to PyPI / cut a real Release"
            )

        # ...while the jobs that prove the release is complete carry no such
        # guard, so a dry run exercises them in full.
        for name in ("build-wheel", "verify-assets"):
            assert "dry_run" not in str(jobs[name].get("if", "")), (
                f"job {name!r} is skipped on a dry run, which defeats the point "
                "of having one"
            )

    def test_only_the_wheel_build_and_the_dry_run_touch_versions(self) -> None:
        """A dry run is dispatched against a branch, so setuptools-scm has no
        release tag to find and would silently build `X.Y.Z.devN+g<sha>`."""
        steps = _load(PUBLISH_YML)["jobs"]["build-wheel"]["steps"]
        tagging = [s for s in steps if "git tag" in s.get("run", "")]
        assert len(tagging) == 1, "expected exactly one throwaway-tag step"
        assert "dry_run" in str(tagging[0].get("if", "")), (
            "the throwaway local tag must be dry-run-only — creating one during "
            "a real release would mask the tag being published"
        )

    def test_wheel_build_checks_out_tags_for_setuptools_scm(self) -> None:
        jobs = _load(PUBLISH_YML)["jobs"]
        wheel_job = jobs["build-wheel"]
        checkout = next(
            step
            for step in wheel_job["steps"]
            if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/checkout")
        )
        # setuptools-scm falls back to `X.Y.Z.devN+g<sha>` rather than failing
        # when no tag is reachable (#1238) — a shallow clone would therefore
        # publish a dev version under a `vX.Y.Z` release, immutably.
        assert checkout["with"]["fetch-depth"] == 0
        assert checkout["with"].get("fetch-tags") is True

    def test_wheel_build_runs_the_artifact_verifier(self) -> None:
        steps = _load(PUBLISH_YML)["jobs"]["build-wheel"]["steps"]
        runs = "\n".join(step.get("run", "") for step in steps if step.get("run"))
        assert "scripts/verify_release_wheel.py" in runs, (
            "publish.yml no longer verifies the built wheel against the tag — "
            "that check is the only thing standing between a shallow clone and "
            "an immutable PyPI upload of the wrong version"
        )
        # #2009: the wheel no longer carries a webapp bundle — the source
        # moved to the `coord-web` repo, so there is nothing here to `npm run
        # build`. `--no-webapp` is what keeps the REST of the verifier
        # (version stamp, `[server]` extra) blocking the release: dropping
        # the flag would fail every release on the now-permanently-absent
        # bundle, and dropping the step would stop checking anything.
        assert "npm run build" not in runs, (
            "publish.yml is trying to build a React bundle from source this "
            "repo no longer has (#2009) — the webapp lives in coord-web"
        )
        assert "--no-webapp" in runs, (
            "publish.yml must pass --no-webapp now that the wheel carries no "
            "bundle, or every release fails on a deliberately-absent artifact"
        )

    def test_publish_workflow_installs_no_node_toolchain(self) -> None:
        """#2009: nothing in the release path builds JavaScript any more.

        A leftover `actions/setup-node` would be the harmless-looking half of
        a re-added webapp build step, and the expensive half (`npm ci`
        against a missing directory) fails loudly enough to not need a test.
        """
        steps = _load(PUBLISH_YML)["jobs"]["build-wheel"]["steps"]
        uses = [step.get("uses", "") for step in steps]
        assert not [u for u in uses if u.startswith("actions/setup-node")], (
            f"build-wheel still sets up Node with nothing to build: {uses}"
        )

    def test_tui_workflow_uploads_the_asset_names_coord_tui_update_expects(self) -> None:
        from coord.tui_release import asset_filename

        steps = _load(RELEASE_TUI_YML)["jobs"]["build"]["steps"]
        upload = next(
            step
            for step in steps
            if isinstance(step.get("uses"), str)
            and step["uses"].startswith("actions/upload-artifact")
        )
        path = upload["with"]["path"]
        # `${{ matrix.target_name }}`/`${{ matrix.bin_ext }}` substituted by hand.
        for target in ("x86_64-linux", "aarch64-macos", "x86_64-windows"):
            ext = ".exe" if target.endswith("-windows") else ""
            rendered = (
                path.replace("${{ matrix.target_name }}", target).replace(
                    "${{ matrix.bin_ext }}", ext
                )
            )
            assert rendered.endswith(asset_filename(target)), (
                f"release-tui.yml uploads {rendered!r} but coord/tui_release.py's "
                f"`coord tui update` looks for {asset_filename(target)!r}"
            )


# ──────────────────────────────────────────────────────────────────────────
# scripts/verify_release_wheel.py
# ──────────────────────────────────────────────────────────────────────────


def _make_wheel(
    dist: Path,
    version: str,
    *,
    extra: str | None = "server",
    webapp: bool = True,
    name: str = "code_coordinator",
) -> Path:
    path = dist / f"{name}-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.1\nName: code-coordinator\nVersion: {version}\n"
    if extra:
        metadata += f"Provides-Extra: {extra}\n"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
        zf.writestr("coord/__init__.py", "")
        if webapp:
            zf.writestr("coord/dashboard/webapp/dist/index.html", "<html></html>")
    return path


def _make_sdist(dist: Path, version: str, name: str = "code_coordinator") -> Path:
    path = dist / f"{name}-{version}.tar.gz"
    path.write_bytes(b"")
    return path


@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    return d


class TestVerifyReleaseWheel:
    def test_happy_path_returns_the_tag_version(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106")
        _make_sdist(dist, "0.4.106")
        assert str(verify(dist, "v0.4.106")) == "0.4.106"

    def test_pep440_normalisation_is_not_a_mismatch(self, dist: Path) -> None:
        # setuptools-scm writes `1.0.0rc1` into the filename for a tag spelled
        # `v1.0.0-rc1`; those are the same version, not a drift.
        _make_wheel(dist, "1.0.0rc1")
        _make_sdist(dist, "1.0.0rc1")
        assert str(verify(dist, "v1.0.0-rc1")) == "1.0.0rc1"

    def test_setuptools_scm_dev_fallback_is_rejected(self, dist: Path) -> None:
        """The shallow-clone / missing-tag failure this exists for: the build
        succeeds, the wheel is valid, and the version is silently wrong."""
        _make_wheel(dist, "0.4.106.dev3+gdeadbee")
        _make_sdist(dist, "0.4.106.dev3+gdeadbee")
        with pytest.raises(VerificationError, match="setuptools-scm did not resolve the"):
            verify(dist, "v0.4.106")

    def test_missing_server_extra_is_rejected(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106", extra=None)
        _make_sdist(dist, "0.4.106")
        with pytest.raises(VerificationError, match="Provides-Extra: server"):
            verify(dist, "v0.4.106")

    def test_missing_webapp_bundle_is_rejected(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106", webapp=False)
        _make_sdist(dist, "0.4.106")
        with pytest.raises(VerificationError, match="React bundle was not built"):
            verify(dist, "v0.4.106")

    def test_missing_webapp_bundle_can_be_waived_for_local_builds(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106", webapp=False)
        _make_sdist(dist, "0.4.106")
        assert str(verify(dist, "v0.4.106", require_webapp=False)) == "0.4.106"

    def test_missing_sdist_is_rejected(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106")
        with pytest.raises(VerificationError, match="exactly 1 sdist"):
            verify(dist, "v0.4.106")

    def test_stale_wheel_left_in_dist_is_rejected(self, dist: Path) -> None:
        """A rebuilt-without-cleaning `dist/` would otherwise upload two
        versions of the same package under one tag."""
        _make_wheel(dist, "0.4.105")
        _make_wheel(dist, "0.4.106")
        _make_sdist(dist, "0.4.106")
        with pytest.raises(VerificationError, match="exactly 1 wheel"):
            verify(dist, "v0.4.106")

    def test_all_problems_are_reported_together(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.105", extra=None, webapp=False)
        _make_sdist(dist, "0.4.105")
        with pytest.raises(VerificationError) as exc:
            verify(dist, "v0.4.106")
        message = str(exc.value)
        assert "setuptools-scm did not resolve" in message
        assert "Provides-Extra: server" in message
        assert "React bundle was not built" in message

    def test_cli_exits_nonzero_and_annotates_on_failure(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.105")
        _make_sdist(dist, "0.4.105")
        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--tag", "v0.4.106", "--dist", str(dist)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1
        assert "::error::" in proc.stderr

    def test_cli_exits_zero_on_a_good_dist(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106")
        _make_sdist(dist, "0.4.106")
        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--tag", "v0.4.106", "--dist", str(dist)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "0.4.106" in proc.stdout
