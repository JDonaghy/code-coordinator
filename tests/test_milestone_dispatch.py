"""Tests for coord.milestone_dispatch — #769 Phase 1 (milestone dispatch:
machine picking + actual dispatch on top of Phase 0's pure frontier).

Pure-function tests (``pick_machine`` / ``plan_dispatch`` / ``is_milestone_
complete``) seed a :class:`~coord.models.Board` + :class:`~coord.config.
Config` directly — no GitHub, no HTTP. ``dispatch_entry`` / ``fetch_
milestone_context`` tests mock ``coord.github_ops`` and ``coord.dispatch``
so no live network call ever happens. CLI-level black-box coverage
(including the #769 acceptance-criteria scenario) lives in
tests/test_cli_milestone_dispatch.py.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coord.config import Config, ModelsConfig, ProviderDef, ProvidersConfig
from coord.milestone_dispatch import (
    MilestoneContext,
    MilestoneDispatchError,
    dispatch_entry,
    fetch_milestone_context,
    gate_a_status,
    is_milestone_complete,
    issue_oracle_ready,
    pick_machine,
    plan_dispatch,
    plan_queue,
)
from coord.milestone_order import WorkOrder, WorkOrderNode
from coord.models import Assignment, Board, Machine, Repo


def _config(machines: list[Machine], repos: list[Repo] | None = None) -> Config:
    repos = repos or [Repo(name="api", github="acme/api")]
    return Config(repos=repos, machines=machines)


def _machine(name: str, repos: list[str], repo_paths: dict[str, str] | None = None) -> Machine:
    if repo_paths is None:
        repo_paths = {r: f"/tmp/{r}" for r in repos}
    return Machine(name=name, host=f"{name}.tailnet", repos=repos, repo_paths=repo_paths)


def _running(machine_name: str, issue: int, repo: str = "api") -> Assignment:
    return Assignment(
        machine_name=machine_name,
        repo_name=repo,
        issue_number=issue,
        issue_title="t",
        status="running",
        assignment_id=f"a{issue}",
        type="work",
    )


WORK_ORDER = WorkOrder(
    nodes=(
        WorkOrderNode(762, group="A"),
        WorkOrderNode(763, group="A"),
        WorkOrderNode(765, after=(762, 763)),
    )
)


# ── pick_machine ─────────────────────────────────────────────────────────────


class TestPickMachine:
    def test_picks_idle_capable_machine(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        m = pick_machine("api", board, cfg)
        assert m is not None
        assert m.name == "laptop"

    def test_excludes_machine_without_repo_in_repos_list(self) -> None:
        """The #688 mechanism: a machine whose `repos:` omits the target repo
        (e.g. dellserver's coordinator.yml entry omitting claude-coordinator)
        is never picked — no special-case "coord-self" code needed."""
        cfg = _config([_machine("dellserver", ["quadraui"])])
        board = Board()
        assert pick_machine("claude-coordinator", board, cfg) is None

    def test_excludes_busy_machine(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board(active=[_running("laptop", 1)])
        assert pick_machine("api", board, cfg) is None

    def test_excludes_paused_machine(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        with patch("coord.machine_pause.paused_set", return_value={"laptop"}):
            assert pick_machine("api", board, cfg) is None

    def test_excludes_machine_without_repo_path(self) -> None:
        cfg = _config([_machine("laptop", ["api"], repo_paths={})])
        board = Board()
        assert pick_machine("api", board, cfg) is None

    def test_respects_exclude_set(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        assert pick_machine("api", board, cfg, exclude=frozenset({"laptop"})) is None

    def test_first_match_wins_in_config_order(self) -> None:
        cfg = _config([_machine("laptop", ["api"]), _machine("server", ["api"])])
        board = Board()
        m = pick_machine("api", board, cfg)
        assert m.name == "laptop"

    def test_returns_none_when_no_idle_machine(self) -> None:
        cfg = _config([])
        board = Board()
        assert pick_machine("api", board, cfg) is None


# ── plan_dispatch ────────────────────────────────────────────────────────────


class TestPlanDispatch:
    def test_cohort_fans_out_to_distinct_machines(self) -> None:
        cfg = _config([_machine("laptop", ["api"]), _machine("server", ["api"])])
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues=set())

        ready_issues = {p.entry.issue_number for p in plan.to_dispatch}
        assert ready_issues == {762, 763}
        picked_machines = {p.machine.name for p in plan.to_dispatch}
        assert picked_machines == {"laptop", "server"}  # distinct, no double-booking

        waiting_issues = {b.issue_number for b in plan.waiting}
        assert waiting_issues == {765}

    def test_ready_entries_beyond_idle_machines_are_skipped(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])  # only one idle machine
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues=set())

        assert len(plan.to_dispatch) == 1
        assert len(plan.skipped) == 1
        # The one dispatched + the one skipped account for the full cohort.
        covered = {plan.to_dispatch[0].entry.issue_number, plan.skipped[0].entry.issue_number}
        assert covered == {762, 763}
        assert "no idle machine" in plan.skipped[0].reason

    def test_cohort_merging_unblocks_gated_node(self) -> None:
        """#769 acceptance criteria (second half): once the group:A cohort is
        terminal, the after-gated node enters the ready frontier."""
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues={762, 763})

        assert [p.entry.issue_number for p in plan.to_dispatch] == [765]
        assert plan.waiting == ()

    def test_all_terminal_yields_empty_plan(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues={762, 763, 765})
        assert plan.to_dispatch == ()
        assert plan.skipped == ()
        assert plan.waiting == ()

    # ── oracle_loop serialization (#2542) ────────────────────────────────────
    #
    # `_milestone_gate_tick`'s `work` DISPATCH state and the legacy
    # `_milestone_drain_tick` both call `plan_dispatch` directly (not through
    # the drive-queue `plan_queue` chains), so the same "two same-milestone
    # entries must never both start their JIT slice-authoring phase in one
    # tick" hazard `plan_queue`'s `oracle_loop` param closes for the
    # drive-queue path has to be closed here too — the reviewer's blocking
    # finding on this issue: the gate tick received no oracle_loop awareness
    # at all in the first pass.

    def test_oracle_loop_caps_dispatch_at_one_entry_per_tick(self) -> None:
        """#762/#763 share {group: A} — normally independent (no claim or
        `after` edge between them) and, per
        `test_cohort_fans_out_to_distinct_machines` above, both fan out to
        distinct idle machines in the SAME call. Under oracle-loop control
        only the first may dispatch this tick; the second is held back as
        `deferred`, not silently dropped."""
        cfg = _config([_machine("laptop", ["api"]), _machine("server", ["api"])])
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(
            WORK_ORDER, board, cfg, repo, terminal_issues=set(), oracle_loop=True
        )

        assert [p.entry.issue_number for p in plan.to_dispatch] == [762]
        assert [d.entry.issue_number for d in plan.deferred] == [763]
        assert "one entry dispatches per tick" in plan.deferred[0].reason
        # #765's own `after` edge is unaffected — still correctly waiting.
        assert {b.issue_number for b in plan.waiting} == {765}

    def test_oracle_loop_false_is_unchanged_from_default(self) -> None:
        """Explicit `oracle_loop=False` must be byte-identical to omitting
        it — every existing caller keeps today's fan-out behaviour exactly."""
        cfg = _config([_machine("laptop", ["api"]), _machine("server", ["api"])])
        board = Board()
        repo = cfg.repo("api")
        default_plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues=set())
        explicit_plan = plan_dispatch(
            WORK_ORDER, board, cfg, repo, terminal_issues=set(), oracle_loop=False
        )
        assert default_plan == explicit_plan
        assert default_plan.deferred == ()

    def test_oracle_loop_no_machine_available_reports_skipped_not_swallowed(self) -> None:
        """The one-dispatch-slot cap only engages once an entry is actually
        picked (a machine found for it) — when nothing can dispatch at all,
        every ready entry still surfaces as `skipped` (no idle machine), the
        real reason, rather than being silently reclassified as `deferred`."""
        cfg = _config([])  # no machines at all
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(
            WORK_ORDER, board, cfg, repo, terminal_issues=set(), oracle_loop=True
        )
        assert plan.to_dispatch == ()
        assert plan.deferred == ()
        assert {s.entry.issue_number for s in plan.skipped} == {762, 763}


