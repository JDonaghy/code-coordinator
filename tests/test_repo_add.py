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
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import github_ops
from coord.commands.repo import repo_create


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
