"""#2220: a half-onboarded repo must produce one distinct, NAMED finding per
defect — not a generic failure.

The headline test here is :class:`TestSeededHalfOnboardedFleet`, which is
#2220's third acceptance bullet verbatim: a seeded fleet with a repo that is
missing its clone on one machine, stale in another agent's repo list, has no
``pull_request``-triggered workflow and no ``coord`` label produces four
separate findings with four separate check ids and four separate remedies. The
whole point of the feature is that these four defects have *nothing* in common
except that they are all invisible — a single "repo is not ready" line would
be no better than the silence it replaces.

Every test is hermetic: :func:`coord.repo_onboard.evaluate` is pure, and the
GitHub layer is driven through the ``ops`` seam rather than ``gh``.
"""

from __future__ import annotations

import pytest

from coord import repo_onboard as ro
from coord.config import load as load_config
from coord.network import ONLINE, OFFLINE, MachineStatus


SEEDED_CONFIG = """\
repos:
  - name: api
    github: acme/api
    depends_on: []
    default_branch: main
    build_command: "make build"
    test_command: "make test"
  - name: newrepo
    github: acme/newrepo
    depends_on: []
    default_branch: main
    build_command: "make build"
    test_command: "make test"

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api, newrepo]
    repo_paths:
      api: /srv/api
      newrepo: /srv/newrepo
  - name: dellserver
    host: dellserver.tailnet
    capabilities: [python]
    repos: [api, newrepo]
    repo_paths:
      api: /srv/api
      newrepo: /srv/newrepo
"""


@pytest.fixture
def seeded_config(tmp_path):
    p = tmp_path / "coordinator.yml"
    p.write_text(SEEDED_CONFIG)
    return load_config(p)


class _FakeOps:
    """Stand-in for :mod:`coord.github_ops` — the whole GitHub layer behind one
    injectable object, so no test here shells out to ``gh``."""

    def __init__(
        self,
        *,
        default_branch="main",
        labels=None,
        workflows=None,
        files=None,
        raise_on=(),
    ):
        self._default_branch = default_branch
        self._labels = labels if labels is not None else ["coord", "tier:small", "tier:large"]
        self._workflows = workflows if workflows is not None else []
        self._files = files or {}
        self._raise_on = set(raise_on)

    def _maybe_raise(self, what):
        if what in self._raise_on:
            raise RuntimeError(f"simulated {what} failure")

    def get_repo_default_branch(self, repo):
        self._maybe_raise("default_branch")
        return self._default_branch

    def list_repo_labels(self, repo):
        self._maybe_raise("labels")
        return list(self._labels)

    def list_repo_workflows(self, repo):
        self._maybe_raise("workflows")
        return list(self._workflows)

    def get_repo_file(self, repo, path, branch):
        self._maybe_raise("files")
        if path not in self._files:
            raise RuntimeError(f"not found: {path}")
        return self._files[path]

    def repo_file_exists(self, repo, path, branch):
        self._maybe_raise("exists")
        return path in self._files


def _status(machine, *, repos, degraded=None, online=True, reason=None):
    if not online:
        return MachineStatus(machine=machine, state=OFFLINE, latency_ms=None, reason=reason)
    return MachineStatus(
        machine=machine, state=ONLINE, latency_ms=3.0,
        health={
            "machine": machine.name,
            "capabilities": list(machine.capabilities),
            "repos": list(repos),
            "degraded": dict(degraded or {}),
        },
    )


def _checks(report) -> set[str]:
    return {f.check for f in report.findings}


def _healthy_machine(repo="r"):
    """A machine that has fully cleared layer 2, so a test aimed at layers 3-5
    isn't dragged down by an unrelated `config.no_machines` CRIT."""
    return ro.MachineFacts(name="laptop", declared=True, published_repos=[repo])


# ── The acceptance test ──────────────────────────────────────────────────────


