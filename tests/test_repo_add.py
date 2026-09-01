"""IL-1 (#2747): ``coord repo create`` — create a repo through the forge seam
and seed it (CLAUDE.md, a ``pull_request``-triggered CI workflow, the
``.githooks/`` port) so it is actually workable, then chain into `coord repo
add`.

``coord repo add`` already requires the repo to exist and prints 8 residue
items a human must still do by hand; three of those are traps (no CI on
``pull_request`` blocks every merge forever, no CLAUDE.md makes the review
gate structurally empty, no ``.githooks/`` leaves every worktree graph-blind).
These tests are the ones that matter most: that `coord repo create` never
touches GitHub before its local pre-flight checks pass, that a create failing
partway through is reported honestly rather than silently, and that the
residue it prints is genuinely the shrunk 4-item list — not the full 8, and
not so shrunk that it drops the per-machine coord-settings `git pull` that
clears `coord repo doctor`'s `agent_repo_skew` CRIT.

#2861 adds a second story to this file: ``--for-submission``, which collapses
portal repo genesis (create + seed + map the submission's project + commit +
push + distribute to every machine + ``coord repo doctor --fix``) into one
command, and the **stale-checkout guard** that refuses to write a
``coordinator.yml`` entry onto a coord-settings checkout that is behind its
upstream. Those tests drive the real thing end to end against a real (local,
throwaway) git origin+clone pair — the guard, the commit and the push are
git behaviour, and stubbing git would test nothing.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import github_ops
from coord.commands import repo as repo_cmd
from coord.commands.repo import repo_add, repo_create


CONFIG = """\
repos:
  - name: api
    github: acme/api
    depends_on: []
    default_branch: main
    test_command: "make test"

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
  - name: dellserver
    host: dellserver.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG)
    return p


def _stub_create(monkeypatch, *, exists=False, default_branch="main"):
    """Stub the forge-seam functions `coord repo create` calls, recording
    every call so tests can assert on ordering/arguments without touching a
    real GitHub repo."""
    calls: dict[str, list] = {
        "repo_exists": [], "create_repo": [], "seed": [], "labels": [],
    }

    def _repo_exists(repo):
        calls["repo_exists"].append(repo)
        return exists

    def _create_repo(repo, *, private=False, description=None):
        calls["create_repo"].append(
            {"repo": repo, "private": private, "description": description}
        )
        return {
            "name": repo.split("/")[-1], "full_name": repo,
            "url": f"https://github.com/{repo}", "default_branch": default_branch,
        }

    def _create_commit_with_files(repo, branch, files, message):
        calls["seed"].append(
            {"repo": repo, "branch": branch, "files": list(files), "message": message}
        )
        return "deadbeef"

    def _create_label(repo, label, **kw):
        calls["labels"].append((repo, label))

    monkeypatch.setattr(github_ops, "repo_exists", _repo_exists)
    monkeypatch.setattr(github_ops, "create_repo", _create_repo)
    monkeypatch.setattr(github_ops, "create_commit_with_files", _create_commit_with_files)
    monkeypatch.setattr(github_ops, "create_label", _create_label)
    # `coord repo create` chains into `_do_repo_add_core`, which (like `coord
    # repo add`) re-reads the default branch from GitHub rather than trusting
    # `create_repo`'s own return value — stub it to agree, so these tests
    # exercise the chained write path without a live `gh` call (#1484).
    monkeypatch.setattr(
        github_ops, "get_repo_default_branch", lambda repo: default_branch
    )
    return calls