# ── plan_queue (#2335) ───────────────────────────────────────────────────────


class TestPlanQueue:
    def test_whole_dag_in_declared_order_with_qualified_after_keys(self) -> None:
        plan = plan_queue(WORK_ORDER, terminal_issues=set(), repo_name="api")
        assert [q.issue_number for q in plan] == [762, 763, 765]
        assert plan[0].after == ()
        assert plan[0].group == "A"
        assert plan[1].after == ()
        assert plan[2].after == ("api#762", "api#763")
        assert plan[2].group is None

    def test_terminal_nodes_are_dropped_and_their_edges_filtered(self) -> None:
        plan = plan_queue(WORK_ORDER, terminal_issues={762}, repo_name="api")
        assert [q.issue_number for q in plan] == [763, 765]
        # The closed pre-req never enters the queue, so its edge is dropped;
        # the still-open one is kept.
        assert plan[1].after == ("api#763",)

    def test_all_terminal_yields_empty_plan(self) -> None:
        plan = plan_queue(WORK_ORDER, terminal_issues={762, 763, 765}, repo_name="api")
        assert plan == ()

    def test_dependent_declared_before_prereq_sorts_topologically(self) -> None:
        order = WorkOrder(
            nodes=(WorkOrderNode(765, after=(762,)), WorkOrderNode(762))
        )
        plan = plan_queue(order, terminal_issues=set(), repo_name="api")
        assert [q.issue_number for q in plan] == [762, 765]
        assert plan[1].after == ("api#762",)

    def test_independent_nodes_keep_declared_order(self) -> None:
        order = WorkOrder(
            nodes=(WorkOrderNode(3), WorkOrderNode(1), WorkOrderNode(2))
        )
        plan = plan_queue(order, terminal_issues=set(), repo_name="api")
        assert [q.issue_number for q in plan] == [3, 1, 2]

    # ── oracle_loop serialization (#2542) ────────────────────────────────────

    def test_oracle_loop_false_is_unchanged_from_default(self) -> None:
        """Explicit `oracle_loop=False` must be byte-identical to omitting
        it — every existing caller keeps today's behaviour exactly."""
        plan = plan_queue(
            WORK_ORDER, terminal_issues=set(), repo_name="api", oracle_loop=False
        )
        assert plan == plan_queue(WORK_ORDER, terminal_issues=set(), repo_name="api")

    def test_oracle_loop_chains_same_group_cohort(self) -> None:
        """#762/#763 share {group: A} — normally independent (no `after`
        between them) — but under oracle-loop control the second entry in
        topological order gets an implicit `after` onto the first, so the
        drive-queue can never launch both at once (coord-portal#122's
        original #128/#132 collision)."""
        plan = plan_queue(
            WORK_ORDER, terminal_issues=set(), repo_name="api", oracle_loop=True
        )
        assert [q.issue_number for q in plan] == [762, 763, 765]
        assert plan[0].after == ()
        assert plan[1].after == ("api#762",)
        # #765 already declared `after: #762,#763` — the implicit chain onto
        # its immediate predecessor (#763) is already covered, not duplicated.
        assert plan[2].after == ("api#762", "api#763")

    def test_oracle_loop_chains_ungrouped_independent_nodes_too(self) -> None:
        """#2542's SECOND collision (#130 vs. #132): no declared group at
        all, just independent nodes the drive-queue's per-repo concurrency
        cap would otherwise launch concurrently. oracle_loop serializes
        those too, not just declared-group cohorts."""
        order = WorkOrder(
            nodes=(WorkOrderNode(129), WorkOrderNode(130), WorkOrderNode(131))
        )
        plan = plan_queue(
            order, terminal_issues=set(), repo_name="portal", oracle_loop=True
        )
        assert [q.issue_number for q in plan] == [129, 130, 131]
        assert plan[0].after == ()
        assert plan[1].after == ("portal#129",)
        assert plan[2].after == ("portal#130",)

    def test_oracle_loop_terminal_predecessor_never_becomes_an_edge(self) -> None:
        """The implicit chain only ever points at another QUEUED (non-
        terminal) entry — a closed predecessor is dropped from `ordered`
        entirely, so it can never end up named in a later entry's `after`."""
        plan = plan_queue(
            WORK_ORDER, terminal_issues={762}, repo_name="api", oracle_loop=True
        )
        assert [q.issue_number for q in plan] == [763, 765]
        assert plan[0].after == ()
        assert plan[1].after == ("api#763",)