PUSH_ONLY_WORKFLOW = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      # Deliberate trap: the STRING "pull_request" appears here, in a workflow
      # that never triggers on one. A grep-based check calls this green.
      - run: echo "${{ github.event.pull_request.number }}"
"""

PR_WORKFLOW = """\
name: CI
on:
  pull_request:
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""


class TestSeededHalfOnboardedFleet:
    """#2220 acceptance: four deliberate defects, four distinct named findings."""

    @pytest.fixture
    def report(self, seeded_config):
        cfg = seeded_config
        laptop, dellserver = cfg.machines

        statuses = [
            # DEFECT 1 — laptop has the repo in its live list but the clone is
            # not on disk, so the agent reports it degraded.
            _status(
                laptop, repos=["api"],
                degraded={"newrepo": "repo_path does not exist: /srv/newrepo"},
            ),
            # DEFECT 2 — dellserver's agent has never re-read config: `newrepo`
            # is in coordinator.yml for it, but /health neither serves it nor
            # calls it degraded. This is the stick-demo#1 shape (#2219).
            _status(dellserver, repos=["api"], degraded={}),
        ]

        ops = _FakeOps(
            # DEFECT 3 — no `coord` label: issues are live but invisible.
            labels=["tier:small", "tier:large", "bug"],
            # DEFECT 4 — a workflow exists but nothing triggers on
            # pull_request, so checks_absent blocks every merge forever.
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PUSH_ONLY_WORKFLOW, "CLAUDE.md": "# rules"},
        )

        facts = ro.gather_facts(cfg, "newrepo", statuses=statuses, ops=ops)
        return ro.evaluate(facts)

    def test_all_four_defects_produce_distinct_named_findings(self, report):
        found = _checks(report)
        for expected in (
            "machines.clone_missing",
            "machines.agent_repo_skew",
            "github.coord_label_missing",
            "github.no_pull_request_trigger",
        ):
            assert expected in found, f"{expected} missing from {sorted(found)}"

    def test_each_defect_names_the_right_subject_and_a_distinct_remedy(self, report):
        by_check = {f.check: f for f in report.findings}

        clone = by_check["machines.clone_missing"]
        assert clone.subject == "laptop"
        assert "/srv/newrepo" in clone.summary
        assert "restart" not in (clone.fix or "").lower()

        skew = by_check["machines.agent_repo_skew"]
        assert skew.subject == "dellserver"
        assert "does not call it degraded" in skew.summary
        # #2299: agents re-read coordinator.yml on their own /health tick, so
        # the remedy leads with "wait a poll", then the three things that can
        # actually keep a reload from landing (stale file on THAT machine, a
        # malformed edit, an agent too old to reload at all). Restarting a busy
        # agent — which kills its live workers — is no longer step one.
        assert "wait one /health poll" in (skew.fix or "")
        assert "git pull" in (skew.fix or "")
        assert "restart coord-agent" not in (skew.fix or "")

        # The two machine findings must not share a remedy — that is the whole
        # reason they are separate checks.
        assert clone.fix != skew.fix

    def test_report_is_not_ok_and_summary_counts_the_crits(self, report):
        assert not report.ok
        assert len(report.crits) >= 4
        summary = ro.summary_line(report)
        assert summary.startswith("REPO_DOCTOR: repo=newrepo")
        assert "ok=false" in summary

    def test_rendered_report_carries_every_check_id(self, report):
        text = "\n".join(ro.format_report(report))
        for expected in (
            "machines.clone_missing",
            "machines.agent_repo_skew",
            "github.coord_label_missing",
            "github.no_pull_request_trigger",
        ):
            assert f"[{expected}]" in text


class TestFullyOnboardedRepoIsGreen:
    """#2220's second acceptance bullet: a repo that cleared every layer must
    come out clean, or the verifier is just noise."""

    def test_no_crits(self, seeded_config):
        cfg = seeded_config
        statuses = [
            _status(m, repos=["api", "newrepo"]) for m in cfg.machines
        ]
        ops = _FakeOps(
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "# rules"},
        )
        facts = ro.gather_facts(cfg, "newrepo", statuses=statuses, ops=ops)
        report = ro.evaluate(facts)
        assert report.ok, [f.summary for f in report.crits]
        assert "machines.servable" in _checks(report)
        assert "github.pull_request_trigger_present" in _checks(report)