class TestRepoCreateCommand:
    def test_happy_path_creates_seeds_and_chains_into_add(self, config_path, monkeypatch):
        calls = _stub_create(monkeypatch)

        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery",
                "--machines", "laptop,dellserver",
                "--config", str(config_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # The remote was created before anything else, public by default.
        assert calls["create_repo"] == [
            {"repo": "acme/grocery", "private": False, "description": None}
        ]

        # Seeded in one commit, on the repo's real default branch.
        assert len(calls["seed"]) == 1
        seed = calls["seed"][0]
        assert seed["repo"] == "acme/grocery"
        assert seed["branch"] == "main"
        seeded_paths = {path for path, _content, _exe in seed["files"]}
        assert seeded_paths == {
            "CLAUDE.md",
            ".github/workflows/ci.yml",
            ".githooks/_lib.sh",
            ".githooks/post-checkout",
            ".githooks/post-commit",
            ".githooks/post-merge",
            "graphify-out/.gitignore",
        }
        # The hook shims must be executable — a git hook that isn't simply
        # never runs — everything else must not be.
        executable = {path: exe for path, _content, exe in seed["files"]}
        assert executable[".githooks/post-checkout"] is True
        assert executable[".githooks/post-commit"] is True
        assert executable[".githooks/post-merge"] is True
        assert executable[".githooks/_lib.sh"] is False
        assert executable["CLAUDE.md"] is False
        assert executable[".github/workflows/ci.yml"] is False
        assert executable["graphify-out/.gitignore"] is False

        # #3037: the seeded `graphify-out/.gitignore` must actually guard the
        # directory (the self-ignoring `*` / `!.gitignore` form) — a repo
        # created by `coord repo create` is born with the seeded
        # post-checkout hook's own documented invariant made true.
        gitignore_content = dict(
            (p, c) for p, c, _e in seed["files"]
        )["graphify-out/.gitignore"]
        assert "*" in gitignore_content.splitlines()
        assert "!.gitignore" in gitignore_content.splitlines()

        # The default `generic` CI template actually triggers on pull_request
        # — the whole point, since `expects_checks()` blocks every merge
        # forever without it.
        ci_content = dict((p, c) for p, c, _e in seed["files"])[".github/workflows/ci.yml"]
        assert "pull_request" in ci_content

        # Chained into the same write path `coord repo add` uses.
        from coord.config import load

        cfg = load(config_path)
        repo = cfg.repo("grocery")
        assert repo is not None
        assert repo.default_branch == "main"
        for machine in ("laptop", "dellserver"):
            m = next(x for x in cfg.machines if x.name == machine)
            assert "grocery" in m.repos
        assert [lbl for _repo, lbl in calls["labels"]] == [
            "coord", "tier:small", "tier:large",
        ]

        # The residue is shrunk to 4 items, not repo add's 8 — CLAUDE.md/CI/
        # .githooks must read as ALREADY done, not as outstanding residue.
        assert "4 things still need a human" in result.output
        assert "already seeded" in result.output
        assert "add a CLAUDE.md to the repo" not in result.output
        assert "GRAPH, versioned half" not in result.output
        assert "coord repo doctor grocery --fix" in result.output
        # The per-machine coord-settings `git pull` must survive — dropping
        # it leaves every non-daemon machine's agent serving a stale
        # coordinator.yml with nothing telling the operator to expect the
        # resulting `agent_repo_skew` CRIT (review finding on #2747).
        assert "agent_repo_skew" in result.output
        for machine in ("laptop", "dellserver"):
            assert f"coord-settings checkout on {machine}" in result.output

    def test_private_flag_and_description_are_forwarded(self, config_path, monkeypatch):
        calls = _stub_create(monkeypatch)
        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery", "--private",
                "--description", "the grocery list app",
                "--config", str(config_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert calls["create_repo"] == [
            {
                "repo": "acme/grocery", "private": True,
                "description": "the grocery list app",
            }
        ]

    def test_template_selects_the_seeded_ci_workflow(self, config_path, monkeypatch):
        calls = _stub_create(monkeypatch)
        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery", "--template", "python",
                "--config", str(config_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        ci_content = dict(
            (p, c) for p, c, _e in calls["seed"][0]["files"]
        )[".github/workflows/ci.yml"]
        assert "pull_request" in ci_content
        assert "setup-python" in ci_content

    def test_python_template_writes_a_cli_pytest_acceptance_driver(
        self, config_path, monkeypatch,
    ):
        """#2748 (IL-2): the repo is oracle-loop-ready on day one — no
        hand-editing coordinator.yml required."""
        _stub_create(monkeypatch)
        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery", "--template", "python",
                "--config", str(config_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "wrote acceptance.drivers.grocery (cli-pytest)" in result.output

        from coord.config import load

        cfg = load(config_path)
        driver = cfg.acceptance.driver_for("grocery")
        assert driver is not None
        assert driver.kind == "cli-pytest"
        assert driver.run == "pytest tests/acceptance/{ms}"
        assert driver.capability == "python"

        # Residue reflects that the driver is already handled, not pending.
        assert "acceptance.drivers` is already written" in result.output

    def test_node_template_writes_a_web_playwright_acceptance_driver(
        self, config_path, monkeypatch,
    ):
        _stub_create(monkeypatch)
        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery", "--template", "node",
                "--config", str(config_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "wrote acceptance.drivers.grocery (web-playwright)" in result.output

        from coord.config import load

        cfg = load(config_path)
        driver = cfg.acceptance.driver_for("grocery")
        assert driver is not None
        assert driver.kind == "web-playwright"
        assert driver.setup == "npm ci"
        assert driver.capability == "browser"

    def test_generic_template_writes_no_acceptance_driver(
        self, config_path, monkeypatch,
    ):
        """`generic` means the stack isn't decided yet — a driver would be
        as much of a guess as the CI template deliberately isn't."""
        _stub_create(monkeypatch)
        result = CliRunner().invoke(
            repo_create,
            ["grocery", "--github", "acme/grocery", "--config", str(config_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "wrote acceptance.drivers" not in result.output

        from coord.config import load

        cfg = load(config_path)
        assert cfg.acceptance.driver_for("grocery") is None
        # Residue still names the manual step, since it wasn't automated.
        assert "if it joins the oracle loop" in result.output
        assert "acceptance.drivers`" in result.output

    def test_dry_run_touches_neither_github_nor_config(self, config_path, monkeypatch):
        calls = _stub_create(monkeypatch)
        before = config_path.read_text()
        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery", "--dry-run",
                "--config", str(config_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert config_path.read_text() == before
        assert calls["create_repo"] == []
        assert calls["seed"] == []
        assert calls["labels"] == []
        assert "would create acme/grocery" in result.output

    def test_refuses_when_github_repo_already_exists(self, config_path, monkeypatch):
        calls = _stub_create(monkeypatch, exists=True)
        before = config_path.read_text()
        result = CliRunner().invoke(
            repo_create, ["grocery", "--github", "acme/grocery", "--config", str(config_path)],
        )
        assert result.exit_code != 0
        assert "already exists on GitHub" in result.output
        assert "repo add" in result.output
        assert calls["create_repo"] == []
        assert config_path.read_text() == before

    def test_refuses_a_local_name_that_already_has_a_config_entry(
        self, config_path, monkeypatch
    ):
        """A config collision must be caught BEFORE anything touches GitHub —
        an orphaned, just-created-but-unconfigured repo is worse than a
        refusal (see the module docstring)."""
        calls = _stub_create(monkeypatch)
        result = CliRunner().invoke(
            repo_create, ["api", "--github", "acme/api", "--config", str(config_path)],
        )
        assert result.exit_code != 0
        assert "already has a repos[] entry" in result.output
        assert calls["repo_exists"] == []
        assert calls["create_repo"] == []

    def test_refuses_an_unknown_machine_before_touching_github(
        self, config_path, monkeypatch
    ):
        calls = _stub_create(monkeypatch)
        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery", "--machines", "ghost",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code != 0
        assert "unknown machine" in result.output
        assert calls["repo_exists"] == []
        assert calls["create_repo"] == []

    def test_seed_failure_reports_the_repo_as_existing_but_unseeded(
        self, config_path, monkeypatch
    ):
        calls = _stub_create(monkeypatch)

        def _boom(repo, branch, files, message):
            raise RuntimeError("gh: secondary rate limit")

        monkeypatch.setattr(github_ops, "create_commit_with_files", _boom)
        before = config_path.read_text()

        result = CliRunner().invoke(
            repo_create, ["grocery", "--github", "acme/grocery", "--config", str(config_path)],
        )
        assert result.exit_code != 0
        assert "was created but seeding failed" in result.output
        assert "EXISTS on GitHub" in result.output
        # The remote WAS created (can't be undone) but coordinator.yml must
        # not be touched — a half-seeded repo should not silently look
        # onboarded.
        assert calls["create_repo"] != []
        assert config_path.read_text() == before
        from coord.config import load

        assert load(config_path).repo("grocery") is None

    def test_create_repo_failure_is_a_clean_click_exception(
        self, config_path, monkeypatch
    ):
        """`gh repo create` failing (rate-limit, a name race, a forbidden
        owner, a network blip) must surface as the same kind of actionable
        `click.ClickException` every other failure path in this command
        produces — not a raw traceback (review finding on #2747)."""
        calls = _stub_create(monkeypatch)

        def _boom(repo, *, private=False, description=None):
            calls["create_repo"].append(
                {"repo": repo, "private": private, "description": description}
            )
            raise RuntimeError("gh: secondary rate limit")

        monkeypatch.setattr(github_ops, "create_repo", _boom)
        before = config_path.read_text()

        result = CliRunner().invoke(
            repo_create, ["grocery", "--github", "acme/grocery", "--config", str(config_path)],
        )
        assert result.exit_code != 0
        assert "could not create acme/grocery" in result.output
        assert "secondary rate limit" in result.output
        assert calls["seed"] == []
        assert config_path.read_text() == before
        from coord.config import load

        assert load(config_path).repo("grocery") is None


class TestGithubOpsRepoCreationSeam:
    """Direct unit tests of the ``coord.github_ops`` primitives ``coord repo
    create`` is built on (#2747) — the CLI tests above cover the command's
    orchestration by stubbing these; these cover what the primitives
    themselves actually send to ``gh``."""

    def test_repo_exists_true_on_a_clean_read(self) -> None:
        with patch("coord.github_ops._gh", return_value='{"name": "api"}'):
            assert github_ops.repo_exists("acme/api") is True

    def test_repo_exists_false_on_a_404(self) -> None:
        with patch(
            "coord.github_ops._gh",
            side_effect=RuntimeError("gh api repos/acme/ghost failed: HTTP 404: Not Found"),
        ):
            assert github_ops.repo_exists("acme/ghost") is False

    def test_repo_exists_reraises_a_transient_failure(self) -> None:
        """A rate limit or auth failure must not read as "doesn't exist" —
        that would let `create_repo` attempt a create against a repo that's
        merely unreadable right now (see docstring)."""
        with patch(
            "coord.github_ops._gh",
            side_effect=RuntimeError("gh api repos/acme/api failed: HTTP 403: rate limited"),
        ):
            with pytest.raises(RuntimeError, match="rate limited"):
                github_ops.repo_exists("acme/api")

    def test_create_repo_passes_public_private_and_description(self) -> None:
        with patch("coord.github_ops._gh") as mock_gh, patch(
            "coord.github_ops._gh_json",
            return_value={
                "name": "grocery", "full_name": "acme/grocery",
                "html_url": "https://github.com/acme/grocery",
                "default_branch": "main",
            },
        ):
            result = github_ops.create_repo(
                "acme/grocery", private=True, description="the grocery app",
            )
        args = mock_gh.call_args.args
        assert args[:3] == ("repo", "create", "acme/grocery")
        assert "--private" in args
        assert "--public" not in args
        assert "--add-readme" in args
        assert "--description" in args
        assert "the grocery app" in args
        assert result == {
            "name": "grocery", "full_name": "acme/grocery",
            "url": "https://github.com/acme/grocery", "default_branch": "main",
        }

    def test_create_repo_defaults_to_public(self) -> None:
        with patch("coord.github_ops._gh") as mock_gh, patch(
            "coord.github_ops._gh_json",
            return_value={"default_branch": "main"},
        ):
            github_ops.create_repo("acme/grocery")
        args = mock_gh.call_args.args
        assert "--public" in args
        assert "--private" not in args

    def test_create_repo_raises_when_the_readback_has_no_default_branch(self) -> None:
        with patch("coord.github_ops._gh"), patch(
            "coord.github_ops._gh_json", return_value={},
        ):
            with pytest.raises(RuntimeError, match="could not read it back"):
                github_ops.create_repo("acme/grocery")

    def test_create_commit_with_files_builds_one_commit_via_the_git_data_api(
        self,
    ) -> None:
        gh_json_returns = [
            {"object": {"sha": "parentsha"}},          # ref lookup
            {"tree": {"sha": "basetreesha"}},           # parent commit lookup
            {"sha": "blob1"},                            # blob for file 1
            {"sha": "blob2"},                            # blob for file 2
        ]
        input_json_returns = [
            {"sha": "newtreesha"},   # tree create
            {"sha": "newcommitsha"},  # commit create
        ]

        with patch(
            "coord.github_ops._gh_json", side_effect=gh_json_returns,
        ) as mock_gh_json, patch(
            "coord.github_ops._gh_input_json", side_effect=input_json_returns,
        ) as mock_input_json, patch("coord.github_ops._gh") as mock_gh:
            result = github_ops.create_commit_with_files(
                "acme/grocery", "main",
                [
                    ("CLAUDE.md", "# grocery\n", False),
                    (".githooks/post-checkout", "#!/bin/sh\n", True),
                ],
                "coord repo create: seed",
            )

        assert result == "newcommitsha"

        # The tree entries carry the right mode per file — 100755 for the
        # executable hook shim, 100644 for the plain file. This is the
        # entire reason this uses the Git Data API instead of the simpler
        # Contents API (`update_repo_file`), which can't express a mode.
        tree_body = json.loads(mock_input_json.call_args_list[0].kwargs["body"])
        modes = {entry["path"]: entry["mode"] for entry in tree_body["tree"]}
        assert modes["CLAUDE.md"] == "100644"
        assert modes[".githooks/post-checkout"] == "100755"
        assert tree_body["base_tree"] == "basetreesha"

        commit_body = json.loads(mock_input_json.call_args_list[1].kwargs["body"])
        assert commit_body["tree"] == "newtreesha"
        assert commit_body["parents"] == ["parentsha"]
        assert commit_body["message"] == "coord repo create: seed"

        # The ref is only updated as the LAST step, once the commit exists.
        ref_update = mock_gh.call_args
        assert ref_update.args[:3] == ("api", "-X", "PATCH")
        assert ref_update.args[3] == "repos/acme/grocery/git/refs/heads/main"
        assert "sha=newcommitsha" in ref_update.args

        assert mock_gh_json.call_count == 4  # ref + parent commit + 2 blobs

    def test_create_commit_with_files_raises_when_the_branch_has_no_head(self) -> None:
        with patch("coord.github_ops._gh_json", return_value={}):
            with pytest.raises(RuntimeError, match="could not resolve branch"):
                github_ops.create_commit_with_files(
                    "acme/grocery", "main", [("CLAUDE.md", "#\n", False)], "msg",
                )


# ── #2861: the stale-checkout guard + `--for-submission` ─────────────────────

_GIT_ENV = [
    "-c", "user.email=test@example.com",
    "-c", "user.name=coord test",
    "-c", "commit.gpgsign=false",
    "-c", "init.defaultBranch=main",
]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *_GIT_ENV, "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )


def _seed_git_identity(checkout):
    """Write the identity into the checkout's own ``.git/config``.

    ``_GIT_ENV`` only covers git invocations *this file* makes. The code under
    test (`_commit_and_push_settings`) shells out to `git commit` itself, with
    a plain inherited environment — so on any machine whose ``$HOME`` has no
    ``user.email`` (a synthesized fleet ``$HOME``, a CI container: the #2269
    class) that commit dies with "unable to auto-detect email address" and
    five tests fail for a reason that has nothing to do with what they assert.
    Repo-local config is what the rest of this suite uses, and it is what the
    production `git commit` actually reads. ``commit.gpgsign=false`` is here
    for the mirror-image case: an operator ``$HOME`` that signs every commit
    globally would otherwise block on a key this test has no business needing.
    """
    _git(checkout, "config", "user.email", "test@example.com")
    _git(checkout, "config", "user.name", "coord test")
    _git(checkout, "config", "commit.gpgsign", "false")


@pytest.fixture
def settings_checkout(tmp_path, monkeypatch):
    """A real coord-settings checkout with a real upstream.

    The guard, the commit and the push under test are *git* behaviour — a
    stubbed git would assert only that this file's stubs agree with
    themselves. A bare repo on local disk is a real remote as far as every
    command here is concerned.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", *_GIT_ENV, "init", "--bare", str(origin)],
        capture_output=True, text=True, check=True,
    )
    clone = tmp_path / "coord-settings"
    subprocess.run(
        ["git", *_GIT_ENV, "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    _seed_git_identity(clone)
    tracked = clone / "coord" / "coordinator.yml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text(CONFIG)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "initial config")
    _git(clone, "push", "-u", "origin", "HEAD:refs/heads/main")
    _git(clone, "branch", "--set-upstream-to=origin/main")

    monkeypatch.setenv("COORD_SETTINGS_DIR", str(clone))
    # The live config must NEVER be the operator's real ~/.coord one in a
    # test — the distribution step writes to it.
    live = tmp_path / "live-coordinator.yml"
    live.write_text(CONFIG)
    monkeypatch.setenv("COORD_CONFIG", str(live))
    return {"origin": origin, "clone": clone, "tracked": tracked, "live": live}


def _push_an_upstream_commit(settings):
    """Move origin ahead so the checkout under test is behind it."""
    other = settings["origin"].parent / "other-clone"
    subprocess.run(
        ["git", *_GIT_ENV, "clone", str(settings["origin"]), str(other)],
        capture_output=True, text=True, check=True,
    )
    (other / "NOTES.md").write_text("someone else pushed a config change\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "someone else's config change")
    _git(other, "push")


def _seed_submission(submission_id="SUB-1EA1D3", project_id="proj_67deaa6d1291"):
    from coord import portal_store

    portal_store.mirror_customer_facts(
        submission_id,
        {"project_id": project_id, "outcome": "a grocery list app"},
    )


@pytest.fixture
def stub_distribution(monkeypatch):
    """Record the per-machine distribution instead of SSHing anywhere, and
    keep `coord repo doctor --fix` (which fans out over HTTP) out of a unit
    test. Both seams are exercised for their *reporting*, which is what the
    acceptance criteria name."""
    calls = {"ssh": [], "doctor": []}
    states = {}

    class _Proc:
        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def _fake_ssh(host, script, *, timeout=180.0):
        calls["ssh"].append({"host": host, "script": script, "timeout": timeout})
        return _Proc(states.get(host, "HEAD=abc1234\nSTATE=symlink\n"))

    monkeypatch.setattr(repo_cmd, "_ssh_run", _fake_ssh)
    monkeypatch.setattr(
        repo_cmd, "_run_repo_doctor_fix", lambda name: calls["doctor"].append(name)
    )
    calls["states"] = states
    return calls


class TestStaleCheckoutGuard:
    """#2861 step 1 — the highest-value line in the issue. `coord repo add`
    and `coord repo create` used to write into whatever the coord-settings
    checkout happened to be, with no freshness check, and the resulting diff
    looked perfectly clean."""

    def test_repo_create_refuses_when_the_checkout_is_behind(
        self, settings_checkout, monkeypatch
    ):
        calls = _stub_create(monkeypatch)
        _push_an_upstream_commit(settings_checkout)
        before = settings_checkout["tracked"].read_text()

        result = CliRunner().invoke(
            repo_create, ["grocery", "--github", "acme/grocery"],
        )
        assert result.exit_code != 0
        assert "1 commit(s) behind" in result.output
        assert "pull --ff-only" in result.output
        # Nothing written, and — critically — nothing created on GitHub: the
        # guard runs before the forge seam is touched at all.
        assert settings_checkout["tracked"].read_text() == before
        assert calls["repo_exists"] == []
        assert calls["create_repo"] == []

    def test_repo_add_refuses_when_the_checkout_is_behind(
        self, settings_checkout, monkeypatch
    ):
        _stub_create(monkeypatch)
        _push_an_upstream_commit(settings_checkout)
        before = settings_checkout["tracked"].read_text()

        result = CliRunner().invoke(
            repo_add, ["grocery", "--github", "acme/grocery", "--no-labels"],
        )
        assert result.exit_code != 0
        assert "behind" in result.output
        assert settings_checkout["tracked"].read_text() == before

    def test_skip_freshness_check_overrides_the_guard(
        self, settings_checkout, monkeypatch
    ):
        _stub_create(monkeypatch)
        _push_an_upstream_commit(settings_checkout)

        result = CliRunner().invoke(
            repo_add,
            [
                "grocery", "--github", "acme/grocery", "--no-labels",
                "--skip-freshness-check",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "--skip-freshness-check" in result.output
        from coord.config import load

        assert load(settings_checkout["tracked"]).repo("grocery") is not None

    def test_a_current_checkout_writes_normally(self, settings_checkout, monkeypatch):
        _stub_create(monkeypatch)
        result = CliRunner().invoke(
            repo_add, ["grocery", "--github", "acme/grocery", "--no-labels"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "behind" not in result.output
        from coord.config import load

        assert load(settings_checkout["tracked"]).repo("grocery") is not None


class TestForSubmissionDryRun:
    def test_dry_run_prints_every_step_and_writes_nothing(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        calls = _stub_create(monkeypatch)
        _seed_submission()
        before = settings_checkout["tracked"].read_text()

        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery", "--private",
                "--machines", "laptop,dellserver",
                "--for-submission", "SUB-1EA1D3", "--dry-run",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        out = result.output
        for step in ("  1. ", "  2. ", "  3. ", "  4. ", "  5. ", "  6. "):
            assert step in out
        assert "behind its upstream" in out
        assert "portal.project_repos" in out
        assert "commit + push" in out
        assert "git pull --ff-only" in out
        assert "laptop, dellserver" in out
        assert "coord repo doctor grocery --fix" in out

        assert settings_checkout["tracked"].read_text() == before
        assert calls["create_repo"] == []
        assert calls["seed"] == []
        assert stub_distribution["ssh"] == []
        assert stub_distribution["doctor"] == []


class TestForSubmission:
    def test_maps_commits_pushes_and_distributes(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        _stub_create(monkeypatch)
        _seed_submission()

        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery",
                "--machines", "laptop,dellserver",
                "--for-submission", "SUB-1EA1D3",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        from coord.config import load

        cfg = load(settings_checkout["tracked"])
        assert cfg.repo("grocery") is not None
        assert cfg.portal.repos_for_project("proj_67deaa6d1291") == ["grocery"]

        # Committed AND pushed — an unpushed commit is exactly the state
        # #2861 exists to stop an operator ending up in unknowingly.
        head = _git(settings_checkout["clone"], "rev-parse", "HEAD").stdout.strip()
        origin_head = _git(
            settings_checkout["clone"], "rev-parse", "origin/main"
        ).stdout.strip()
        assert head == origin_head
        log = _git(settings_checkout["clone"], "log", "-1", "--pretty=%s").stdout
        assert "grocery" in log and "SUB-1EA1D3" in log
        assert _git(
            settings_checkout["clone"], "status", "--porcelain"
        ).stdout.strip() == ""

        # Distributed to every machine serving the repo, each reported.
        assert sorted(c["host"] for c in stub_distribution["ssh"]) == [
            "dellserver.tailnet", "laptop.tailnet",
        ]
        assert "laptop" in result.output
        assert "dellserver" in result.output
        assert stub_distribution["doctor"] == ["grocery"]

        # And the whole point: the submission now resolves to the new repo.
        from coord.approved_work import approved_submissions

        rows = {r["submission_id"]: r for r in approved_submissions(cfg)}
        assert rows["SUB-1EA1D3"]["repos"] == ["grocery"]

    def test_refuses_an_unknown_submission_before_touching_github(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        calls = _stub_create(monkeypatch)
        before = settings_checkout["tracked"].read_text()

        result = CliRunner().invoke(
            repo_create,
            ["grocery", "--github", "acme/grocery", "--for-submission", "SUB-NOPE"],
        )
        assert result.exit_code != 0
        assert "unknown submission 'SUB-NOPE'" in result.output
        assert calls["create_repo"] == []
        assert settings_checkout["tracked"].read_text() == before

    def test_refuses_a_submission_whose_mirror_has_no_project_id(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        """The #2585 mirror-clobber shape: the row exists but carries no
        project_id, so there is nothing to map. Naming the repair command
        matters — the failure is otherwise indistinguishable from a typo."""
        calls = _stub_create(monkeypatch)
        from coord import portal_store

        portal_store.mirror_customer_facts("SUB-BLANK", {"outcome": "something"})

        result = CliRunner().invoke(
            repo_create,
            ["grocery", "--github", "acme/grocery", "--for-submission", "SUB-BLANK"],
        )
        assert result.exit_code != 0
        assert "no project_id" in result.output
        assert "coord portal remirror SUB-BLANK" in result.output
        assert calls["create_repo"] == []

    def test_refuses_a_project_already_mapped_to_another_repo(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        calls = _stub_create(monkeypatch)
        _seed_submission()
        settings_checkout["tracked"].write_text(
            CONFIG
            + "\nportal:\n  project_repos:\n"
            '    - project_id: "proj_67deaa6d1291"\n'
            "      repos: [api]\n"
        )
        _git(settings_checkout["clone"], "commit", "-am", "map the project to api")
        _git(settings_checkout["clone"], "push")
        before = settings_checkout["tracked"].read_text()

        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery",
                "--for-submission", "SUB-1EA1D3",
            ],
        )
        assert result.exit_code != 0
        assert "already mapped to ['api']" in result.output
        assert "proj_67deaa6d1291" in result.output
        assert calls["create_repo"] == []
        assert settings_checkout["tracked"].read_text() == before

    def test_distribution_reports_per_machine_failure_without_aborting(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        """One unreachable host must not sink the sweep — a half-distributed
        fleet that says so is recoverable; one that doesn't is #2861's
        original nine-motion mess."""
        _stub_create(monkeypatch)
        _seed_submission()
        stub_distribution["states"]["laptop.tailnet"] = "STATE=pull-failed\n"

        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery",
                "--machines", "laptop,dellserver",
                "--for-submission", "SUB-1EA1D3",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "✗ laptop" in result.output
        assert "FAILED" in result.output
        assert "✓ dellserver" in result.output
        assert "distribution incomplete on: laptop" in result.output
        # The other machine was still reached, and the doctor still ran.
        assert len(stub_distribution["ssh"]) == 2
        assert stub_distribution["doctor"] == ["grocery"]

    def test_a_diverging_live_config_copy_is_backed_up_before_refresh(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        """The step-5 decision: this command owns the copy explicitly. The
        live file on #2861's own fleet held a comment that existed nowhere
        else — clobbering it with no backup would have destroyed it."""
        _stub_create(monkeypatch)
        _seed_submission()
        live = settings_checkout["live"]
        live.write_text(CONFIG + "\n# a live-only comment nobody committed\n")

        result = CliRunner().invoke(
            repo_create,
            ["grocery", "--github", "acme/grocery", "--for-submission", "SUB-1EA1D3"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "live-only comment" not in live.read_text()
        assert "grocery" in live.read_text()
        backups = list(live.parent.glob(f"{live.name}.bak-*"))
        assert len(backups) == 1
        assert "a live-only comment nobody committed" in backups[0].read_text()
        assert "backed up" in result.output

    def test_no_refresh_live_config_reports_the_divergence_instead(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        _stub_create(monkeypatch)
        _seed_submission()
        live = settings_checkout["live"]
        live.write_text(CONFIG + "\n# a live-only comment nobody committed\n")

        result = CliRunner().invoke(
            repo_create,
            [
                "grocery", "--github", "acme/grocery",
                "--for-submission", "SUB-1EA1D3", "--no-refresh-live-config",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "a live-only comment nobody committed" in live.read_text()
        assert "grocery" not in live.read_text()
        assert "DIFFERS" in result.output

    def test_rerunning_the_mapping_is_idempotent(
        self, settings_checkout, monkeypatch, stub_distribution
    ):
        """`repo add --for-submission` on an already-mapped project (the repo
        entry landing separately, say) must not append a duplicate
        project_id — `_parse_portal_project_repos` rejects duplicates at
        LOAD, so that would take the whole fleet's config down."""
        _stub_create(monkeypatch)
        _seed_submission()

        first = CliRunner().invoke(
            repo_add,
            [
                "grocery", "--github", "acme/grocery", "--no-labels",
                "--for-submission", "SUB-1EA1D3",
            ],
            catch_exceptions=False,
        )
        assert first.exit_code == 0, first.output

        second = CliRunner().invoke(
            repo_add,
            [
                "grocery-two", "--github", "acme/grocery-two", "--no-labels",
                "--for-submission", "SUB-1EA1D3",
            ],
        )
        # A second repo for the same project is a REMAP, and is refused.
        assert second.exit_code != 0
        assert "already mapped to ['grocery']" in second.output

        from coord.config import load

        cfg = load(settings_checkout["tracked"])
        assert [e.project_id for e in cfg.portal.project_repos] == ["proj_67deaa6d1291"]


class TestPortalProjectRepoYamlEdit:
    """`insert_portal_project_repo_entry` — the YAML surgery, in isolation.
    Every shape the fleet's own config has actually been in."""

    def _mapping(self, text):
        from coord.config import load

        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write(text)
            path = Path(fh.name)
        try:
            return load(path).portal
        finally:
            path.unlink(missing_ok=True)

    def test_creates_the_portal_block_when_there_is_none(self):
        from coord.repo_edit import (
            insert_portal_project_repo_entry,
            render_portal_project_repo_entry,
        )

        updated = insert_portal_project_repo_entry(
            CONFIG, render_portal_project_repo_entry("proj_x", ["api"])
        )
        portal = self._mapping(updated)
        assert portal.repos_for_project("proj_x") == ["api"]
        # A created block must not switch the portal client ON.
        assert portal.enabled is False

    def test_appends_to_an_existing_project_repos_list(self):
        from coord.repo_edit import (
            insert_portal_project_repo_entry,
            render_portal_project_repo_entry,
        )

        base = (
            CONFIG
            + '\nportal:\n  enabled: false\n  project_repos:\n'
            '    - project_id: "proj_a"\n      repos: [api]\n'
        )
        updated = insert_portal_project_repo_entry(
            base, render_portal_project_repo_entry("proj_b", ["api"])
        )
        portal = self._mapping(updated)
        assert portal.repos_for_project("proj_a") == ["api"]
        assert portal.repos_for_project("proj_b") == ["api"]

    def test_rewrites_an_inline_empty_list(self):
        from coord.repo_edit import (
            insert_portal_project_repo_entry,
            render_portal_project_repo_entry,
        )

        base = CONFIG + "\nportal:\n  enabled: false\n  project_repos: []\n"
        updated = insert_portal_project_repo_entry(
            base, render_portal_project_repo_entry("proj_a", ["api"])
        )
        assert updated.count("project_repos:") == 1
        assert self._mapping(updated).repos_for_project("proj_a") == ["api"]

    def test_adds_project_repos_to_a_portal_block_that_lacks_it(self):
        from coord.repo_edit import (
            insert_portal_project_repo_entry,
            render_portal_project_repo_entry,
        )

        base = CONFIG + '\nportal:\n  enabled: false\n  timeout_secs: 5.0\n'
        updated = insert_portal_project_repo_entry(
            base, render_portal_project_repo_entry("proj_a", ["api"])
        )
        portal = self._mapping(updated)
        assert portal.repos_for_project("proj_a") == ["api"]
        assert portal.timeout_secs == 5.0

    def test_quotes_a_project_id_yaml_would_otherwise_coerce(self):
        """An all-digit or `yes`-shaped opaque id must survive as a string —
        unquoted, YAML 1.1 hands `_parse_portal_project_repos` a bool/int and
        the whole fleet config stops loading."""
        from coord.repo_edit import (
            insert_portal_project_repo_entry,
            render_portal_project_repo_entry,
        )

        updated = insert_portal_project_repo_entry(
            CONFIG, render_portal_project_repo_entry("12345", ["api"])
        )
        assert self._mapping(updated).repos_for_project("12345") == ["api"]


class TestFreshnessGuardScope:
    def test_a_dev_coordinator_yml_in_an_unrelated_checkout_is_not_guarded(
        self, settings_checkout, tmp_path, monkeypatch
    ):
        """`--config ./coordinator.yml` inside some other source checkout must
        not refuse because THAT repo is behind its own origin — the guard is
        about the fleet's config, not whatever repo you happen to be in."""
        _stub_create(monkeypatch)
        # A behind-its-upstream checkout whose config is at the repo ROOT,
        # not at the coord-settings `coord/coordinator.yml` path.
        _push_an_upstream_commit(settings_checkout)
        dev_config = settings_checkout["clone"] / "coordinator.yml"
        dev_config.write_text(CONFIG)

        result = CliRunner().invoke(
            repo_add,
            [
                "grocery", "--github", "acme/grocery", "--no-labels",
                "--config", str(dev_config),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "behind" not in result.output
        from coord.config import load

        assert load(dev_config).repo("grocery") is not None