# ── is_milestone_complete ────────────────────────────────────────────────────


class TestIsMilestoneComplete:
    def test_false_when_any_node_open(self) -> None:
        ctx = MilestoneContext(
            tracking_issue=100, milestone_number=9, work_order=WORK_ORDER,
            terminal_issues=frozenset({762, 763}),
        )
        assert is_milestone_complete(ctx) is False

    def test_true_when_all_terminal(self) -> None:
        ctx = MilestoneContext(
            tracking_issue=100, milestone_number=9, work_order=WORK_ORDER,
            terminal_issues=frozenset({762, 763, 765}),
        )
        assert is_milestone_complete(ctx) is True


# ── gate_a_status (#930, docs/ORACLE_LOOP.md Gate A) ─────────────────────────


class TestGateAStatus:
    def _cfg(self, *, with_driver: bool) -> Config:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        drivers = {}
        if with_driver:
            drivers["api"] = AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test")
        return Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[_machine("laptop", ["api"])],
            acceptance=AcceptanceConfig(drivers=drivers),
        )

    def test_none_when_no_acceptance_driver_configured(self) -> None:
        cfg = self._cfg(with_driver=False)
        repo = cfg.repo("api")
        assert gate_a_status(repo, cfg, 9, file_exists=lambda *a: False) is None

    def _routed_cfg(self) -> Config:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        drivers = {
            "api": AcceptanceDriverConfig(routes=[
                AcceptanceDriverConfig(match="**", kind="cli-pytest", run="pytest"),
            ]),
        }
        return Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[_machine("laptop", ["api"])],
            acceptance=AcceptanceConfig(drivers=drivers),
        )

    def test_blocked_when_driver_is_routed_and_contract_missing(self) -> None:
        """#1125 review finding 1: a routed driver (acceptance.drivers.<repo>
        .routes) must still gate Gate A — `driver_for(repo_cfg.name)` (no
        path) can't select a route and would otherwise silently return
        None, making gate_a_status wrongly report "dispatch may proceed"
        for every milestone the instant this repo's driver becomes routed.
        """
        cfg = self._routed_cfg()
        repo = cfg.repo("api")
        reason = gate_a_status(repo, cfg, 9, file_exists=lambda *a: False)
        assert reason is not None
        assert "tests/acceptance/ms-9/contract.md" in reason

    def test_blocked_when_contract_missing(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        reason = gate_a_status(repo, cfg, 9, file_exists=lambda *a: False)
        assert reason is not None
        assert "tests/acceptance/ms-9/contract.md" in reason
        assert "coord acceptance mock api" in reason

    def test_none_when_contract_exists(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        assert gate_a_status(repo, cfg, 9, file_exists=lambda *a: True) is None

    def test_file_exists_called_with_expected_args(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        calls: list[tuple] = []

        def _check(repo_github: str, path: str, branch: str) -> bool:
            calls.append((repo_github, path, branch))
            return True

        gate_a_status(repo, cfg, 9, file_exists=_check)
        assert calls == [("acme/api", "tests/acceptance/ms-9/contract.md", "main")]

    def test_default_file_exists_treats_runtime_error_as_missing(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        with patch("coord.github_ops.get_repo_file", side_effect=RuntimeError("404")):
            reason = gate_a_status(repo, cfg, 9)
        assert reason is not None

    def test_default_file_exists_true_when_no_error(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        with patch("coord.github_ops.get_repo_file", return_value="contract body"):
            reason = gate_a_status(repo, cfg, 9)
        assert reason is None


class TestGateAStatusRelocatedSlices:
    """#2896: a repo with an entrypoint-linked route (the relocated
    tui-tuidriver slices) must be checked at BOTH the shared repo-root tree
    AND that route's own sibling dir — a bare milestone number doesn't say
    which one a given milestone's contract actually lives under."""

    def _cfg(self) -> Config:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        return Config(
            repos=[Repo(name="claude-coordinator", github="acme/claude-coordinator")],
            machines=[_machine("laptop", ["claude-coordinator"])],
            acceptance=AcceptanceConfig(drivers={
                "claude-coordinator": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(match="coord/**", kind="cli-pytest", run="pytest"),
                    AcceptanceDriverConfig(
                        match="tui/**", kind="tui-tuidriver", run="cargo test",
                        entrypoint="tui/tests/acceptance.rs",
                    ),
                ]),
            }),
        )

    def test_passes_when_only_the_relocated_root_has_the_contract(self) -> None:
        """The shared repo-root candidate is missing (ms-65 lives under
        tui/tests/acceptance/ instead) — Gate A must still pass rather than
        reporting a false "not satisfied" for a contract that exists, just
        not at the legacy path."""
        cfg = self._cfg()
        repo = cfg.repo("claude-coordinator")

        def _check(repo_github: str, path: str, branch: str) -> bool:
            return path == "tui/tests/acceptance/ms-65/contract.md"

        assert gate_a_status(repo, cfg, 65, file_exists=_check) is None

    def test_checks_both_candidates_in_order(self) -> None:
        cfg = self._cfg()
        repo = cfg.repo("claude-coordinator")
        calls: list[str] = []

        def _check(repo_github: str, path: str, branch: str) -> bool:
            calls.append(path)
            return False

        gate_a_status(repo, cfg, 65, file_exists=_check)
        assert calls == [
            "tests/acceptance/ms-65/contract.md",
            "tui/tests/acceptance/ms-65/contract.md",
        ]

    def test_reason_names_every_candidate_when_none_exist(self) -> None:
        cfg = self._cfg()
        repo = cfg.repo("claude-coordinator")
        reason = gate_a_status(repo, cfg, 65, file_exists=lambda *a: False)
        assert reason is not None
        assert "tests/acceptance/ms-65/contract.md" in reason
        assert "tui/tests/acceptance/ms-65/contract.md" in reason


# ── issue_oracle_ready (#1138, docs/ORACLE_LOOP.md issue-level gate) ─────────


def _oracle_cfg(*, kind: str = "cli-pytest", routes: list | None = None) -> Config:
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig

    if routes is not None:
        entry = AcceptanceDriverConfig(routes=routes)
    else:
        entry = AcceptanceDriverConfig(kind=kind, run="pytest")
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[_machine("laptop", ["api"])],
        acceptance=AcceptanceConfig(drivers={"api": entry}),
    )


#: The Gate-A contract these fixtures pretend is on the default branch.
#: #2063 made `issue_oracle_ready` read its CONTENT (not just its
#: existence), so every fixture that expects dispatch to proceed must serve
#: one and record a matching approval.
CONTRACT = "# Contract\n\n- the Save button says `Save`\n"


def _manifest_fetch(
    mapping: dict[str, str | None],
    *,
    milestone: int = 37,
    contract: str | None = CONTRACT,
):
    """Build a ManifestFetch stub: {path: content_or_None}. Any path not in
    *mapping* returns None (file doesn't exist), matching the real
    GitHub-fetch semantics.

    The milestone's ``contract.md`` is served by default (#2063) so these
    fixtures exercise the *slice* gate rather than tripping over the
    sign-off gate that now runs ahead of it; pass ``contract=None`` to
    simulate a contract that can't be read.
    """
    mapping = dict(mapping)
    if contract is not None:
        mapping.setdefault(f"tests/acceptance/ms-{milestone}/contract.md", contract)

    def _fetch(repo_github: str, path: str, branch: str) -> str | None:
        return mapping.get(path)

    return _fetch


def _approval(
    *,
    repo_name: str = "api",
    milestone: int = 37,
    contract: str | None = CONTRACT,
    verdict: str = "approved",
):
    """A `fetch_gate_a_approval` stub returning a verdict keyed to *contract*.

    ``contract=None`` yields "nobody has recorded anything" — the #2063
    refusal.
    """
    from coord.gate_a import contract_digest, make_record

    if contract is None:
        return lambda *_a: None
    record = make_record(
        repo_name=repo_name,
        milestone_number=milestone,
        verdict=verdict,
        contract_sha=contract_digest(contract),
        now=1000.0,
    ).to_dict()
    return lambda *_a: record


class TestIssueOracleReady:
    def test_not_applicable_when_no_milestone(self) -> None:
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        readiness = issue_oracle_ready(
            repo, cfg, None, 1118, file_exists=lambda *a: True,
        )
        assert readiness.applies is False
        assert readiness.reason is None

    def test_not_applicable_when_no_driver_configured(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[_machine("laptop", ["api"])],
        )
        repo = cfg.repo("api")
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118, file_exists=lambda *a: True,
        )
        assert readiness.applies is False
        assert readiness.reason is None

    def test_not_applicable_when_gate_a_not_satisfied(self) -> None:
        """Gate A itself (contract.md missing) is a separate, already-
        surfaced refusal — issue_oracle_ready must not double-block with a
        confusing "no slice" message before the contract even exists."""
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118, file_exists=lambda *a: False,
        )
        assert readiness.applies is False
        assert readiness.reason is None

    def test_blocked_when_no_slice_authored(self) -> None:
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        fetch = _manifest_fetch({})  # no manifest at all
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.applies is True
        assert readiness.has_slice is False
        assert readiness.reason is not None
        assert "#1118" in readiness.reason
        assert "ms-37" in readiness.reason
        assert "coord acceptance author api <tracking_issue> --issue 1118" in readiness.reason

    def test_ok_when_slice_authored_and_kind_supported(self) -> None:
        cfg = _oracle_cfg(kind="cli-pytest")
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.yml": "tests:\n  ms37::a: 1118\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.applies is True
        assert readiness.has_slice is True
        assert readiness.unsupported_kinds == ()
        assert readiness.reason is None

    def test_ok_when_slice_authored_only_as_fragment(self) -> None:
        """#2543 review: a JIT slice authored under the new per-issue-
        fragment convention (`coord.test_author`'s briefing) writes ONLY
        `manifest.d/<issue>.yml`, never the shared `manifest.yml`. Before
        this fix `_fetch_manifest_data` only ever fetched the legacy
        single-file manifest, so this exact fixture would read
        `has_slice=False` forever and the #1138 hard gate would refuse Work
        dispatch even though the slice is fully authored and merged."""
        cfg = _oracle_cfg(kind="cli-pytest")
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.d/1118.yml": "tests:\n  ms37::a: 1118\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.applies is True
        assert readiness.has_slice is True
        assert readiness.unsupported_kinds == ()
        assert readiness.reason is None

    def test_fragment_for_another_issue_does_not_leak_has_slice(self) -> None:
        """A sibling issue's fragment (a different, differently-keyed file)
        must never make THIS issue read as having a slice — the whole
        point of #2543 is that fragments can never collide OR leak into
        each other."""
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.d/999.yml": "tests:\n  ms37::a: 999\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.has_slice is False
        assert readiness.reason is not None

    def test_slice_merges_legacy_manifest_and_fragment(self) -> None:
        """A milestone-level `exempt:` block still lives in the shared
        `manifest.yml` (#2543 keeps it there by choice) while THIS issue's
        slice lives in its own fragment — both must be visible together."""
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.yml": "exempt: [999]\n",
            "tests/acceptance/ms-37/manifest.d/1118.yml": "tests:\n  ms37::a: 1118\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.has_slice is True
        assert readiness.exempt is False
        assert readiness.reason is None

    def test_slice_mapped_to_different_issue_is_no_slice(self) -> None:
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.yml": "tests:\n  ms37::a: 999\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.has_slice is False
        assert readiness.reason is not None

    def test_exempt_via_manifest_list_bypasses_no_slice(self) -> None:
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.yml": "exempt: [1118]\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.applies is True
        assert readiness.exempt is True
        assert readiness.reason is None

    def test_exempt_via_label_bypasses_no_slice(self) -> None:
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        fetch = _manifest_fetch({})
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118, ["oracle:exempt"],
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.exempt is True
        assert readiness.reason is None

    def test_blocked_when_driver_kind_unsupported(self) -> None:
        # "native" is the one acceptance_drivers.py kind still undelivered
        # (docs/WEB_CONTROL_CENTER.md M-W0) — web-playwright landed in #1539
        # and is a real SUPPORTED_KINDS entry now, so it can no longer stand
        # in as "some unsupported kind" here.
        cfg = _oracle_cfg(kind="native")
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.yml": "tests:\n  ms37::a: 1118\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.has_slice is True
        assert readiness.unsupported_kinds == ("native",)
        assert readiness.reason is not None
        assert "native" in readiness.reason

    def test_exempt_bypasses_unsupported_kind_check(self) -> None:
        cfg = _oracle_cfg(kind="native")
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.yml": "exempt: [1118]\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.exempt is True
        assert readiness.reason is None

    def test_routed_driver_checks_every_route_kind(self) -> None:
        from coord.config import AcceptanceDriverConfig

        cfg = _oracle_cfg(routes=[
            AcceptanceDriverConfig(match="coord/**", kind="cli-pytest", run="pytest"),
            AcceptanceDriverConfig(match="tui/**", kind="tui-tuidriver", run="cargo test"),
        ])
        repo = cfg.repo("api")
        fetch = _manifest_fetch({
            "tests/acceptance/ms-37/manifest.yml": "tests:\n  ms37::a: 1118\n",
        })
        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(),
        )
        assert readiness.unsupported_kinds == ()
        assert readiness.reason is None

    def test_fetch_manifest_tries_yml_yaml_json_in_order(self) -> None:
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        calls: list[str] = []

        def _fetch(repo_github: str, path: str, branch: str) -> str | None:
            calls.append(path)
            if path.endswith(".json"):
                return '{"tests": {"a": 1118}}'
            if path.endswith("contract.md"):
                return CONTRACT
            return None

        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            file_exists=lambda *a: True, fetch_manifest=_fetch,
            fetch_gate_a_approval=_approval(),
        )
        # The legacy manifest probe order is unchanged; #2543 adds the
        # per-issue fragment probe (same yml/yaml/json order) right after
        # it, and the contract read (#2063) rides the same fetch seam.
        assert [c for c in calls if "manifest" in c] == [
            "tests/acceptance/ms-37/manifest.yml",
            "tests/acceptance/ms-37/manifest.yaml",
            "tests/acceptance/ms-37/manifest.json",
            "tests/acceptance/ms-37/manifest.d/1118.yml",
            "tests/acceptance/ms-37/manifest.d/1118.yaml",
            "tests/acceptance/ms-37/manifest.d/1118.json",
        ]
        assert "tests/acceptance/ms-37/contract.md" in calls
        assert readiness.has_slice is True

    def test_file_exists_and_contract_read_share_one_fetch_when_file_exists_not_overridden(
        self,
    ) -> None:
        """#2063 review: `gate_a_status`'s existence probe and the
        sign-off's content read both want `contract.md`. When the caller
        doesn't override `file_exists` (the real dispatch path —
        `coord.dispatch.enforce_oracle_readiness` calls this with neither
        override) they must share one memoised fetch, exactly like
        `gate_a_signoff_status` already does for its own pair of calls —
        otherwise every dispatch-readiness check against an oracle-opted
        milestone doubles its GitHub API cost for no reason."""
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        calls: list[str] = []

        def _fetch(repo_github: str, path: str, branch: str) -> str | None:
            calls.append(path)
            if path.endswith("manifest.yml"):
                return "tests:\n  ms37::a: 1118\n"
            if path.endswith("contract.md"):
                return CONTRACT
            return None

        readiness = issue_oracle_ready(
            repo, cfg, 37, 1118,
            fetch_manifest=_fetch,
            fetch_gate_a_approval=_approval(),
        )
        contract_calls = [c for c in calls if c.endswith("contract.md")]
        assert contract_calls == ["tests/acceptance/ms-37/contract.md"], (
            f"contract.md must be fetched exactly once, not {len(contract_calls)} "
            f"times: {calls}"
        )
        assert readiness.reason is None
        assert readiness.gate_a_state == "approved"

    def test_default_fetch_manifest_uses_github_ops(self) -> None:
        cfg = _oracle_cfg()
        repo = cfg.repo("api")
        with patch(
            "coord.github_ops.get_repo_file",
            return_value="tests:\n  ms37::a: 1118\n",
        ) as mock_fetch:
            readiness = issue_oracle_ready(
                repo, cfg, 37, 1118, file_exists=lambda *a: True,
            )
        assert readiness.has_slice is True
        mock_fetch.assert_any_call(
            "acme/api", "tests/acceptance/ms-37/manifest.yml", branch="main",
        )