# ── Layer-by-layer ───────────────────────────────────────────────────────────


class TestConfigLayer:
    def test_unconfigured_repo_short_circuits_to_one_finding(self):
        report = ro.evaluate(ro.RepoFacts(name="ghost", configured=False))
        assert _checks(report) == {"config.repo_missing"}
        assert not report.ok

    def test_default_branch_mismatch_is_crit(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r",
            config_default_branch="main", smoke_command="make test",
            gh=ro.GithubFacts(slug="acme/r", default_branch="develop"),
        )
        report = ro.evaluate(facts)
        assert "config.default_branch_mismatch" in _checks(report)
        f = next(f for f in report.findings if f.check == "config.default_branch_mismatch")
        assert "develop" in f.summary and "main" in f.summary

    def test_unreadable_default_branch_is_unknown_not_a_pass_or_a_fail(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r",
            config_default_branch="main", smoke_command="make test",
            gh=ro.GithubFacts(slug="acme/r", default_branch_error="rate limited"),
        )
        report = ro.evaluate(facts)
        assert "config.default_branch_unknown" in _checks(report)
        assert "config.default_branch_mismatch" not in _checks(report)
        assert "config.default_branch_ok" not in _checks(report)

    def test_repo_no_machine_declares_is_crit(self, seeded_config):
        cfg = seeded_config
        # No machine lists 'orphan', so gather_facts finds zero MachineFacts.
        cfg.repos.append(type(cfg.repos[0])(name="orphan", github="acme/orphan"))
        facts = ro.gather_facts(cfg, "orphan", statuses=[], probe_github=False)
        report = ro.evaluate(facts)
        assert "config.no_machines" in _checks(report)


class TestMachinesLayer:
    def test_no_repo_path_entry_is_its_own_finding(self, seeded_config):
        cfg = seeded_config
        laptop, dellserver = cfg.machines
        statuses = [
            _status(
                laptop, repos=["api"],
                degraded={"newrepo": "no repo_path configured for this machine"},
            ),
            _status(dellserver, repos=["api", "newrepo"]),
        ]
        facts = ro.gather_facts(cfg, "newrepo", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)
        assert "machines.repo_path_missing" in _checks(report)
        # NOT confused with the clone or the stale-agent case.
        assert "machines.clone_missing" not in _checks(report)
        assert "machines.agent_repo_skew" not in _checks(report)

    def test_unreachable_agent_is_unknown_never_skew(self, seeded_config):
        cfg = seeded_config
        laptop, dellserver = cfg.machines
        statuses = [
            _status(laptop, repos=[], online=False, reason="connect timeout"),
            _status(dellserver, repos=["api", "newrepo"]),
        ]
        facts = ro.gather_facts(cfg, "newrepo", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)
        assert "machines.unreachable" in _checks(report)
        assert "machines.agent_repo_skew" not in _checks(report)

    def test_config_free_agent_is_not_reported_as_skew(self, seeded_config):
        """#1801: an ephemeral worker publishes no repos BY DESIGN. Reading
        that as onboarding skew is the false positive that made the #1712
        check useless for azure-epic1709."""
        cfg = seeded_config
        laptop, dellserver = cfg.machines
        st = _status(laptop, repos=[])
        st.health["config_free"] = "no local coordinator.yml and no board service"
        statuses = [st, _status(dellserver, repos=["api", "newrepo"])]
        facts = ro.gather_facts(cfg, "newrepo", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)
        assert "machines.config_free" in _checks(report)
        assert "machines.agent_repo_skew" not in _checks(report)

    def test_agent_predating_the_repos_field_is_unknown(self, seeded_config):
        cfg = seeded_config
        laptop, dellserver = cfg.machines
        st = MachineStatus(
            machine=laptop, state=ONLINE, latency_ms=1.0,
            health={"machine": "laptop", "capabilities": ["python"]},
        )
        statuses = [st, _status(dellserver, repos=["api", "newrepo"])]
        facts = ro.gather_facts(cfg, "newrepo", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)
        assert "machines.health_incomplete" in _checks(report)
        assert "machines.agent_repo_skew" not in _checks(report)


class TestWorkflowTriggerParsing:
    """``on`` is the YAML 1.1 boolean ``True`` once PyYAML is done with it —
    the single most likely way this check silently inverts."""

    def test_bare_on_key_parses_as_the_boolean_true(self):
        assert ro.workflow_triggers_on_pull_request(PR_WORKFLOW)

    def test_push_only_workflow_mentioning_pull_request_in_a_step_is_false(self):
        assert not ro.workflow_triggers_on_pull_request(PUSH_ONLY_WORKFLOW)

    def test_quoted_on_key(self):
        assert ro.workflow_triggers_on_pull_request(
            '"on":\n  pull_request:\njobs: {}\n'
        )

    def test_flow_list_form(self):
        assert ro.workflow_triggers_on_pull_request("on: [push, pull_request]\njobs: {}\n")

    def test_scalar_form(self):
        assert ro.workflow_triggers_on_pull_request("on: pull_request\njobs: {}\n")
        assert not ro.workflow_triggers_on_pull_request("on: push\njobs: {}\n")

    def test_unparseable_yaml_is_false_not_an_exception(self):
        assert not ro.workflow_triggers_on_pull_request("on: [unclosed\n")


class TestGithubLayer:
    def test_no_workflows_at_all_is_a_warn_not_a_crit(self):
        """A repo with genuinely no CI is a supported state — `expects_checks`
        handles it correctly. Only "workflows exist, none on pull_request" is
        the forever-blocked merge case."""
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            machines=[_healthy_machine()],
            gh=ro.GithubFacts(slug="acme/r", workflow_count=0, labels=["coord", "tier:small", "tier:large"]),
        )
        report = ro.evaluate(facts)
        assert "github.no_workflows" in _checks(report)
        assert "github.no_pull_request_trigger" not in _checks(report)
        assert report.ok

    def test_unreadable_workflow_file_does_not_masquerade_as_no_pr_trigger(self, seeded_config):
        ops = _FakeOps(
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={"CLAUDE.md": "# rules"},  # the workflow file itself is unreadable
        )
        facts = ro.gather_facts(seeded_config, "newrepo", statuses=[], ops=ops)
        report = ro.evaluate(facts)
        assert "github.workflows_unknown" in _checks(report)
        assert "github.no_pull_request_trigger" not in _checks(report)

    def test_unreadable_labels_are_unknown_not_missing(self, seeded_config):
        ops = _FakeOps(raise_on=("labels",))
        facts = ro.gather_facts(seeded_config, "newrepo", statuses=[], ops=ops)
        report = ro.evaluate(facts)
        assert "github.labels_unknown" in _checks(report)
        assert "github.coord_label_missing" not in _checks(report)

    def test_missing_tier_labels_are_a_warn(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            machines=[_healthy_machine()],
            gh=ro.GithubFacts(slug="acme/r", labels=["coord"]),
        )
        report = ro.evaluate(facts)
        assert "github.tier_labels_missing" in _checks(report)
        assert report.ok  # a WARN must not gate

    def test_label_match_is_case_insensitive(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            gh=ro.GithubFacts(slug="acme/r", labels=["Coord", "Tier:Small", "tier:large"]),
        )
        report = ro.evaluate(facts)
        assert "github.coord_label_present" in _checks(report)
        assert "github.tier_labels_missing" not in _checks(report)