class TestIssueOracleReadyRelocatedSlices:
    """#2896: ms-65's contract/manifest live under
    `tui/tests/acceptance/ms-65/` (the tui-tuidriver route's own sibling
    dir), not the shared repo-root tree — the #1138 hard gate must find
    them there, not report a false "no acceptance slice yet"."""

    def _cfg(self) -> Config:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        return Config(
            repos=[Repo(name="claude-coordinator", github="acme/claude-coordinator")],
            machines=[_machine("laptop", ["claude-coordinator"])],
            acceptance=AcceptanceConfig(drivers={
                "claude-coordinator": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(match="coord/**", kind="cli-pytest", run="pytest"),
                    AcceptanceDriverConfig(
                        match="tui/**", kind="tui-tuidriver", run="cargo test",
                        entrypoint="tui/tests/acceptance.rs",
                    ),
                ]),
            }),
        )

    def test_ok_when_slice_authored_at_the_relocated_path(self) -> None:
        cfg = self._cfg()
        repo = cfg.repo("claude-coordinator")
        mapping = {
            "tui/tests/acceptance/ms-65/contract.md": CONTRACT,
            "tui/tests/acceptance/ms-65/manifest.yml": "tests:\n  ms65::a: 2282\n",
        }

        def fetch(repo_github: str, path: str, branch: str) -> str | None:
            return mapping.get(path)

        readiness = issue_oracle_ready(
            repo, cfg, 65, 2282,
            fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(repo_name="claude-coordinator", milestone=65),
        )
        assert readiness.applies is True
        assert readiness.has_slice is True
        assert readiness.reason is None

    def test_blocked_when_no_slice_at_either_root(self) -> None:
        """Gate A is satisfied (contract exists at the relocated path) but
        no manifest maps this issue anywhere — the #1138 refusal must still
        fire, same as the un-relocated case."""
        cfg = self._cfg()
        repo = cfg.repo("claude-coordinator")
        mapping = {"tui/tests/acceptance/ms-65/contract.md": CONTRACT}

        def fetch(repo_github: str, path: str, branch: str) -> str | None:
            return mapping.get(path)

        readiness = issue_oracle_ready(
            repo, cfg, 65, 2282,
            fetch_manifest=fetch,
            fetch_gate_a_approval=_approval(repo_name="claude-coordinator", milestone=65),
        )
        assert readiness.applies is True
        assert readiness.has_slice is False
        assert readiness.reason is not None