class TestContentsLayer:
    def test_missing_claude_md_is_crit(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            gh=ro.GithubFacts(slug="acme/r", claude_md_present=False),
        )
        report = ro.evaluate(facts)
        assert "contents.claude_md_missing" in _checks(report)
        assert not report.ok

    def test_unresolvable_test_command_is_crit(self):
        facts = ro.RepoFacts(name="r", configured=True, github="acme/r", smoke_command=None)
        report = ro.evaluate(facts)
        assert "contents.test_command_unresolved" in _checks(report)

    def test_resolved_test_command_names_its_source(self, seeded_config):
        facts = ro.gather_facts(seeded_config, "newrepo", statuses=[], probe_github=False)
        assert facts.smoke_command == "make test"
        assert "newrepo" in (facts.smoke_command_source or "")

    # ── #3037: graphify-out/ guard ───────────────────────────────────────────

    def test_neither_guard_present_warns(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            machines=[_healthy_machine()],
            gh=ro.GithubFacts(
                slug="acme/r",
                graphify_out_gitignore_present=False,
                root_gitignore_has_graphify_out=False,
            ),
        )
        report = ro.evaluate(facts)
        assert "contents.graphify_out_unguarded" in _checks(report)
        # WARN, not CRIT — an unguarded repo works fine until someone runs
        # `git add -A`, so it must not hard-gate doctor's exit code.
        assert report.ok
        f = next(f for f in report.findings if f.check == "contents.graphify_out_unguarded")
        assert f.severity == ro.WARN

    def test_self_ignoring_gitignore_guards(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            gh=ro.GithubFacts(
                slug="acme/r",
                graphify_out_gitignore_present=True,
                root_gitignore_has_graphify_out=False,
            ),
        )
        report = ro.evaluate(facts)
        assert "contents.graphify_out_guarded" in _checks(report)
        assert "contents.graphify_out_unguarded" not in _checks(report)

    def test_root_gitignore_form_guards_no_false_positive(self):
        """space-invaders and grocery-list guard graphify-out/ via a line in
        the ROOT .gitignore, not a graphify-out/.gitignore of their own. A
        naive "is graphify-out/.gitignore tracked?" probe reported both as
        unguarded on the first pass of the fleet audit that found #3037 —
        this is the regression test for that false positive.
        """
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            gh=ro.GithubFacts(
                slug="acme/r",
                graphify_out_gitignore_present=False,
                root_gitignore_has_graphify_out=True,
            ),
        )
        report = ro.evaluate(facts)
        assert "contents.graphify_out_guarded" in _checks(report)
        assert "contents.graphify_out_unguarded" not in _checks(report)

    def test_guard_probe_error_is_unknown_not_warn(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            machines=[_healthy_machine()],
            gh=ro.GithubFacts(
                slug="acme/r",
                graphify_out_gitignore_error="rate limited",
                root_gitignore_has_graphify_out=False,
            ),
        )
        report = ro.evaluate(facts)
        assert "contents.graphify_out_guard_unknown" in _checks(report)
        assert "contents.graphify_out_unguarded" not in _checks(report)
        assert report.ok  # UNKNOWN must never gate

    def test_unguarded_fix_is_informational_not_automatic(self):
        """`--fix`'s contract is idempotent, machine-local graph repair only
        (coord/commands/repo.py's `_run_graph_fix` calls `/graph-fix` and the
        local clone fixer — nothing content-layer). The finding's `fix` text
        must say so rather than implying `--fix` will handle it.
        """
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="make test",
            gh=ro.GithubFacts(
                slug="acme/r",
                graphify_out_gitignore_present=False,
                root_gitignore_has_graphify_out=False,
            ),
        )
        report = ro.evaluate(facts)
        f = next(f for f in report.findings if f.check == "contents.graphify_out_unguarded")
        assert "not automatic" in (f.fix or "")
        assert "--fix" in (f.fix or "")