# ── fetch_milestone_context ──────────────────────────────────────────────────


TRACKING_BODY = """\
## Work order
- [ ] #762  {group: A}
- [ ] #763  {group: A}
- [ ] #765  {after: #762,#763}
"""


class TestFetchMilestoneContext:
    def test_raises_on_fetch_failure(self) -> None:
        repo = Repo(name="api", github="acme/api")
        with patch("coord.github_ops.get_issue", side_effect=RuntimeError("boom")):
            with pytest.raises(MilestoneDispatchError, match="could not fetch #100"):
                fetch_milestone_context(repo, 100)

    def test_raises_when_no_milestone(self) -> None:
        repo = Repo(name="api", github="acme/api")
        with patch(
            "coord.github_ops.get_issue",
            return_value={"number": 100, "body": TRACKING_BODY, "milestone": None},
        ):
            with pytest.raises(MilestoneDispatchError, match="no milestone"):
                fetch_milestone_context(repo, 100)

    def test_empty_work_order_short_circuits(self) -> None:
        repo = Repo(name="api", github="acme/api")
        with patch(
            "coord.github_ops.get_issue",
            return_value={"number": 100, "body": "no block here", "milestone": {"number": 9}},
        ):
            ctx = fetch_milestone_context(repo, 100)
        assert ctx.work_order.nodes == ()
        assert ctx.terminal_issues == frozenset()

    def test_resolves_terminal_issues_and_membership(self) -> None:
        repo = Repo(name="api", github="acme/api")

        def get_issue(_repo, number):
            if number == 100:
                return {
                    "number": 100, "body": TRACKING_BODY,
                    "milestone": {"number": 9},
                }
            state = "CLOSED" if number in (762, 763) else "OPEN"
            return {"number": number, "state": state, "milestone": {"number": 9}}

        open_issues = [{"number": 765, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues):
            ctx = fetch_milestone_context(repo, 100)

        assert ctx.milestone_number == 9
        assert ctx.terminal_issues == frozenset({762, 763})


# ── dispatch_entry ────────────────────────────────────────────────────────────


class TestDispatchEntry:
    def _pick(self, cfg: Config, board: Board, issue: int = 762):
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues=set())
        return next(p for p in plan.to_dispatch if p.entry.issue_number == issue)

    def test_successful_dispatch_records_and_marks_board_busy(self, coord_db) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        with patch("coord.github_ops.get_issue", return_value={"title": "Fix X", "body": "b", "labels": []}), \
             patch("coord.dispatch.dispatch", return_value={"id": "asn-1"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is True
        assert outcome.assignment_id == "asn-1"
        assert outcome.machine_name == "laptop"

        # Board mutated in place so a subsequent plan_dispatch call in the same
        # batch/tick sees "laptop" as busy.
        assert any(a.assignment_id == "asn-1" and a.status == "running" for a in board.active)
        assert pick_machine("api", board, cfg) is None

    def test_opencode_provider_definition_model_wins_over_models_default(
        self, coord_db,
    ) -> None:
        """#1706 review fix: `coord milestone dispatch` — like `coord
        assign`/`coord approve` — must not force `models.default` (a
        Claude alias) onto a repo routed through a non-claude/claude-pty
        provider that pins its own `model`. Milestone dispatch has no
        per-call --model override, so this is the only lever an operator
        has to pick a non-default model for it."""
        cfg = _config(
            [_machine("laptop", ["api"])],
            repos=[Repo(name="api", github="acme/api", provider="opencode")],
        )
        cfg.providers = ProvidersConfig(
            default="claude",
            definitions={
                "claude": ProviderDef(type="claude"),
                "opencode": ProviderDef(type="opencode", model="zhipuai/glm-4.6"),
            },
        )
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        proposals = []

        def fake_dispatch(proposal, config):
            proposals.append(proposal)
            return {"id": "asn-oc-1"}

        with patch("coord.github_ops.get_issue", return_value={"title": "Fix X", "body": "b", "labels": []}), \
             patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is True
        assert len(proposals) == 1
        assert proposals[0].model is None, (
            "expected proposal.model=None so OpenCodeProvider's own "
            f"definition.model wins; got {proposals[0].model!r}"
        )
        assert outcome.model_reason is not None
        assert "zhipuai/glm-4.6" in outcome.model_reason

    def test_opencode_pin_wins_over_matched_label_and_names_it(
        self, coord_db,
    ) -> None:
        """#1798 review fix: when a tier label DOES match (unlike the sibling
        test above, which has none) but the effective provider still pins
        its own model, the pin must keep winning (namespace-mismatched
        Claude alias vs. the provider's own model) — and `outcome.
        model_reason` must name the label that lost, mirroring
        `_dispatch_headless`'s equivalent branch in dispatch_workers.py, so
        `coord milestone dispatch`'s output doesn't silently omit it."""
        cfg = _config(
            [_machine("laptop", ["api"])],
            repos=[Repo(name="api", github="acme/api", provider="opencode")],
        )
        cfg.models = ModelsConfig(default="sonnet", labels={"tier:large": "opus"})
        cfg.providers = ProvidersConfig(
            default="claude",
            definitions={
                "claude": ProviderDef(type="claude"),
                "opencode": ProviderDef(type="opencode", model="zhipuai/glm-4.6"),
            },
        )
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        proposals = []

        def fake_dispatch(proposal, config):
            proposals.append(proposal)
            return {"id": "asn-oc-2"}

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Fix X", "body": "b", "labels": [{"name": "tier:large"}]},
        ), \
             patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is True
        assert len(proposals) == 1
        assert proposals[0].model is None, (
            "expected proposal.model=None so OpenCodeProvider's own "
            f"definition.model wins over the matched 'tier:large' label; "
            f"got {proposals[0].model!r}"
        )
        assert outcome.model_reason is not None
        assert "zhipuai/glm-4.6" in outcome.model_reason
        assert "overriding label 'tier:large'" in outcome.model_reason

    def test_providers_labels_routes_the_dispatch_and_names_it(self, coord_db) -> None:
        """#1889: `coord milestone dispatch` is a headless path (also the
        daemon's auto-drain tick) with no `--provider` flag to type — a
        `harness:opencode`-labelled issue must still route to the labelled
        provider, and `outcome.provider_reason` must name the label so the
        CLI/auto-drain log is self-explaining."""
        cfg = _config(
            [_machine("laptop", ["api"])],
            repos=[Repo(name="api", github="acme/api")],  # no repo.provider
        )
        cfg.providers = ProvidersConfig(
            default="claude",
            definitions={
                "claude": ProviderDef(type="claude"),
                "fast-claude": ProviderDef(type="claude"),
            },
            labels={"harness:opencode": "fast-claude"},
        )
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        proposals = []

        def fake_dispatch(proposal, config):
            proposals.append(proposal)
            return {"id": "asn-label-1"}

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Fix X", "body": "b", "labels": [{"name": "harness:opencode"}]},
        ), \
             patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is True
        assert len(proposals) == 1
        assert outcome.provider_reason is not None
        assert outcome.provider_reason == "fast-claude (via label 'harness:opencode')"

    def test_claimed_issue_is_not_dispatched(self, coord_db) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board(active=[_running("server", 762)])  # already claimed elsewhere
        pick = self._pick(cfg, Board())  # plan against an unclaimed board...
        repo = cfg.repo("api")

        with patch("coord.dispatch.dispatch") as disp:
            # ...but dispatch_entry re-checks the LIVE board defensively.
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is False
        assert "already claimed" in outcome.error
        disp.assert_not_called()

    def test_opted_in_repo_ensures_feature_branch_and_threads_milestone_number(
        self, coord_db,
    ) -> None:
        """#934: a repo with develop_branch set + an issue that belongs to a
        milestone should call branch_model.ensure_feature_branch_exists and
        thread milestone_number onto the Proposal — the actual integration
        point the review flagged as untested (only dispatch()'s own base-
        branch resolution had coverage, not dispatch_entry's derivation of
        milestone_number from the fetched issue or its call to
        ensure_feature_branch_exists)."""
        cfg = _config([_machine("laptop", ["api"])], repos=[
            Repo(name="api", github="acme/api", develop_branch="develop"),
        ])
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        proposals = []

        def fake_dispatch(proposal, config):
            proposals.append(proposal)
            return {"id": "asn-1"}

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Fix X", "body": "b", "labels": [],
                          "milestone": {"number": 9, "title": "M9"}},
        ), \
             patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.branch_model.ensure_feature_branch_exists",
                   return_value="feature/ms-9") as ensure_mock:
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is True
        ensure_mock.assert_called_once_with(repo, 9)
        assert len(proposals) == 1
        assert proposals[0].milestone_number == 9

    def test_feature_branch_creation_failure_fails_dispatch_loudly(
        self, coord_db,
    ) -> None:
        """#934 review finding #1: ensure_feature_branch_exists raising
        RuntimeError (e.g. the remote branch-create call failed) must fail
        dispatch_entry with a clear error at dispatch time, not silently
        proceed to dispatch a worker whose branch payload names a ref that
        doesn't exist on the remote."""
        cfg = _config([_machine("laptop", ["api"])], repos=[
            Repo(name="api", github="acme/api", develop_branch="develop"),
        ])
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Fix X", "body": "b", "labels": [],
                          "milestone": {"number": 9, "title": "M9"}},
        ), \
             patch("coord.dispatch.dispatch") as disp, \
             patch("coord.branch_model.ensure_feature_branch_exists",
                   side_effect=RuntimeError("failed to create feature branch")):
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is False
        assert "could not ensure feature/ms-9 exists" in outcome.error
        disp.assert_not_called()

    def test_tier_label_resolves_model_for_work_dispatch(self, coord_db) -> None:
        """#1430: dispatch_entry resolves models.labels for a plain "work"
        dispatch (require_plan=false, the default in _config())."""
        cfg = _config([_machine("laptop", ["api"])])
        cfg.models = ModelsConfig(default="sonnet", labels={"tier:large": "opus"})
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        proposals = []

        def fake_dispatch(proposal, config):
            proposals.append(proposal)
            return {"id": "asn-1"}

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Fix X", "body": "b", "labels": [{"name": "tier:large"}]},
        ), \
             patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is True
        assert len(proposals) == 1
        assert proposals[0].model == "opus"
        assert proposals[0].type == "work"
        # #1454: the outcome states *why* opus was picked.
        assert outcome.model == "opus"
        assert "via label 'tier:large'" in outcome.model_reason

    def test_require_plan_does_not_inherit_label_model(self, coord_db) -> None:
        """#1430: when dispatch.require_plan upgrades this to a plan-type
        dispatch, it must stay on models.default, not inherit tier:large."""
        from coord.config import DispatchConfig

        cfg = _config([_machine("laptop", ["api"])])
        cfg.models = ModelsConfig(default="sonnet", labels={"tier:large": "opus"})
        cfg.dispatch = DispatchConfig(require_plan=True)
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        proposals = []

        def fake_dispatch(proposal, config):
            proposals.append(proposal)
            return {"id": "asn-1"}

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Fix X", "body": "b", "labels": [{"name": "tier:large"}]},
        ), \
             patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is True
        assert len(proposals) == 1
        assert proposals[0].model == "sonnet"
        assert proposals[0].type == "plan"