class TestGraphifyOutGitignoreLineMatcher:
    """Pure unit tests for `root_gitignore_ignores_graphify_out` — the parser
    behind the root-.gitignore guard shape."""

    @pytest.mark.parametrize("line", [
        "graphify-out/",
        "graphify-out",
        "/graphify-out/",
        "/graphify-out",
        "graphify-out/*",
        "graphify-out/**",
    ])
    def test_recognized_forms(self, line):
        content = f"node_modules/\n{line}\n*.pyc\n"
        assert ro.root_gitignore_ignores_graphify_out(content)

    def test_comment_mentioning_it_does_not_count(self):
        content = "# ignore graphify-out/ contents\n*.pyc\n"
        assert not ro.root_gitignore_ignores_graphify_out(content)

    def test_unrelated_gitignore_does_not_count(self):
        content = "node_modules/\n*.pyc\ndist/\n"
        assert not ro.root_gitignore_ignores_graphify_out(content)

    def test_partial_name_does_not_false_positive(self):
        content = "not-graphify-out/\n"
        assert not ro.root_gitignore_ignores_graphify_out(content)


class TestGraphLayer:
    def test_absent_local_clone_is_a_skip_not_a_pass(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            machines=[_healthy_machine()],
        )
        report = ro.evaluate(facts)
        assert "graph.not_probed" in _checks(report)
        assert report.ok  # UNKNOWN must never gate

    def test_unbuilt_graph_warns_with_the_build_remedy(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            graph=ro.GraphFacts(probed=True, repo_path="/srv/r", built=False),
        )
        report = ro.evaluate(facts)
        f = next(f for f in report.findings if f.check == "graph.not_built")
        # #2237: the remedy used to read `graphify build`, which is not a
        # subcommand graphify has — an operator following it verbatim got
        # "Run 'graphify --help' for full usage." and no graph. The real
        # build-from-nothing command is `graphify update .` (AST-only, no LLM;
        # docs/GRAPHIFY_SETUP.md), and it is what the hooks, the agent's
        # self-heal and `--fix` all run.
        assert "graphify update ." in (f.fix or "")
        assert "--fix" in (f.fix or "")
        assert "graphify build" not in (f.fix or "")

    def test_missing_hooks_are_their_own_finding(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            graph=ro.GraphFacts(
                probed=True, repo_path="/srv/r", built=True, fresh=True,
                hooks_installed=False, hooks_detail="core.hooksPath unset",
                hooks_shipped=True,
            ),
        )
        report = ro.evaluate(facts)
        assert "graph.hooks_missing" in _checks(report)
        assert "graph.fresh" in _checks(report)

    def test_repo_that_never_ported_the_hook_gets_the_porting_remedy(self):
        """#2236: `hooks_installed=False` hides two different failures.  A repo
        that never ported `.githooks/post-checkout` (coord-portal, stick-demo)
        must NOT be told to set `core.hooksPath` — pointing git at a directory
        that does not exist silently disables every hook in the checkout."""
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            graph=ro.GraphFacts(
                probed=True, repo_path="/srv/r", built=False,
                hooks_installed=False, hooks_shipped=False,
                hooks_detail="no .githooks/post-checkout in this repo",
            ),
        )
        report = ro.evaluate(facts)
        checks = _checks(report)
        assert "graph.hooks_not_ported" in checks
        assert "graph.hooks_missing" not in checks
        f = next(f for f in report.findings if f.check == "graph.hooks_not_ported")
        assert "port .githooks/" in (f.fix or "")
        # The one-git-config remedy must not leak into this branch.
        assert not (f.fix or "").strip().startswith("git config")


class TestOracleLayer:
    """#2748 (IL-2): layer 6, oracle-loop readiness. Never CRITs on "no
    driver at all" — `coord acceptance mock` needs none — only on a driver
    that claims to be wired but demonstrably is not."""

    def test_no_driver_configured_is_a_warn_not_a_crit(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            machines=[_healthy_machine()],
        )
        report = ro.evaluate(facts)
        assert "oracle.no_driver" in _checks(report)
        assert report.ok  # a repo with no driver at all is not a failure
        f = next(f for f in report.findings if f.check == "oracle.no_driver")
        assert "coord acceptance mock" in f.summary
        assert "coord repo create" in (f.fix or "")

    def test_directory_discovered_driver_needs_no_entrypoint(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            machines=[_healthy_machine()],
            acceptance=ro.AcceptanceFacts(configured=True, kinds=["cli-pytest"]),
        )
        report = ro.evaluate(facts)
        checks = _checks(report)
        assert "oracle.driver_declared" in checks
        assert "oracle.sealed_paths_resolvable" not in checks
        assert "oracle.entrypoint_not_required" in checks
        assert "oracle.fixture_server_not_needed" in checks
        assert report.ok

    def test_missing_entrypoint_on_disk_is_crit(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            machines=[_healthy_machine()],
            acceptance=ro.AcceptanceFacts(
                configured=True, kinds=["tui-tuidriver"],
                entrypoints=["tui/tests/acceptance.rs"],
                entrypoints_missing=["tui/tests/acceptance.rs"],
            ),
        )
        report = ro.evaluate(facts)
        checks = _checks(report)
        assert "oracle.entrypoint_missing" in checks
        # #2748 review: this finding used to run alongside a hardcoded-OK
        # `oracle.sealed_paths_resolvable` for the very same declared path,
        # so the same report simultaneously claimed the path "resolves" and
        # "does not exist". That finding is gone now — the entrypoint branch
        # is the only signal for sealed-path resolvability.
        assert "oracle.sealed_paths_resolvable" not in checks
        assert not report.ok
        f = next(f for f in report.findings if f.check == "oracle.entrypoint_missing")
        assert "tui/tests/acceptance.rs" in f.summary
        assert "1552" in f.summary

    def test_entrypoint_present_on_disk_is_ok(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            machines=[_healthy_machine()],
            acceptance=ro.AcceptanceFacts(
                configured=True, kinds=["tui-tuidriver"],
                entrypoints=["tui/tests/acceptance.rs"], entrypoints_missing=[],
            ),
        )
        report = ro.evaluate(facts)
        assert "oracle.entrypoint_present" in _checks(report)
        assert report.ok

    def test_entrypoint_declared_but_not_probed_is_unknown_not_a_pass(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            machines=[_healthy_machine()],
            acceptance=ro.AcceptanceFacts(
                configured=True, kinds=["tui-tuidriver"],
                entrypoints=["tui/tests/acceptance.rs"], entrypoints_missing=None,
            ),
        )
        report = ro.evaluate(facts)
        assert "oracle.entrypoint_not_probed" in _checks(report)
        assert "oracle.entrypoint_present" not in _checks(report)
        assert report.ok  # UNKNOWN must never gate

    def test_web_playwright_flags_the_unshipped_fixture_server(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            machines=[_healthy_machine()],
            acceptance=ro.AcceptanceFacts(
                configured=True, kinds=["web-playwright"],
                fixture_server_dependent=True,
            ),
        )
        report = ro.evaluate(facts)
        f = next(f for f in report.findings if f.check == "oracle.fixture_server_unmet")
        assert f.severity == ro.WARN
        assert "1538" in f.summary
        assert report.ok  # a smoke net is a WARN, not a hard gate

    def test_cli_pytest_does_not_flag_a_fixture_server_dependency(self):
        facts = ro.RepoFacts(
            name="r", configured=True, github="acme/r", smoke_command="t",
            machines=[_healthy_machine()],
            acceptance=ro.AcceptanceFacts(configured=True, kinds=["cli-pytest"]),
        )
        report = ro.evaluate(facts)
        assert "oracle.fixture_server_unmet" not in _checks(report)
        assert "oracle.fixture_server_not_needed" in _checks(report)

    def test_gather_acceptance_facts_detects_a_missing_entrypoint_on_a_real_clone(
        self, tmp_path
    ):
        """End-to-end through `gather_facts`: a driver with a declared
        `entrypoint:` that does not exist in the local clone must surface as
        `entrypoint_missing`, not a silent `entrypoint_present`."""
        config = SEEDED_CONFIG + """\

acceptance:
  drivers:
    newrepo:
      kind: tui-tuidriver
      run: "cargo test --test acceptance"
      entrypoint: tui/tests/acceptance.rs
"""
        p = tmp_path / "coordinator.yml"
        p.write_text(config)
        cfg = load_config(p)
        clone = tmp_path / "clone"
        clone.mkdir()

        facts = ro.gather_facts(
            cfg, "newrepo", statuses=[], probe_github=False, local_clone=clone,
        )
        assert facts.acceptance.configured
        assert facts.acceptance.kinds == ["tui-tuidriver"]
        assert facts.acceptance.entrypoints_missing == ["tui/tests/acceptance.rs"]

        report = ro.evaluate(facts)
        assert "oracle.entrypoint_missing" in _checks(report)

    def test_gather_acceptance_facts_confirms_an_existing_entrypoint(self, tmp_path):
        config = SEEDED_CONFIG + """\

acceptance:
  drivers:
    newrepo:
      kind: tui-tuidriver
      run: "cargo test --test acceptance"
      entrypoint: tui/tests/acceptance.rs
"""
        p = tmp_path / "coordinator.yml"
        p.write_text(config)
        cfg = load_config(p)
        clone = tmp_path / "clone"
        (clone / "tui" / "tests").mkdir(parents=True)
        (clone / "tui" / "tests" / "acceptance.rs").write_text("// wired\n")

        facts = ro.gather_facts(
            cfg, "newrepo", statuses=[], probe_github=False, local_clone=clone,
        )
        assert facts.acceptance.entrypoints_missing == []

        report = ro.evaluate(facts)
        assert "oracle.entrypoint_present" in _checks(report)

    def test_gather_acceptance_facts_derives_fixture_dependency_from_kind(
        self, tmp_path
    ):
        """`fixture_server_dependent` must be DERIVED from the driver's
        `kind` (via `coord.acceptance_drivers.FIXTURE_SERVER_DEPENDENT_KINDS`)
        by `gather_acceptance_facts`, not left for every caller to guess."""
        config = SEEDED_CONFIG + """\

acceptance:
  drivers:
    newrepo:
      kind: web-playwright
      run: "npx playwright test tests/acceptance"
"""
        p = tmp_path / "coordinator.yml"
        p.write_text(config)
        cfg = load_config(p)

        facts = ro.gather_facts(cfg, "newrepo", statuses=[], probe_github=False)
        assert facts.acceptance.fixture_server_dependent is True

        report = ro.evaluate(facts)
        assert "oracle.fixture_server_unmet" in _checks(report)

    def test_unconfigured_repo_never_reaches_the_oracle_layer(self):
        report = ro.evaluate(ro.RepoFacts(name="ghost", configured=False))
        assert "oracle.no_driver" not in _checks(report)


class TestDoctorFolding:
    """``coord doctor`` folds in the LIVE layer only — see
    :func:`coord.repo_onboard.doctor_summary_lines`."""

    def test_only_crits_from_the_machines_layer_reach_the_fleet_report(self, seeded_config):
        cfg = seeded_config
        laptop, dellserver = cfg.machines
        statuses = [
            _status(laptop, repos=["api", "newrepo"]),
            _status(dellserver, repos=["api"], degraded={}),
        ]
        facts = ro.gather_facts(cfg, "newrepo", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)
        lines = ro.doctor_summary_lines(report)
        assert len(lines) == 1
        assert "machines.agent_repo_skew" in lines[0][1]
        assert lines[0][0] is True

    def test_config_derivable_crits_are_not_duplicated_into_the_fleet_report(self):
        """A missing test_command is a real CRIT for `coord repo doctor`, but
        it is equally visible from a config read that costs nothing — putting
        it in the fleet report buries the finding that needed a live probe."""
        facts = ro.RepoFacts(name="r", configured=True, github="acme/r", smoke_command=None)
        report = ro.evaluate(facts)
        assert "contents.test_command_unresolved" in _checks(report)
        assert ro.doctor_summary_lines(report) == []
