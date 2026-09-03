"""Tests for coord/drive.py — the `coord drive` state machine (#1392).

Every bug ``scripts/drive-issue.sh`` shipped was in decision logic, not in
subprocess orchestration:

- an ``advisory`` work row fell through to a bare ``sleep; continue`` — a silent
  240-minute spin (PR #1386)
- merge verification used ``merge-base --is-ancestor``, which is **always**
  wrong under ``coord merge --method rebase``
- unbounded merge retries until the deadline
- interactive work parked on a review that was never coming (#555)

In bash those were untestable. Here they are :func:`coord.drive.decide` /
:func:`coord.drive.preflight` calls, and this file is the "behaviour that must
survive the port" list from #1392, one test per item.

The other invariant under test is the CLI boundary: every board mutation must
go out as a ``coord`` subcommand argv, never a direct internal call. Calling
``record_test_verdict()`` instead of ``coord test --passed`` silently
reintroduces #1384 (the CLI mirrors ``test_state`` → the legacy ``smoke_test``
field; the function alone does not), which makes ``coord fix`` refuse to
dispatch. So the assertions below are on ``action.command``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coord.config import (
    Config,
    PipelineConfig,
    ProviderDef,
    ProvidersConfig,
    UsageGateConfig,
)
from coord.drive import (
    EXIT_DEAD_END,
    EXIT_DEADLINE,
    EXIT_DISPATCH_REFUSED,
    EXIT_ESCALATED,
    EXIT_OK,
    EXIT_SELF_STALE,
    EXIT_TERMINAL_FAILURE,
    EXIT_USAGE,
    RUN,
    WAIT,
    Action,
    DriveCounters,
    DriveError,
    DriveOptions,
    Driver,
    FileLock,
    GitHubAcceptanceGateChecker,
    GitMergeVerifier,
    LockBusy,
    OracleDecision,
    _die,
    _remote_matches_repo,
    coord_argv,
    decide,
    preflight,
    resolve_oracle_decision,
)
from coord.drive_state import IssueState
from coord.failure_class import environmental_backoff_secs
from coord.models import POLICY_REFUSAL_MARKER, Machine, Repo
from coord.usage_limits import PlanLimits


REPO = "claude-coordinator"
ISSUE = 1392


def make_config() -> Config:
    return Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator", test_command="pytest -q")],
        machines=[Machine(name="precision", host="precision", repos=[REPO])],
    )


def make_config_no_stall_overrides() -> Config:
    """#2649: ``make_config()`` with ``PipelineConfig.stall_thresholds``
    cleared. The generic re-nudge/monotonicity/fingerprint-clear tests below
    exercise the FLAT ``--stall`` mechanism in isolation using tiny
    fake-clock ``stall_mins`` values and ``board()``'s default
    ``type="work"`` — without this, the built-in ``work`` override
    (comfortably larger, by design, than anything those tests configure)
    would silently take over and the flat value they set would never
    govern anything."""
    cfg = make_config()
    cfg.pipeline.stall_thresholds = {}
    return cfg


def state(**kw) -> IssueState:
    base = dict(repo=REPO, issue=ISSUE, repo_github="john/claude-coordinator")
    base.update(kw)
    return IssueState(**base)


class FakeVerifier:
    """Stands in for git/gh so decision tests never touch the network."""

    def __init__(
        self,
        *,
        has_commits: bool | None = True,
        merged: bool = True,
        head_sha: str | None = "deadbeef" * 5,
    ) -> None:
        self._has_commits = has_commits
        self._merged = merged
        self._head_sha = head_sha
        self.commits_calls = 0
        self.merged_calls = 0
        self.head_sha_calls = 0

    def branch_has_commits(self, s: IssueState) -> bool | None:
        self.commits_calls += 1
        return self._has_commits

    def verify_merged(self, s: IssueState) -> bool:
        self.merged_calls += 1
        return self._merged

    def branch_head_sha(self, s: IssueState) -> str | None:
        self.head_sha_calls += 1
        return self._head_sha


def step(s: IssueState, opts: DriveOptions | None = None, **kw) -> Action:
    """One decide() call with sensible defaults."""
    verifier = kw.pop("verifier", None) or FakeVerifier()
    counters = kw.pop("counters", None) or DriveCounters()
    # A default gate_checker whose resolve_for_path() is a no-op (None, "no
    # --for-path needed") — every pre-#1453-review test drives an unrouted
    # (or no) acceptance config, so this preserves their assertions
    # byte-for-byte; oracle tests that care override it explicitly.
    gate_checker = kw.pop("gate_checker", None) or FakeGateChecker()
    return decide(
        s,
        opts or DriveOptions(machine="precision"),
        counters,
        verifier,
        machine=kw.pop("machine", "precision"),
        oracle=kw.pop("oracle", None),
        gate_checker=gate_checker,
    )


# ═══════════════════════════════════════════════════════════════════════════
# preflight
# ═══════════════════════════════════════════════════════════════════════════


def test_preflight_resolves_the_least_loaded_machine_when_none_given():
    pre = preflight(state(picked_machine="dellserver"), DriveOptions())
    assert pre.machine == "dellserver"


def test_preflight_prefers_an_explicit_machine():
    pre = preflight(
        state(picked_machine="dellserver"), DriveOptions(machine="precision")
    )
    assert pre.machine == "precision"


def test_preflight_refuses_when_no_machine_hosts_the_repo():
    with pytest.raises(DriveError) as exc:
        preflight(state(), DriveOptions())
    assert "no unpaused machine hosts" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE


def test_preflight_refuses_distinctly_when_hosts_exist_but_none_are_capable():
    """#1906: a fleet that DOES host the repo but has no machine advertising
    the resolved provider must not collapse into the generic 'no unpaused
    machine hosts' message — the two are different problems (add a machine
    vs. add a capability) with different fixes."""
    with pytest.raises(DriveError) as exc:
        preflight(
            state(picked_machine_no_capable=True, picked_machine_provider="opencode"),
            DriveOptions(),
        )
    message = str(exc.value)
    assert "no unpaused machine advertises" in message
    assert "opencode" in message
    assert "no unpaused machine hosts" not in message
    assert exc.value.exit_code == EXIT_USAGE


def test_preflight_explicit_machine_wins_even_when_selection_found_no_capable_host():
    """Explicit beats inferred (#1906 design point): an operator naming a
    machine is never silently re-routed or refused by THIS gate — #1711's
    dispatch-time guard is the one that gets to refuse an explicit but
    incapable machine, not the picker."""
    pre = preflight(
        state(picked_machine_no_capable=True, picked_machine_provider="opencode"),
        DriveOptions(machine="precision"),
    )
    assert pre.machine == "precision"


def test_preflight_warns_when_the_auto_loop_is_off():
    pre = preflight(state(picked_machine="m", auto_loop=False), DriveOptions())
    assert any("auto_loop is OFF" in w for w in pre.warnings)


# ── #555: interactive work is refused at PREFLIGHT, not at the review gate ──


def test_interactive_work_is_refused_at_preflight_not_after_the_test_suite():
    """#555 + the #1357 drive: a run must not burn ~6min of tests then park.

    `dispatch_pending_reviews` carries `provider_name != "claude-pty"`, so for
    interactive work the review is not late — it is never coming.
    """
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        work_status="done",
        work_branch="b",
    )
    with pytest.raises(DriveError) as exc:
        preflight(s, DriveOptions())
    assert "INTERACTIVELY" in str(exc.value)
    assert "--force-review" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE


def test_force_review_turns_the_preflight_refusal_into_a_warning():
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        work_status="done",
    )
    pre = preflight(s, DriveOptions(force_review=True))
    assert any("--force-review set" in w for w in pre.warnings)


def test_preflight_allows_interactive_work_that_already_has_a_review():
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        review_aid="r1",
    )
    assert preflight(s, DriveOptions()).machine == "m"


def test_preflight_allows_headless_work():
    s = state(picked_machine="m", work_aid="w1", work_provider="claude-code")
    assert preflight(s, DriveOptions()).machine == "m"


# ── #1466: the Max-plan 5h/weekly usage gate ────────────────────────────────
#
# preflight() stays pure — it never probes itself. A black-box test drives
# it with a stubbed PlanLimits exactly like MergeVerifier/AcceptanceGate-
# Checker are stubbed elsewhere in this file.


def _config_with_gate(**gate_kw) -> Config:
    cfg = make_config()
    cfg.usage_gate = UsageGateConfig(**gate_kw)
    return cfg


def test_preflight_with_no_config_skips_the_gate_entirely():
    """Every pre-#1466 call site (and most of this file's own tests) passes
    no config at all — must behave exactly as before, gate or no gate."""
    pre = preflight(
        state(picked_machine="m"), DriveOptions(),
        usage_limits=PlanLimits(status="ok", session_pct=99.0, week_pct=99.0),
    )
    assert pre.machine == "m"
    assert pre.warnings == ()


def test_preflight_gate_disabled_mode_ignores_a_maxed_out_probe():
    cfg = _config_with_gate(mode="disabled", session_threshold_pct=1.0)
    pre = preflight(
        state(picked_machine="m"), DriveOptions(), cfg,
        usage_limits=PlanLimits(status="ok", session_pct=99.0),
    )
    assert pre.warnings == ()


def test_preflight_gate_below_threshold_proceeds_with_no_warning():
    cfg = _config_with_gate(mode="warn", session_threshold_pct=85.0, week_threshold_pct=90.0)
    pre = preflight(
        state(picked_machine="m"), DriveOptions(), cfg,
        usage_limits=PlanLimits(status="ok", session_pct=10.0, week_pct=10.0),
    )
    assert pre.warnings == ()


def test_preflight_gate_above_threshold_warns_by_default_and_still_proceeds():
    cfg = _config_with_gate(mode="warn", session_threshold_pct=85.0)
    pre = preflight(
        state(picked_machine="m"), DriveOptions(), cfg,
        usage_limits=PlanLimits(status="ok", session_pct=90.0, session_resets_at="8pm (UTC)"),
    )
    assert pre.machine == "m"
    assert any("90" in w and "8pm (UTC)" in w for w in pre.warnings)


def test_preflight_gate_block_mode_refuses_above_threshold():
    cfg = _config_with_gate(mode="block", week_threshold_pct=90.0)
    with pytest.raises(DriveError) as exc:
        preflight(
            state(picked_machine="m"), DriveOptions(), cfg,
            usage_limits=PlanLimits(status="ok", week_pct=95.0, week_resets_at="Aug 1"),
        )
    assert "week" in str(exc.value)
    assert "Aug 1" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE


def test_preflight_gate_unavailable_probe_proceeds_even_in_block_mode():
    """A probe we can't trust must never block (or warn) a dispatch — see
    coord.usage_limits.evaluate_usage_gate's docstring."""
    cfg = _config_with_gate(mode="block", session_threshold_pct=1.0, week_threshold_pct=1.0)
    pre = preflight(
        state(picked_machine="m"), DriveOptions(), cfg,
        usage_limits=PlanLimits(status="unknown", error="claude -p /usage timed out"),
    )
    assert pre.machine == "m"
    assert pre.warnings == ()


def test_preflight_gate_no_usage_limits_passed_is_treated_as_unavailable():
    """config given but usage_limits omitted (e.g. a caller that skipped the
    probe) — never fabricate an "ok" reading."""
    cfg = _config_with_gate(mode="block", session_threshold_pct=1.0)
    pre = preflight(state(picked_machine="m"), DriveOptions(), cfg)
    assert pre.machine == "m"
    assert pre.warnings == ()


# ── Driver._loop wiring: the probe is consulted end-to-end ──────────────────


def test_driver_loop_surfaces_a_usage_gate_warning(driver_factory, capsys):
    cfg = _config_with_gate(mode="warn", session_threshold_pct=50.0)
    driver = driver_factory(
        [board(status="merged")],
        config=cfg,
        usage_prober=lambda: PlanLimits(status="ok", session_pct=95.0, session_resets_at="8pm"),
    )
    assert driver.run() == EXIT_OK
    assert "Max-plan usage near limit" in capsys.readouterr().err


def test_driver_loop_block_mode_refuses_before_dispatching(driver_factory, capsys):
    cfg = _config_with_gate(mode="block", session_threshold_pct=50.0)
    driver = driver_factory(
        [board(status="merged")],
        config=cfg,
        usage_prober=lambda: PlanLimits(status="ok", session_pct=95.0),
    )
    # DriveError propagates out of run() unhandled (the CLI boundary in
    # coord/commands/drive.py converts it to an exit code) — same contract
    # every other preflight refusal in this file already uses.
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert "Max-plan usage near limit" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE
    assert not driver.recorded  # never got as far as running a `coord` subcommand


def test_driver_loop_disabled_gate_never_calls_the_prober(driver_factory):
    cfg = _config_with_gate(mode="disabled")
    calls = []

    def prober():
        calls.append(1)
        return PlanLimits(status="ok", session_pct=99.0)

    driver = driver_factory([board(status="merged")], config=cfg, usage_prober=prober)
    assert driver.run() == EXIT_OK
    assert calls == []


# ═══════════════════════════════════════════════════════════════════════════
# no work yet: plan / dispatch
# ═══════════════════════════════════════════════════════════════════════════


def test_no_work_row_dispatches_work_through_the_cli():
    action = step(state())
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_dispatch_work_passes_model_and_briefing_file():
    opts = DriveOptions(machine="precision", model="opus", briefing_file="/tmp/b.md")
    action = step(state(), opts)
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
        "--model", "opus",
        "--briefing-file", "/tmp/b.md",
    )


def test_plan_flag_dispatches_a_plan_only_assignment_first():
    action = step(state(), DriveOptions(machine="precision", do_plan=True))
    assert action.command == (
        "assign", "--plan-only", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_a_done_plan_is_auto_approved():
    action = step(
        state(plan_aid="p1", plan_status="done"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.command == ("approve-plan", "p1")


def test_a_failed_plan_is_terminal():
    action = step(
        state(plan_aid="p1", plan_status="failed"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "plan assignment p1 failed" in action.message


def test_a_running_plan_just_waits():
    action = step(
        state(plan_aid="p1", plan_status="running"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.kind == WAIT


def test_anything_active_just_waits():
    action = step(state(active_count=1, active_types=("smoke",)))
    assert action.kind == WAIT


# ═══════════════════════════════════════════════════════════════════════════
# #1453: the oracle-loop JIT slice gate
# ═══════════════════════════════════════════════════════════════════════════


def make_config_with_acceptance_driver() -> Config:
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig

    return Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator", test_command="pytest -q")],
        machines=[Machine(name="precision", host="precision", repos=[REPO])],
        acceptance=AcceptanceConfig(
            drivers={REPO: AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
        ),
    )


class FakeGateChecker:
    def __init__(
        self,
        *,
        exists: bool = True,
        for_path: str | None = None,
        for_path_error: Exception | None = None,
        exempt: bool = False,
        landed: bool = False,
    ) -> None:
        self._exists = exists
        self._for_path = for_path
        self._for_path_error = for_path_error
        self._exempt = exempt
        self._landed = landed
        self.calls: list[tuple[str, int]] = []
        self.for_path_calls: list[tuple[str, int]] = []
        self.exempt_calls: list[tuple[str, int, int, tuple[str, ...]]] = []
        self.landed_calls: list[tuple[str, int, int]] = []

    def contract_exists(self, repo_name: str, milestone_number: int) -> bool:
        self.calls.append((repo_name, milestone_number))
        return self._exists

    def resolve_for_path(self, repo_name: str, milestone_number: int) -> str | None:
        self.for_path_calls.append((repo_name, milestone_number))
        if self._for_path_error is not None:
            raise self._for_path_error
        return self._for_path

    def is_issue_exempt(
        self,
        repo_name: str,
        milestone_number: int,
        issue_number: int,
        issue_labels: tuple[str, ...],
    ) -> bool:
        self.exempt_calls.append((repo_name, milestone_number, issue_number, issue_labels))
        return self._exempt

    def has_authored_slice(
        self, repo_name: str, milestone_number: int, issue_number: int,
    ) -> bool:
        self.landed_calls.append((repo_name, milestone_number, issue_number))
        return self._landed


def oracle_state(**kw) -> IssueState:
    base = dict(milestone_number=38, milestone_tracking_issue=1120)
    base.update(kw)
    return state(**base)


# ── resolve_oracle_decision ──────────────────────────────────────────────────


def test_resolve_oracle_decision_is_inactive_without_no_acceptance_flag_by_default():
    """No acceptance driver configured at all -> normal drive, no GitHub call."""
    checker = FakeGateChecker()
    decision = resolve_oracle_decision(
        oracle_state(), DriveOptions(), make_config(), checker
    )
    assert decision.active is False
    assert "no acceptance.drivers entry" in decision.reason
    assert checker.calls == []


def test_resolve_oracle_decision_respects_no_acceptance_opt_out():
    checker = FakeGateChecker()
    decision = resolve_oracle_decision(
        oracle_state(),
        DriveOptions(no_acceptance=True),
        make_config_with_acceptance_driver(),
        checker,
    )
    assert decision.active is False
    assert "--no-acceptance" in decision.reason
    assert checker.calls == []


def test_resolve_oracle_decision_is_inactive_with_no_milestone():
    decision = resolve_oracle_decision(
        oracle_state(milestone_number=None),
        DriveOptions(),
        make_config_with_acceptance_driver(),
        FakeGateChecker(),
    )
    assert decision.active is False
    assert "no GitHub milestone" in decision.reason


def test_resolve_oracle_decision_is_inactive_with_no_tracking_issue():
    decision = resolve_oracle_decision(
        oracle_state(milestone_tracking_issue=None),
        DriveOptions(),
        make_config_with_acceptance_driver(),
        FakeGateChecker(),
    )
    assert decision.active is False
    assert "tracked milestone work order" in decision.reason


def test_resolve_oracle_decision_is_inactive_when_the_contract_is_not_merged_yet():
    checker = FakeGateChecker(exists=False)
    decision = resolve_oracle_decision(
        oracle_state(), DriveOptions(), make_config_with_acceptance_driver(), checker
    )
    assert decision.active is False
    assert "ms-38/contract.md" in decision.reason
    assert checker.calls == [(REPO, 38)]


def test_resolve_oracle_decision_is_active_when_everything_lines_up():
    checker = FakeGateChecker(exists=True)
    decision = resolve_oracle_decision(
        oracle_state(), DriveOptions(), make_config_with_acceptance_driver(), checker
    )
    assert decision.active is True
    assert decision.tracking_issue == 1120
    assert "ms-38" in decision.reason


def test_resolve_oracle_decision_resolves_issue_exempt_when_active():
    """#2199: `issue_exempt` is resolved alongside `active` — one GitHub
    fetch budget, not a second one paid later by the trust gate."""
    checker = FakeGateChecker(exists=True, exempt=True)
    decision = resolve_oracle_decision(
        oracle_state(issue_labels=("bug",)),
        DriveOptions(),
        make_config_with_acceptance_driver(),
        checker,
    )
    assert decision.active is True
    assert decision.issue_exempt is True
    assert checker.exempt_calls == [(REPO, 38, ISSUE, ("bug",))]


def test_resolve_oracle_decision_issue_exempt_defaults_false_when_not_exempt():
    checker = FakeGateChecker(exists=True, exempt=False)
    decision = resolve_oracle_decision(
        oracle_state(), DriveOptions(), make_config_with_acceptance_driver(), checker
    )
    assert decision.active is True
    assert decision.issue_exempt is False


def test_resolve_oracle_decision_never_resolves_exempt_when_inactive():
    """No point paying the fetch when the trust gate wouldn't consult it
    anyway — every inactive branch returns before `is_issue_exempt` runs."""
    checker = FakeGateChecker(exists=False)
    decision = resolve_oracle_decision(
        oracle_state(), DriveOptions(), make_config_with_acceptance_driver(), checker
    )
    assert decision.active is False
    assert decision.issue_exempt is False
    assert checker.exempt_calls == []


def test_the_default_gate_checker_is_issue_exempt_reuses_the_1138_hard_gate():
    """#2199: must not re-derive `manifest.exempt`/`oracle:exempt` — reuse
    `coord.milestone_dispatch.issue_oracle_ready` (the #1138 hard gate)
    exactly, so the JIT-authoring gate and the trust gate can never
    disagree about which issues the oracle loop covers."""
    import inspect

    src = inspect.getsource(GitHubAcceptanceGateChecker.is_issue_exempt)
    assert "issue_oracle_ready" in src


def test_the_default_gate_checker_has_authored_slice_reuses_the_1138_hard_gate():
    """#2061: "has the slice landed?" must be answered from the SAME
    manifest-vs-default-branch read the #1138 hard gate performs
    (`issue_oracle_ready`'s `has_slice`), not a fresh re-derivation that
    could drift from it."""
    import inspect

    src = inspect.getsource(GitHubAcceptanceGateChecker.has_authored_slice)
    assert "issue_oracle_ready" in src


def test_the_default_gate_checker_reuses_gate_a_status_not_a_reimplementation():
    """#1453: must not drift from tui's gate_a_contract_exists_for /
    coord.milestone_dispatch.gate_a_status — both keyed on
    coord.acceptance.gate_a_contract_path.
    """
    import inspect

    src = inspect.getsource(GitHubAcceptanceGateChecker.contract_exists)
    assert "gate_a_status" in src


def test_the_default_gate_checker_delegates_for_path_to_the_shared_helper():
    """#1453 review finding 1: GitHubAcceptanceGateChecker.resolve_for_path
    must call coord.acceptance.resolve_for_path (the ONE shared derivation)
    rather than re-deriving --for-path itself."""
    import inspect

    src = inspect.getsource(GitHubAcceptanceGateChecker.resolve_for_path)
    assert "resolve_for_path(" in src


def test_the_default_gate_checker_resolve_for_path_is_wired_end_to_end(monkeypatch):
    """Exercises the real resolve_for_path() call through the checker with a
    stubbed mock-lister, rather than trusting the source-scan above alone."""
    calls = []

    def fake_list_repo_dir(repo: str, path: str, branch: str = "develop") -> list[str]:
        calls.append((repo, path, branch))
        return ["plans-base.screen"]

    monkeypatch.setattr("coord.github_ops.list_repo_dir", fake_list_repo_dir)

    from coord.config import AcceptanceConfig, AcceptanceDriverConfig

    cfg = Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator")],
        machines=[],
        acceptance=AcceptanceConfig(
            drivers={
                REPO: AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(match="coord/**", kind="cli-pytest", run="pytest"),
                    AcceptanceDriverConfig(match="tui/**", kind="tui-tuidriver", run="cargo test"),
                ])
            }
        ),
    )
    checker = GitHubAcceptanceGateChecker(config=cfg)
    assert checker.resolve_for_path(REPO, 38) == "tui/**"
    assert calls == [
        ("john/claude-coordinator", "tests/acceptance/ms-38/mocks", "main"),
    ]


def test_the_default_gate_checker_resolve_for_path_returns_none_for_unknown_repo():
    checker = GitHubAcceptanceGateChecker(config=make_config())
    assert checker.resolve_for_path("no-such-repo", 38) is None


def test_gate_a_contract_path_agrees_across_python_dispatch_and_drive():
    """#1453's acceptance bar: "the gate matches the TUI's and Python's,
    with a test asserting the implementations agree."

    Three independent call sites decide whether a milestone's Gate-A
    contract exists:

    - coord-tui's ``src/app/pipeline.rs::gate_a_contract_exists_for`` — a
      local-fs check for the interactive JIT-author menu item (#1060).
    - ``coord.milestone_dispatch.gate_a_status`` — the #930 milestone-
      dispatch gate (GitHub-fetch based); also what #1453's
      ``GitHubAcceptanceGateChecker`` reuses (previous test).
    - ``coord.drive.resolve_oracle_decision`` (#1453, this issue) — the
      unattended driver's pre-work JIT-author gate.

    All three MUST derive the path from the ``tests/acceptance/ms-NN/
    contract.md`` convention — ``coord.acceptance.gate_a_contract_path`` on
    the Python side — rather than re-deriving their own. A drifted format is
    silent: the driver would wait forever for a contract that actually
    exists at a slightly different path.

    #2899 DROPPED THE RUST THIRD OF THIS TEST. It read
    ``tui/src/app/pipeline.rs`` as source text and regex-extracted its
    ``.join()`` segments; that file is not in this checkout any more, so the
    assertion is not weakened here — it is structurally impossible here. A
    single-checkout test cannot pin two repos. Re-establishing it is a
    CROSS-REPO drift net, which #2894 files as its own story precisely so a
    red there does not block this move; the natural home is coord-tui's CI,
    which already installs ``code-coordinator`` from PyPI for the
    ``generated.rs`` gate and can therefore import
    ``coord.acceptance.gate_a_contract_path`` and compare against its own
    ``pipeline.rs`` — the same direction the codegen and board-fixture gates
    now run.

    What survives here is the Python pair, which is what this checkout owns.

    #2896: ``gate_a_status``/``resolve_oracle_decision`` no longer call
    ``gate_a_contract_path`` directly — they go through
    ``coord.acceptance.gate_a_contract_candidates`` instead, since a bare
    milestone number can now resolve to more than one candidate path (the
    shared repo-root tree, or an entrypoint-linked driver's own relocated
    sibling dir). ``gate_a_contract_candidates`` is itself built on
    ``gate_a_contract_path`` (single source of truth preserved, just one
    layer removed), and for a repo with no entrypoint-linked driver — the
    Rust TUI side's own ``coordinator.yml`` shape isn't exercised here — it
    always resolves to exactly the same single legacy path, so the drift
    check below still holds by construction.
    """
    from coord.acceptance import gate_a_contract_path

    path = gate_a_contract_path(42)
    assert path == "tests/acceptance/ms-42/contract.md"

    # Python: coord.milestone_dispatch.gate_a_status must go through
    # gate_a_contract_candidates (#2896) — itself built on
    # gate_a_contract_path — not a private re-derivation.
    import inspect

    from coord import milestone_dispatch

    assert "gate_a_contract_candidates(" in inspect.getsource(
        milestone_dispatch.gate_a_status
    )

    # coord.drive: resolve_oracle_decision must do the same.
    from coord import drive

    assert "gate_a_contract_candidates(" in inspect.getsource(
        drive.resolve_oracle_decision
    )

    # And gate_a_contract_candidates itself must be built on
    # gate_a_contract_path, not a second re-derivation of the convention.
    from coord.acceptance import gate_a_contract_candidates

    assert "gate_a_contract_path(" in inspect.getsource(gate_a_contract_candidates)


# ── decide()/_dispatch_work_stage with an active oracle decision ────────────


def test_oracle_inactive_dispatches_work_directly_as_before():
    """oracle=None (the default) is byte-for-byte the pre-#1453 behaviour."""
    action = step(state())
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_oracle_active_authors_the_slice_before_dispatching_work():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(state(), oracle=oracle)
    assert action.kind == RUN
    assert action.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
    )


def test_oracle_active_skips_dispatching_a_new_author_when_the_slice_already_landed():
    """#2061 (coord-portal#13): a retry must not re-author a slice that
    already merged from an earlier attempt — ask the manifest, not just
    the (possibly missing/stale) assignment row, before dispatching."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(landed=True)
    action = step(oracle_state(), oracle=oracle, gate_checker=checker)
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )
    assert checker.landed_calls == [(REPO, 38, 1392)]


def test_oracle_active_waits_while_the_slice_is_still_authoring():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker()
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="running"),
        oracle=oracle,
        gate_checker=checker,
    )
    assert action.kind == WAIT
    # #2061: "still authoring" never needs the authoritative manifest read —
    # the slice cannot possibly be on the default branch yet, so this must
    # not pay a GitHub fetch on every poll tick.
    assert checker.landed_calls == []


def test_oracle_active_dispatches_work_once_the_slice_has_merged():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="merged"),
        oracle=oracle,
    )
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_oracle_active_is_terminal_when_the_slice_authoring_fails():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="failed"),
        oracle=oracle,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "acceptance author ta1 failed" in action.message
    assert "--no-acceptance" in action.message


def test_oracle_active_failed_row_is_not_terminal_when_the_slice_already_landed():
    """#2061: a FAILED row describes what happened to THIS author, not
    whether the issue's slice exists — a stale/retried row can fail (or
    just be wrong) while an earlier attempt already merged the slice."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(landed=True)
    action = step(
        oracle_state(acceptance_author_aid="ta1", acceptance_author_status="failed"),
        oracle=oracle,
        gate_checker=checker,
    )
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_oracle_active_is_terminal_when_the_slice_authoring_is_cancelled():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="cancelled"),
        oracle=oracle,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "cancelled" in action.message


def test_oracle_active_cancelled_row_is_not_terminal_when_the_slice_already_landed():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(landed=True)
    action = step(
        oracle_state(acceptance_author_aid="ta1", acceptance_author_status="cancelled"),
        oracle=oracle,
        gate_checker=checker,
    )
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


# ── refused_policy: #2234 fix-1 — the acceptance-author half ────────────────
#
# `type="test-author"` is one of `_ZERO_COMMIT_TYPES` (coord.agent), so a JIT
# acceptance-author row goes through the identical `_looks_like_policy_
# refusal` classification as a plain `work` row and can reap
# `status="refused_policy"` just like it. Mirrors the `work_status ==
# "refused_policy"` tests above.


def test_oracle_active_is_terminal_when_the_slice_authoring_refuses_on_policy():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="refused_policy"),
        oracle=oracle,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert POLICY_REFUSAL_MARKER in action.message
    assert "needs the coordinator" in action.message.lower()


def test_oracle_active_refused_policy_row_is_not_terminal_when_the_slice_already_landed():
    """#2061: a stale/retried refusal row can still coexist with an earlier
    attempt's slice already landed — check the manifest before dying, same
    as the `failed`/`cancelled`/`advisory` branches above."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(landed=True)
    action = step(
        oracle_state(acceptance_author_aid="ta1", acceptance_author_status="refused_policy"),
        oracle=oracle,
        gate_checker=checker,
    )
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_oracle_active_still_honours_do_plan_after_the_slice_has_landed():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="merged"),
        DriveOptions(machine="precision", do_plan=True),
        oracle=oracle,
    )
    assert action.command == (
        "assign", "--plan-only", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


# ── #1453 review finding 1: --for-path resolution for a routed repo ─────────


def test_oracle_active_appends_for_path_when_the_gate_checker_resolves_one():
    """A ROUTED repo's `coord acceptance author` hard-refuses with no
    --for-path (coord.test_author.dispatch_test_author) — the driver must
    resolve and pass it, not dispatch blind."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(for_path="tui/**")
    action = step(oracle_state(), oracle=oracle, gate_checker=checker)
    assert action.kind == RUN
    assert action.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
        "--for-path", "tui/**",
    )
    assert checker.for_path_calls == [(REPO, 38)]


def test_oracle_active_omits_for_path_for_an_unrouted_repo():
    """resolve_for_path() returning None means "no --for-path needed" (flat,
    unrouted driver config, or none at all) — command is unchanged from
    before #1453's review fix."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(oracle_state(), oracle=oracle, gate_checker=FakeGateChecker())
    assert action.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
    )


def test_oracle_active_dies_when_for_path_cannot_be_resolved():
    """An ambiguous/unresolvable routed config must report and stop — not
    dispatch a `coord acceptance author` that the CLI will reject anyway
    (coord.acceptance.ForPathResolutionError)."""
    from coord.acceptance import ForPathResolutionError

    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(
        for_path_error=ForPathResolutionError("no route matched")
    )
    action = step(oracle_state(), oracle=oracle, gate_checker=checker)
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "no route matched" in action.message


# ── #1453 review finding 2: an ADVISORY JIT slice must not spin forever ─────


def test_oracle_active_advisory_with_no_commits_retries_a_fresh_author_then_stops_at_the_cap():
    """#2334: this used to be an immediate, unconditional `_die()` — a
    deliberate copy of the pre-#2416 `_decide_advisory` dead end (its own
    comment said "Mirror `_decide_advisory` exactly", including the bug).
    Now it mirrors the FIXED `_decide_advisory`: a bounded number of fresh
    `coord acceptance author` dispatches (`opts.max_work_retries`) before
    finally giving up — same shape as
    `test_advisory_with_no_commits_retries_via_coord_retry_then_stops_at_the_cap`."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = oracle_state(
        acceptance_author_aid="ta1",
        acceptance_author_status="advisory",
        acceptance_author_branch="",
    )

    first = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert first.kind == RUN
    assert first.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
    )
    assert counters.acceptance_author_retries == 1

    second = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "no commits" in second.message
    assert "ta1" in second.message


def test_oracle_active_advisory_with_no_commits_retry_resolves_for_path():
    """#2334: the retry dispatch must reuse the SAME `--for-path` resolution
    as the first-ever dispatch (#1453 review finding 1) — a ROUTED repo's
    `coord acceptance author` hard-refuses without it, so a retry that
    skipped this would just dispatch a doomed command."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    checker = FakeGateChecker(for_path="tui/**")
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = oracle_state(
        acceptance_author_aid="ta1",
        acceptance_author_status="advisory",
        acceptance_author_branch="",
    )

    retry = step(
        s, opts, oracle=oracle, verifier=verifier, counters=counters,
        gate_checker=checker,
    )
    assert retry.kind == RUN
    assert retry.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
        "--for-path", "tui/**",
    )
    assert "#2334" in retry.label
    assert "attempt 1/1" in retry.label


def test_oracle_active_advisory_with_no_commits_is_not_terminal_when_the_slice_already_landed():
    """#2061: a commit-less ADVISORY row is exactly what a stale/retried
    author's row looks like when an earlier attempt already merged the
    slice — check the manifest before declaring failure."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    checker = FakeGateChecker(landed=True)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="",
        ),
        oracle=oracle,
        verifier=verifier,
        gate_checker=checker,
    )
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_oracle_active_advisory_with_unverifiable_branch_waits():
    """#2426: `branch_has_commits` returning `None` (e.g. a `git fetch`
    failure) must NOT be treated as "no commits" — that misdiagnoses a
    transient verification failure as the terminal "nothing was authored"
    dead end (claude-coordinator#2286: a real, pushed commit sat on the
    remote while this exact path declared the branch commit-less). It must
    wait and let the next poll retry the check instead."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=None)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="test-author-ms-65-slice-2286",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.kind == WAIT
    assert not action.is_exit
    assert "no commits" not in (action.message or "")


def test_oracle_active_advisory_with_commits_requires_accept_advisory():
    """The #1357 false-positive shape — real commits, downgraded to
    advisory anyway — must not be treated as "still landing"; it needs the
    same --accept-advisory opt-in the main work row uses."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=True)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="issue-1453-slice",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "--accept-advisory" in action.message


def test_oracle_active_advisory_with_commits_and_accept_advisory_lands_the_slice():
    """#2079: "proceeding per --accept-advisory" proceeds to the MERGE.

    It used to be a bare WAIT, which for an ADVISORY row can never resolve —
    `coord.reconcile` skips advisory rows in the Test/Review/Merge auto-loop.
    """
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=True)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="issue-1453-slice",
        ),
        DriveOptions(machine="precision", accept_advisory=True),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "ta1", "--method", "rebase")
    assert any("--accept-advisory" in w for w in action.warnings)


def test_oracle_active_advisory_never_falls_through_to_a_bare_wait_label():
    """Regression guard for the #1453-review bug itself: an advisory JIT
    slice must never produce the generic "authoring/merging in progress"
    wait label — that label is the silent-spin signature (#1386's class)."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="",
        ),
        oracle=oracle,
        verifier=FakeVerifier(has_commits=False),
    )
    assert "authoring/merging in progress" not in (action.label or "")


# ── #1535: a DONE JIT slice must not spin to --deadline on zero commits ─────


def test_oracle_active_done_with_no_commits_retries_a_fresh_author_then_stops_at_the_cap():
    """The advisory-path guard, applied to `done`: a terminal status whose
    branch has zero commits can never reach `merged` on its own — waiting
    burns the deadline with no diagnosis (#1526's defect, reborn here).

    #2334: this used to `_die()` on the very first observation — the third
    copy of the `_decide_advisory` dead end (the ADVISORY branch above is
    the second). Now bounded-retries a fresh author first, same as the
    ADVISORY branch, sharing the same `acceptance_author_retries` budget."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = oracle_state(
        acceptance_author_aid="ta1",
        acceptance_author_status="done",
        acceptance_author_branch="test-author-ms-38-slice-1124",
    )

    first = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert first.kind == RUN
    assert first.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
    )
    assert counters.acceptance_author_retries == 1

    second = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "test-author-ms-38-slice-1124" in second.message
    assert "no commits" in second.message
    assert "ta1" in second.message


def test_oracle_active_done_with_no_commits_is_not_terminal_when_the_slice_already_landed():
    """#2061 (coord-portal#13): the exact observed shape — a re-dispatched
    author lands in a world where the slice is already merged from an
    earlier attempt, correctly does nothing (DONE, zero commits), and
    drive must read that as "already done", not re-diagnose it as a
    failure and block the queue entry."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    checker = FakeGateChecker(landed=True)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="done",
            acceptance_author_branch="test-author-ms-38-slice-1124",
        ),
        oracle=oracle,
        verifier=verifier,
        gate_checker=checker,
    )
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )
    assert checker.landed_calls == [(REPO, 38, 1392)]


def test_oracle_active_done_with_no_branch_eventually_terminal():
    """#2334: no branch at all is the same zero-commit dead-end signature as
    a branch verified empty — bounded retry, then terminal, same as
    `test_oracle_active_done_with_no_commits_retries_a_fresh_author_then_stops_at_the_cap`."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = oracle_state(
        acceptance_author_aid="ta1",
        acceptance_author_status="done",
        acceptance_author_branch="",
    )

    first = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert first.kind == RUN

    second = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "no commits" in second.message


def test_oracle_active_done_with_unverifiable_branch_waits():
    """#2426: the exact incident this issue documents — a terminal
    `test-author` DONE row whose branch really did carry a commit
    (claude-coordinator#2286's `test-author-ms-65-slice-2286`, commit
    `250b4df8`), observed through a `git fetch` failure. Must wait and
    retry, not declare DONE terminal-with-no-commits and burn a re-author
    cycle."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=None)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="done",
            acceptance_author_branch="test-author-ms-65-slice-2286",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.kind == WAIT
    assert not action.is_exit
    assert "no commits" not in (action.message or "")


def test_oracle_active_done_with_commits_lands_the_slice():
    """#2079: a DONE slice whose branch carries commits is MERGED by this
    driver, not waited on.

    Pre-#2079 this returned a bare WAIT on the theory that "coord's own tick
    loop drives Test → Review → Merge". Three of those four steps are indeed
    the daemon's, but the merge is `serve_app._auto_drain_tick`, gated on
    `merge.auto_drain` — off by default and off in the standing fleet config
    — so the wait was for an event that could not occur, and every oracle
    issue burned `2 × --deadline` and ended `blocked`.
    """
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=True)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="done",
            acceptance_author_branch="test-author-ms-38-slice-1124",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "ta1", "--method", "rebase")
    assert action.serialize_merge
    assert action.merge_scope == "acceptance"
    assert action.label.startswith("ACCEPTANCE/")
    assert not action.is_exit
    assert verifier.commits_calls == 1


def test_oracle_active_advisory_behaviour_is_unchanged_by_the_done_fix():
    """Regression guard: the `done` probe must not leak into the `advisory`
    branch's own handling. #2334: both now bounded-retry before dying, so
    exhaust the (shared, budget-1) retry first."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = oracle_state(
        acceptance_author_aid="ta1",
        acceptance_author_status="advisory",
        acceptance_author_branch="",
    )

    first = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert first.kind == RUN

    second = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "ADVISORY" in second.message


# ═══════════════════════════════════════════════════════════════════════════
# #2079: the JIT slice is LANDED, not merely watched
#
# The incident: coord-portal #32 sat `blocked` after two 240-minute attempts
# with no `issue-32-*` branch ever created — the worker never ran, because
# `issue_oracle_ready` reads the manifest from the DEFAULT BRANCH and the
# slice was still sitting on an unmerged branch behind a green,
# MERGEABLE/CLEAN PR. Nothing was wrong except that nothing merges a READY
# merge-queue entry when `merge.auto_drain` is false.
# ═══════════════════════════════════════════════════════════════════════════


def landing_state(**kw) -> IssueState:
    """An oracle state whose slice is authored, pushed and awaiting its
    merge — the shape the whole issue is about."""
    base = dict(
        acceptance_author_aid="ta1",
        acceptance_author_status="done",
        acceptance_author_branch="test-author-ms-38-slice-32",
        acceptance_author_test_state="passed",
        acceptance_review_aid="rv1",
        acceptance_review_verdict="approve",
        acceptance_merge_status="READY",
    )
    base.update(kw)
    return oracle_state(**base)


def landing_step(s: IssueState, opts: DriveOptions | None = None, **kw) -> Action:
    kw.setdefault("oracle", OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120))
    kw.setdefault("verifier", FakeVerifier(has_commits=True))
    return step(s, opts, **kw)


def test_ready_slice_is_merged_by_this_driver():
    """The fix, in one assertion: a READY slice queue entry gets the same
    bounded `coord merge --only <aid>` the work row already gets."""
    counters = DriveCounters()
    action = landing_step(landing_state(), counters=counters)
    assert action.command == ("merge", "--only", "ta1", "--method", "rebase")
    assert action.serialize_merge


def test_slice_merge_spends_its_own_budget_not_the_work_row_s():
    """Two PRs, two queues, two budgets. Landing the slice must not silently
    leave the issue's own merge with zero attempts left."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    for _ in range(3):
        assert landing_step(landing_state(), opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 0
    assert counters.acceptance is not None
    assert counters.acceptance.merge_attempts == 3

    # ...and the budget really is bounded: the 4th poll gives up with the
    # captured diagnostic rather than retrying forever.
    action = landing_step(landing_state(), opts, counters=counters)
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "merge attempted 3 times" in action.message
    # ...and the message says WHICH merge. It is the only thing a human sees
    # once the pane is gone (it becomes the issue comment / queue stop reason).
    assert "JIT acceptance slice" in action.message
    assert "ta1" in action.message


def test_slice_merge_diagnostic_is_filed_against_the_slice_budget():
    """`merge_scope` is what stops a slice attempt's `coord merge --only`
    output from being read back as the work row's own diagnosis."""
    action = landing_step(landing_state())
    assert action.merge_scope == "acceptance"

    work = step(
        state(
            work_aid="w1",
            work_status="done",
            work_branch="issue-1392",
            work_test_state="passed",
            review_verdict="approve",
            merge_status="READY",
        )
    )
    assert work.command[0] == "merge"
    assert work.merge_scope == "work"


def test_already_merged_slice_waits_for_the_board_instead_of_escalating():
    """MERGED is not in `_RETRYABLE_MERGE_STATUSES`, so handing it straight
    to `_decide_merge` would escalate a success."""
    action = landing_step(landing_state(acceptance_merge_status="MERGED"))
    assert action.kind == WAIT
    assert not action.is_exit
    assert "MERGED" in action.label


def test_failed_slice_test_stops_with_the_fix_command():
    """Nothing dispatches a fix for a test-author row whose Test stage
    failed — so this must not be a wait."""
    action = landing_step(
        landing_state(acceptance_author_test_state="failed", acceptance_merge_status="")
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "coord fix ta1" in action.message


def test_slice_review_request_changes_dispatches_a_fix():
    """#2425: the JIT lane's own twin of the work row's `REVIEW:
    request-changes → fix round` arm — before this, a request-changes
    verdict on the slice was a dead end nothing ever dispatched a fix for
    (claude-coordinator#2286: 11 re-authoring retries over ~11h, never once
    addressing the review)."""
    counters = DriveCounters()
    action = landing_step(
        landing_state(
            acceptance_review_verdict="request-changes",
            acceptance_merge_status="",
        ),
        counters=counters,
    )
    assert action.kind == RUN
    assert action.command == ("fix", "rv1")
    assert "rv1" in action.label
    assert not action.is_exit
    # Spends the SLICE's own budget, not the work row's (#2079's rule,
    # extended to fix rounds): landing the slice must not silently leave
    # the issue's own fix budget at zero.
    assert counters.fix_rounds == 0
    assert counters.acceptance is not None
    assert counters.acceptance.fix_rounds == 1
    assert counters.acceptance.review_fix_dispatched_for == "rv1"


def test_slice_review_fix_already_dispatched_waits_for_the_board():
    """The de-dup latch: a second poll against the SAME review id must not
    spawn a second fix worker on the same branch (#476/#477)."""
    counters = DriveCounters()
    counters.slice_budget().review_fix_dispatched_for = "rv1"
    action = landing_step(
        landing_state(
            acceptance_review_verdict="request-changes",
            acceptance_merge_status="",
        ),
        counters=counters,
    )
    assert action.kind == WAIT
    assert "rv1" in action.label
    assert counters.acceptance.fix_rounds == 0


def test_slice_review_fix_rounds_are_bounded_then_die():
    """Bounded the same way the work row's fix loop is — a slice review that
    keeps coming back request-changes must eventually escalate, not spin."""
    opts = DriveOptions(machine="precision", max_fix_rounds=2)
    counters = DriveCounters()
    for expected_round in (1, 2):
        action = landing_step(
            landing_state(
                acceptance_review_verdict="request-changes",
                acceptance_merge_status="",
            ),
            opts,
            counters=counters,
        )
        assert action.kind == RUN
        assert action.command == ("fix", "rv1")
        assert counters.acceptance.fix_rounds == expected_round
        # Clear the latch between rounds the same way a changed review row
        # would on the real board (a fresh review after the fix landed).
        counters.acceptance.review_fix_dispatched_for = ""

    action = landing_step(
        landing_state(
            acceptance_review_verdict="request-changes",
            acceptance_merge_status="",
        ),
        opts,
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "2 fix round(s)" in action.message
    assert "coord fix rv1" in action.message


def test_slice_review_request_changes_with_auto_loop_off_dies_without_dispatching():
    action = landing_step(
        landing_state(
            acceptance_review_verdict="request-changes",
            acceptance_merge_status="",
            auto_loop=False,
        )
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "auto_loop is OFF" in action.message
    assert "coord assign --interactive --fix-of rv1" in action.message


def test_slice_review_request_changes_with_no_review_id_refuses_to_guess():
    action = landing_step(
        landing_state(
            acceptance_review_verdict="request-changes",
            acceptance_merge_status="",
            acceptance_review_aid="",
        )
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "refusing to guess" in action.message


def test_no_merge_refuses_up_front_instead_of_idling_to_the_deadline():
    """`--no-merge` makes the run unable to progress at all (#1138 refuses
    the work dispatch until the slice lands) — say so immediately."""
    action = landing_step(
        landing_state(), DriveOptions(machine="precision", do_merge=False)
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "coord merge --only ta1" in action.message


def test_slice_still_awaiting_its_daemon_dispatched_test_verdict_waits():
    """The three stages the daemon DOES run unconditionally are still just
    observed — no `coord merge` is fired at a row that isn't enqueued."""
    action = landing_step(
        landing_state(
            acceptance_author_test_state="",
            acceptance_review_aid="",
            acceptance_review_verdict="",
            acceptance_merge_status="",
        )
    )
    # `merge_status=""` is retryable (the entry may simply not be enqueued
    # yet), so the driver attempts once and reads the refusal back — it never
    # sits silent.
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "ta1", "--method", "rebase")


def test_slice_escalation_names_the_tracking_issue_not_the_member_issue():
    """The slice's board row and queue entry are keyed on the milestone's
    TRACKING issue, so every command an escalation proposes must point
    there."""
    action = landing_step(landing_state(acceptance_merge_status="NEEDS_ATTENTION"))
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert "escalate" in action.command
    assert "1120" in action.command
    assert str(ISSUE) not in action.command


def test_slice_landing_never_touches_the_work_row_dispatch():
    """Belt and braces: while the slice is landing, `coord assign` must not
    be reachable — that is the #1138 refusal this whole path exists to
    avoid."""
    action = landing_step(landing_state())
    assert action.command[0] != "assign"


# ═══════════════════════════════════════════════════════════════════════════
# work-stage terminal states
# ═══════════════════════════════════════════════════════════════════════════


def test_failed_work_retries_through_the_cli_then_stops_at_the_cap():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(work_aid="w1", work_status="failed", work_failure_reason="boom")

    first = step(s, opts, counters=counters)
    assert first.command == ("retry", "w1")
    assert counters.work_retries == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "boom" in second.message


def test_usage_limit_kill_waits_instead_of_retrying_or_dying():
    """#1461: a usage-limit kill must WAIT (not retry, not die/escalate) —
    retrying before the reset just burns the same exhausted budget and fails
    again for no diagnostic reason. Must not consume the retry budget."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
    )
    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert counters.work_retries == 0
    assert any("usage-limit" in w for w in action.warnings)
    assert "8:30pm (America/Chicago)" in action.warnings[0]

    # And it keeps waiting — never escalates into the retry-cap die either,
    # even across repeated polls.
    action2 = step(s, opts, counters=counters)
    assert action2.kind == WAIT
    assert counters.work_retries == 0


def test_usage_limit_kill_on_advisory_also_waits():
    """A kill has been observed landing ADVISORY (clean exit, 0 commits) just
    as often as FAILED — must be recognised regardless of which terminal
    status the agent's own reap chose."""
    action = step(
        state(
            work_aid="w1", work_status="advisory",
            work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
        ),
        verifier=FakeVerifier(has_commits=False),
    )
    assert action.kind == WAIT


def test_usage_limit_wait_surfaces_the_earliest_resume_time():
    """#1590 part 3/6: the `reset_at_raw` the detector has always parsed is now
    turned into an absolute earliest-resume instant and surfaced, so the
    operator knows when the node comes back rather than just that it's parked."""
    action = step(
        state(
            work_aid="w1", work_status="failed",
            work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
        ),
    )
    assert action.kind == WAIT
    joined = "\n".join(action.warnings)
    assert "environmental (usage limit)" in joined
    assert "earliest resume 20" in joined  # ISO-8601, 20:30 local to Chicago


def test_retry_cap_death_names_the_failure_class():
    """#1590 part 6: 'drive died 3x' used to send the next person looking at
    the work. The exhausted-retry message now states which class it was."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=0)

    work = step(
        state(work_aid="w1", work_status="failed", work_failure_reason="tests failed"),
        opts, counters=counters,
    )
    assert work.is_exit
    assert "cause: work failure" in work.message

    # #2360: an environmental failure now gets the WIDER
    # `_ENVIRONMENTAL_WORK_RETRY_BUDGET` budget regardless of this test's
    # `max_work_retries=0` — drive it through the whole budget (a backoff
    # WAIT, then the retry, repeated) before it finally dies and still names
    # the cause. The exact number of rounds is an implementation detail; this
    # only asserts it eventually gives up and still reports why.
    env_state = state(
        work_aid="w1", work_status="failed",
        work_failure_reason='API Error: 529 {"type":"overloaded_error"}',
    )
    env_counters = DriveCounters()
    env = step(env_state, opts, counters=env_counters)
    rounds = 0
    while not env.is_exit:
        assert env.kind in (WAIT, RUN)
        rounds += 1
        assert rounds < 100  # guard against an accidental infinite loop
        env = step(env_state, opts, counters=env_counters)
    assert env.is_exit
    assert "cause: environmental" in env.message
    assert "529" in env.message


def test_environmental_work_failure_backs_off_then_retries_with_a_wider_budget():
    """#2360: an environmental WORK-stage failure (a Claude API 5xx here — the
    #2335 incident's actual shape was an OAuth hiccup, but any classifier-
    recognised environmental signal exercises the same widened path) no
    longer dies after the flat `max_work_retries=1` budget. It backs off
    `environmental_backoff_secs` before each retry and keeps going past what
    the flat budget would have allowed, only dying once its own wider budget
    is exhausted.

    #2335: an OAuth-refresh hiccup died after exactly the flat one retry and
    sat `blocked` for ~19h even though the machine was fine the entire time —
    this is the gap that incident exposed."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason='API Error: 529 {"type":"overloaded_error"}',
    )

    # Attempt 1: backs off before spending it — not an immediate retry.
    first = step(s, opts, counters=counters)
    assert first.kind == WAIT
    assert first.sleep_after == pytest.approx(environmental_backoff_secs(1))
    assert counters.work_retries == 0

    # The next poll (the backoff has "elapsed" by the time decide() is asked
    # again) actually fires the retry.
    retried = step(s, opts, counters=counters)
    assert retried.command == ("retry", "w1")
    assert counters.work_retries == 1

    # A SECOND failure keeps going past `max_work_retries=1` — a flat budget
    # would have died right here. Backoff grows with the attempt number.
    second_backoff = step(s, opts, counters=counters)
    assert second_backoff.kind == WAIT
    assert second_backoff.sleep_after == pytest.approx(environmental_backoff_secs(2))
    assert second_backoff.sleep_after > first.sleep_after
    assert counters.work_retries == 1

    second_retry = step(s, opts, counters=counters)
    assert second_retry.command == ("retry", "w1")
    assert counters.work_retries == 2

    # Eventually — after the wider budget, not the flat one — it still gives
    # up, and still names the cause.
    action = second_retry
    rounds = 0
    while not action.is_exit:
        assert action.kind in (WAIT, RUN)
        rounds += 1
        assert rounds < 100
        action = step(s, opts, counters=counters)
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "cause: environmental" in action.message
    assert counters.work_retries > opts.max_work_retries


def test_non_environmental_work_failure_still_dies_after_flat_max_work_retries():
    """#2360 acceptance: a real code-defect signature must NOT get the wider
    environmental budget — `classify_failure` calls it `work`, so it keeps
    today's tight `max_work_retries` budget completely unchanged, with no
    backoff wait inserted before the one retry it's allowed."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason="AssertionError: expected 2 commits, got 0 — tests failed",
    )

    first = step(s, opts, counters=counters)
    assert first.kind == RUN
    assert first.command == ("retry", "w1")
    assert counters.work_retries == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "cause: work failure" in second.message


def test_usage_limit_wait_is_unaffected_by_the_wider_environmental_retry_budget():
    """#2360: the usage-limit branch is checked (and returns) BEFORE the
    bounded-retry code that now widens the budget for other environmental
    causes — it must keep waiting for the reset, never retrying, even across
    enough polls to have exhausted the new wider budget for any other
    environmental cause."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
    )
    for _ in range(10):
        action = step(s, opts, counters=counters)
        assert action.kind == WAIT
        assert action.command == ()
        assert counters.work_retries == 0


def test_normal_advisory_failure_reason_is_not_mistaken_for_a_usage_limit():
    """A failure_reason that doesn't carry the exact stamped prefix must fall
    through to the ordinary retry-or-die path unchanged."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason="usage limit exceeded on some unrelated API call",
    )
    action = step(s, opts, counters=counters)
    assert action.command == ("retry", "w1")
    assert counters.work_retries == 1


def test_cancelled_work_is_terminal_and_says_how_to_re_dispatch():
    action = step(state(work_aid="w1", work_status="cancelled"))
    assert action.is_exit
    assert "--force" in action.message


# ── refused_policy: #2234's distinct terminal shape ─────────────────────────


def test_refused_policy_work_is_terminal_and_carries_the_2234_marker():
    """`coord.agent.REFUSED_POLICY` reaches here as `work_status ==
    "refused_policy"` and must exit — never wait, never treat it like a
    bounded-retry failure. The message embeds `POLICY_REFUSAL_MARKER` so
    `coord/drive_queue.py`'s `_reconcile_running` can recognise it out of
    this run's own `drive_exited` audit summary and park the queue entry
    (STATE_PARKED) instead of spending an attempt or landing in `blocked`
    — see tests/test_drive_queue.py's #2234 section for that half."""
    action = step(state(work_aid="w1", work_status="refused_policy"))
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert POLICY_REFUSAL_MARKER in action.message
    assert "needs the coordinator" in action.message.lower()


def test_refused_policy_still_blocking_names_the_assignment_and_age():
    """#2871: even the still-blocking case must stop reading as though a
    FRESH worker just refused again — the message names the pre-dispatch
    assignment it refused on and the row's age, not just generic prose."""
    action = step(
        state(
            work_aid="w1",
            work_status="refused_policy",
            work_finished_at=1000.0,
        )
    )
    assert action.is_exit
    assert "pre-dispatch refusal on assignment w1" in action.message
    assert "refused_policy" in action.message
    assert POLICY_REFUSAL_MARKER in action.message


def test_refused_policy_is_bypassed_when_the_issue_was_retargeted():
    """#2871 / CC#916: a `refused_policy` row is a verdict on the ASK the
    worker was shown, not a standing veto on the issue number forever.
    `coord.claim.find_work_claim` never even sees this row (terminal rows
    live in `board.completed`) — the reason a stale refusal vetoed every
    later drive was entirely in `decide()` reading the fossil row as "this
    run's" state before ever reaching dispatch. If the issue's title was
    rewritten after the row finished, the branch it would have produced no
    longer matches — that mismatch means the ask changed, so this must
    dispatch fresh work instead of dying on the old refusal again."""
    s = state(
        work_aid="w1",
        work_status="refused_policy",
        work_branch="issue-1392-old-pre-retarget-title",
        issue_title="A completely different retargeted deliverable",
        work_finished_at=1000.0,
    )
    action = step(s)
    assert action.kind == RUN
    assert action.command[0] == "assign"
    assert action.audit_event is not None
    event_type, summary, details = action.audit_event
    assert event_type == "refused_policy_stale"
    assert "w1" in summary
    assert details["stale_assignment_id"] == "w1"
    assert details["stale_branch"] == "issue-1392-old-pre-retarget-title"


def test_refused_policy_is_not_bypassed_when_the_title_is_unchanged():
    """The inverse of the retarget test above: the branch still matches
    `issue-{N}-{slugify(current title)}`, so nothing was retargeted — this
    must still die rather than silently re-dispatch over a worker that
    correctly refused."""
    from coord.agent import _slugify  # noqa: PLC0415

    title = "Exactly the title this branch was named from"
    s = state(
        work_aid="w1",
        work_status="refused_policy",
        work_branch=f"issue-{ISSUE}-{_slugify(title)}",
        issue_title=title,
    )
    action = step(s)
    assert action.is_exit
    assert POLICY_REFUSAL_MARKER in action.message
    assert action.audit_event is None


def test_refused_policy_still_blocking_distinguishes_uncertain_from_confident():
    """#2881: the original #2871 message collapsed "title didn't resolve
    from the board payload" and "title resolved but is unchanged" into the
    identical confident "retarget the issue, the next drive will see it"
    prose — which is exactly why the #2881 payload bug (``title`` missing
    from the daemon-host ``_local_issue_rows()`` SELECT) went unnoticed for a
    release cycle: every real drive silently landed in the uncertain case and
    nothing in the operator-facing text said so. The uncertain case (no
    branch recorded, or no title resolved) must say the driver could not
    confirm either way; the confident case (title resolved, branch
    unchanged) keeps the original #2871 remedy verbatim.
    """
    uncertain = step(
        state(work_aid="w1", work_status="refused_policy", work_finished_at=1000.0)
    )
    assert uncertain.is_exit
    assert "could not confirm" in uncertain.message.lower()
    assert "the next `coord drive` detects the retarget" not in uncertain.message

    from coord.agent import _slugify  # noqa: PLC0415

    title = "Exactly the title this branch was named from"
    confident = step(
        state(
            work_aid="w1",
            work_status="refused_policy",
            work_branch=f"issue-{ISSUE}-{_slugify(title)}",
            issue_title=title,
            work_finished_at=1000.0,
        )
    )
    assert confident.is_exit
    assert "could not confirm" not in confident.message.lower()
    assert "the next `coord drive` detects the retarget" in confident.message


def test_an_unknown_terminal_status_refuses_to_guess():
    """No terminal status may fall through to a bare wait (PR #1386)."""
    action = step(state(work_aid="w1", work_status="wat"))
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "refusing to guess" in action.message


def test_done_work_with_no_branch_is_terminal():
    action = step(state(work_aid="w1", work_status="done", work_branch=""))
    assert action.is_exit
    assert "no branch" in action.message


# ── advisory: distinguished by whether the branch actually carries commits ───


def test_advisory_with_no_commits_retries_via_coord_retry_then_stops_at_the_cap():
    """#2416: a genuine zero-commit ADVISORY row used to be an immediate,
    unconditional `_die()` — the ONE thing that can actually change it,
    `coord retry <aid>`'s zero-commit-advisory path (#1606: `ahead == 0` →
    reassign a fresh worker), was never invoked automatically, so every
    automatic retry (drive-queue's own included) just relaunched a whole new
    `coord drive` process, which re-observed the identical terminal row and
    died again in seconds — burning a queue attempt+backoff cycle for
    nothing (coord-portal#119, drive-queue entry 432). Now this is a bounded
    `coord retry` dispatch, same shape/budget as the `failed` branch's
    `work_retries` (`opts.max_work_retries`), mirroring
    `test_failed_work_retries_through_the_cli_then_stops_at_the_cap` above —
    before finally giving up for a human."""
    verifier = FakeVerifier(has_commits=False)
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(work_aid="w1", work_status="advisory", work_branch="b")

    first = step(s, opts, verifier=verifier, counters=counters)
    assert first.kind == RUN
    assert first.command == ("retry", "w1")
    assert counters.advisory_retries == 1

    second = step(s, opts, verifier=verifier, counters=counters)
    assert second.is_exit
    assert "no commits on its branch" in second.message
    assert "coord retry w1" in second.message
    assert verifier.commits_calls == 2


def test_advisory_with_unverifiable_branch_waits_without_spending_a_retry():
    """#2426: a `git fetch` failure while checking the branch is not proof
    it's empty. Must wait for the next poll rather than either spending an
    `advisory_retries` budget attempt or (once the budget is spent) dying
    with the terminal "no commits" message on evidence that never
    materialized."""
    verifier = FakeVerifier(has_commits=None)
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(work_aid="w1", work_status="advisory", work_branch="b")

    action = step(s, opts, verifier=verifier, counters=counters)
    assert action.kind == WAIT
    assert not action.is_exit
    assert counters.advisory_retries == 0


def test_advisory_with_no_commits_retry_is_unaffected_by_accept_advisory():
    """#1606: `--accept-advisory` exists to unblock the #1357 false-positive
    (real commits, downgraded status) — it must NOT adopt a genuine
    zero-commit advisory as though it were completed work. The zero-commit
    check (and its #2416 bounded retry) runs before the accept_advisory
    branch is ever consulted, so it behaves identically with or without the
    flag."""
    verifier = FakeVerifier(has_commits=False)
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", accept_advisory=True, max_work_retries=1)
    s = state(work_aid="w1", work_status="advisory", work_branch="b")

    first = step(s, opts, verifier=verifier, counters=counters)
    assert first.kind == RUN
    assert first.command == ("retry", "w1")

    second = step(s, opts, verifier=verifier, counters=counters)
    assert second.is_exit
    assert "no commits on its branch" in second.message


def test_advisory_with_commits_stops_and_names_1357_without_accept_advisory():
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b"),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.is_exit
    assert "#1357" in action.message
    assert "--accept-advisory" in action.message


def test_advisory_with_commits_proceeds_under_accept_advisory():
    """PR #1386: this arm used to fall through to a bare sleep — a silent
    240-minute spin. It must reach the Test gate and warn while doing so."""
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b", work_test_state=""),
        DriveOptions(machine="precision", accept_advisory=True),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.kind == WAIT  # waiting on coord to dispatch the Test stage
    assert any("--accept-advisory" in w for w in action.warnings)


def test_advisory_with_commits_and_a_passed_test_reaches_the_merge_stage():
    action = step(
        state(
            work_aid="w1",
            work_status="advisory",
            work_branch="b",
            work_test_state="passed",
            review_verdict="approve",
        ),
        DriveOptions(machine="precision", accept_advisory=True),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.command[:2] == ("merge", "--only")
    assert any("--accept-advisory" in w for w in action.warnings)


def test_advisory_with_no_branch_at_all_retries_then_is_eventually_terminal():
    """No branch at all is the same #2416 dead-end signature as a branch
    verified to carry zero commits — bounded `coord retry`, then terminal."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(work_aid="w1", work_status="advisory", work_branch="")

    first = step(s, opts, counters=counters)
    assert first.kind == RUN
    assert first.command == ("retry", "w1")

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert "no commits on its branch" in second.message


# ── analysis deliverable: done + 0 commits is a SUCCESS, not a dead end (#2188)


def test_analysis_deliverable_done_with_no_commits_succeeds():
    """#2188: `coord.agent.AgentServer._reap` never lands a `deliverable:
    analysis` issue's 0-commit exit on `advisory` — it lands DONE. `coord
    drive` must record that as a success instead of dying on 'no commits
    on its branch'/'no branch' the way an ordinary work row would."""
    action = step(
        state(
            work_aid="w1",
            work_status="done",
            work_branch="issue-2132-diagnose",
            issue_labels=("deliverable:analysis",),
        ),
        verifier=FakeVerifier(has_commits=False),
    )
    assert action.is_exit
    assert action.exit_code == EXIT_OK
    assert "ANALYSIS" in action.message
    assert "deliverable:analysis" in action.message


def test_analysis_deliverable_done_with_unverifiable_branch_waits():
    """#2426: a `git fetch` failure while checking an analysis-labelled
    branch is neither "0-commit success" nor "commits pushed, fall
    through" — it's no evidence at all. Must wait for the next poll."""
    action = step(
        state(
            work_aid="w1",
            work_status="done",
            work_branch="issue-2132-diagnose",
            issue_labels=("deliverable:analysis",),
        ),
        verifier=FakeVerifier(has_commits=None),
    )
    assert action.kind == WAIT
    assert not action.is_exit


def test_analysis_deliverable_done_with_no_branch_at_all_succeeds():
    """Same as above when the reap never captured a branch name at all
    (e.g. the worktree was already gone) — `not state.work_branch` alone
    must be enough to take the analysis-deliverable exit, without ever
    calling `verifier.branch_has_commits` on an empty branch."""
    verifier = FakeVerifier(has_commits=True)
    action = step(
        state(
            work_aid="w1",
            work_status="done",
            work_branch="",
            issue_labels=("deliverable:analysis",),
        ),
        verifier=verifier,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_OK
    assert verifier.commits_calls == 0


def test_analysis_deliverable_done_with_commits_falls_through_unchanged():
    """The label describes the common case, not a hard rule: a
    `deliverable:analysis` issue whose worker DID push commits must reach
    the ordinary Test/Review/Merge pipeline exactly like any other done
    row, not the analysis short-circuit."""
    action = step(
        done_work(work_test_state="", issue_labels=("deliverable:analysis",)),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.kind == WAIT  # waiting on coord to dispatch the Test stage


def test_unlabelled_done_with_no_commits_is_unaffected_by_2188():
    """#2188 acceptance: an ordinary (unlabelled) issue must be completely
    unaffected by the analysis-deliverable short-circuit — `coord.agent.
    AgentServer._reap` never puts an unlabelled 0-commit work row on
    `done` in the first place (it stays `advisory`, covered by the tests
    above), but this guards `decide()`'s own logic in case a `done` row
    with no branch/commits reaches it some other way."""
    action = step(
        state(work_aid="w1", work_status="done", work_branch=""),
        verifier=FakeVerifier(has_commits=False),
    )
    assert action.is_exit
    assert "no branch" in action.message
    assert "ANALYSIS" not in action.message


# ═══════════════════════════════════════════════════════════════════════════
# the TEST gate
# ═══════════════════════════════════════════════════════════════════════════


def done_work(**kw) -> IssueState:
    base = dict(work_aid="w1", work_status="done", work_branch="issue-1392-x")
    base.update(kw)
    return state(**base)


# ── #2199: the oracle-loop TRUST GATE (`_decide_acceptance_gate`) ──────────
#
# Before #2199 nothing ever called `coord acceptance record` — an issue
# driven end-to-end by `coord drive` completed with `acceptance_state =
# None` forever, so `_maybe_clear_expected_red` could never clear
# (quadraui#542). These tests are the call site: it must fire between the
# dead-end predicate and the Test gate, run EXTERNALLY (this process, not
# the worker's), block (not warn) on a genuinely failed verdict, and never
# fire at all for an issue that opted out of the sealed suite.

TRUST_GATE_SHA = "deadbeef" * 5  # FakeVerifier's own default head_sha


def test_non_oracle_drive_never_touches_the_trust_gate():
    """`oracle=None` — every pre-#2199 call site — behaves byte-identically:
    straight through to the Test gate, `coord acceptance record` never
    dispatched, the SHA never even resolved."""
    verifier = FakeVerifier()
    action = step(done_work(work_test_state=""), verifier=verifier)
    assert action.kind == WAIT
    assert action.command == ()
    assert verifier.head_sha_calls == 0


def test_oracle_inactive_never_touches_the_trust_gate():
    """`oracle.active=False` (no driver, no milestone, `--no-acceptance`) —
    same as `oracle=None`."""
    verifier = FakeVerifier()
    oracle = OracleDecision(False, "no driver configured — normal drive")
    action = step(done_work(work_test_state=""), oracle=oracle, verifier=verifier)
    assert action.kind == WAIT
    assert action.command == ()
    assert verifier.head_sha_calls == 0


def test_oracle_active_but_issue_exempt_skips_the_gate():
    """`manifest.exempt`/`oracle:exempt` — #2199 acceptance criterion:
    exempt issues must not acquire a new blocking gate. Falls straight
    through to Test; `coord acceptance record` is never dispatched because
    there is no sealed slice for it to re-run."""
    verifier = FakeVerifier()
    oracle = OracleDecision(
        True, "ORACLE DRIVE", tracking_issue=1120, issue_exempt=True,
    )
    action = step(done_work(work_test_state=""), oracle=oracle, verifier=verifier)
    assert action.kind == WAIT
    assert action.command == ()
    assert verifier.head_sha_calls == 0


def test_oracle_active_dispatches_the_trust_gate_when_no_verdict_recorded():
    counters = DriveCounters()
    verifier = FakeVerifier(head_sha=TRUST_GATE_SHA)
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        done_work(work_test_state=""), oracle=oracle, verifier=verifier,
        counters=counters,
    )
    assert action.command == (
        "acceptance", "record", "--repo", REPO, "--issue", str(ISSUE),
        "--sha", TRUST_GATE_SHA,
    )
    # #2199: a red verdict must not crash the whole drive — see
    # `_decide_acceptance_gate`'s docstring for why this specific RUN
    # action is the one exception to the die-on-nonzero default.
    assert action.on_error == "warn"
    assert counters.acceptance_gate_attempts == 1


def test_trust_gate_passed_for_current_sha_falls_through_to_test():
    verifier = FakeVerifier(head_sha=TRUST_GATE_SHA)
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    s = done_work(
        work_test_state="",
        work_acceptance_state="passed",
        work_acceptance_sha=TRUST_GATE_SHA,
    )
    action = step(s, oracle=oracle, verifier=verifier)
    # Falls through to the REAL Test gate: work_test_state == "" waits for
    # coord's own dispatch, exactly like the non-oracle case.
    assert action.kind == WAIT
    assert action.command == ()


def test_trust_gate_failed_for_current_sha_loops_through_coord_fix():
    """#2199 acceptance: 'a failed trust gate must block, not warn ... at
    the same place a failed Test verdict does' — the SAME bounded fix-round
    loop `_decide_test` uses for a failed test."""
    counters = DriveCounters()
    verifier = FakeVerifier(head_sha=TRUST_GATE_SHA)
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    s = done_work(
        work_acceptance_state="failed",
        work_acceptance_sha=TRUST_GATE_SHA,
        work_acceptance_reason="2/4 acceptance red",
    )
    action = step(s, oracle=oracle, verifier=verifier, counters=counters)
    assert action.command == ("fix", "w1")
    assert counters.fix_rounds == 1


def test_trust_gate_fix_loop_is_bounded_by_max_fix_rounds():
    counters = DriveCounters()
    verifier = FakeVerifier(head_sha=TRUST_GATE_SHA)
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    opts = DriveOptions(machine="precision", max_fix_rounds=1)
    s = done_work(
        work_acceptance_state="failed",
        work_acceptance_sha=TRUST_GATE_SHA,
        work_acceptance_reason="2/4 acceptance red",
    )
    first = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert first.command == ("fix", "w1")
    exhausted = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_TERMINAL_FAILURE
    assert "after 1 fix round(s)" in exhausted.message
    assert "2/4 acceptance red" in exhausted.message


def test_trust_gate_reruns_record_against_a_fresh_sha_after_a_fix_round():
    """New commits landed (a fix round, a rebase) since the last recorded
    verdict — re-dispatch against the CURRENT sha, not the stale one that
    verdict was for (mirrors `review_head_sha` staleness detection)."""
    verifier = FakeVerifier(head_sha=TRUST_GATE_SHA)
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    s = done_work(
        work_acceptance_state="passed",
        work_acceptance_sha="stalestalestalestalestalestalestalestale",
    )
    action = step(s, oracle=oracle, verifier=verifier)
    assert action.command == (
        "acceptance", "record", "--repo", REPO, "--issue", str(ISSUE),
        "--sha", TRUST_GATE_SHA,
    )


def test_trust_gate_waits_when_the_sha_cannot_be_resolved():
    """GitHub unreachable or the branch vanished between polls — retry
    next poll rather than blocking a whole drive run on a transient
    lookup."""
    verifier = FakeVerifier(head_sha=None)
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(done_work(), oracle=oracle, verifier=verifier)
    assert action.kind == WAIT
    assert action.command == ()


def test_trust_gate_dispatch_attempts_are_bounded_by_max_work_retries():
    """A `coord acceptance record` that keeps erroring out before ever
    recording a verdict (broken checkout, driver crash) must reach a named
    terminal failure instead of re-running a full worktree + suite
    invocation every poll forever."""
    counters = DriveCounters()
    verifier = FakeVerifier(head_sha=TRUST_GATE_SHA)
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = done_work(work_test_state="")

    first = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert first.command[:2] == ("acceptance", "record")
    exhausted = step(s, opts, oracle=oracle, verifier=verifier, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_TERMINAL_FAILURE
    assert "never produced a verdict" in exhausted.message


def test_trust_gate_attempts_reset_for_a_fresh_sha_after_a_fix_round():
    """#2199 review (blocking finding 1): `acceptance_gate_attempts` must be
    scoped to ONE sha, not a lifetime total shared across every sha a drive
    ever sees. With default `opts` (`max_work_retries=1`), sharing one
    un-reset counter meant the legitimate SECOND dispatch — for the FRESH
    sha a fix round just pushed — inherited the first sha's already-spent
    budget and died immediately with a false "environment broken"
    diagnosis, even though the trust gate was working exactly as designed.
    This is quadraui#542's actual shape: one fix round, then this."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision")  # max_work_retries defaults to 1
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)

    sha1 = "a" * 40
    first = step(
        done_work(work_test_state=""), opts, oracle=oracle,
        verifier=FakeVerifier(head_sha=sha1), counters=counters,
    )
    assert first.command == (
        "acceptance", "record", "--repo", REPO, "--issue", str(ISSUE),
        "--sha", sha1,
    )
    assert counters.acceptance_gate_attempts == 1

    # A failed verdict recorded at sha1 spends the SHARED fix_rounds budget
    # (not acceptance_gate_attempts) and dispatches `coord fix`.
    s_failed = done_work(
        work_acceptance_state="failed", work_acceptance_sha=sha1,
        work_acceptance_reason="2/4 acceptance red",
    )
    fix = step(
        s_failed, opts, oracle=oracle, verifier=FakeVerifier(head_sha=sha1),
        counters=counters,
    )
    assert fix.command == ("fix", "w1")
    assert counters.fix_rounds == 1

    # coord fix pushed a new commit -> a fresh sha. The next poll must
    # dispatch `coord acceptance record` against it — NOT die claiming the
    # trust gate "never produced a verdict" — even though
    # acceptance_gate_attempts already sat at opts.max_work_retries (1)
    # from sha1's dispatch above.
    sha2 = "b" * 40
    second = step(
        done_work(work_test_state=""), opts, oracle=oracle,
        verifier=FakeVerifier(head_sha=sha2), counters=counters,
    )
    assert second.command == (
        "acceptance", "record", "--repo", REPO, "--issue", str(ISSUE),
        "--sha", sha2,
    )
    assert counters.acceptance_gate_attempts == 1
    assert counters.acceptance_gate_attempts_sha == sha2


def test_trust_gate_still_bounded_by_max_work_retries_for_the_same_sha():
    """The reset above must not turn the budget into an unbounded retry —
    two consecutive dispatch attempts for the SAME sha (never advancing to
    failed/passed) still exhausts `opts.max_work_retries` and dies."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    s = done_work(work_test_state="")

    first = step(s, opts, oracle=oracle, verifier=FakeVerifier(head_sha=TRUST_GATE_SHA), counters=counters)
    assert first.command[:2] == ("acceptance", "record")
    exhausted = step(s, opts, oracle=oracle, verifier=FakeVerifier(head_sha=TRUST_GATE_SHA), counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_TERMINAL_FAILURE
    assert "never produced a verdict" in exhausted.message


# ── #2199 review (blocking finding 3): --for-path resolution for the gate ──


def test_trust_gate_appends_for_path_when_the_gate_checker_resolves_one():
    """A ROUTED repo's `coord acceptance record` hard-refuses with no
    --for-path (coord.commands.acceptance._resolve_driver's "no route
    matched") — the trust gate must resolve and pass it, exactly like
    `_decide_acceptance_author` already does, not dispatch blind."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(for_path="tui/**")
    action = step(
        done_work(work_test_state="", milestone_number=38), oracle=oracle,
        verifier=FakeVerifier(head_sha=TRUST_GATE_SHA), gate_checker=checker,
    )
    assert action.command == (
        "acceptance", "record", "--repo", REPO, "--issue", str(ISSUE),
        "--sha", TRUST_GATE_SHA, "--for-path", "tui/**",
    )
    assert checker.for_path_calls == [(REPO, 38)]


def test_trust_gate_omits_for_path_for_an_unrouted_repo():
    """resolve_for_path() returning None means "no --for-path needed" —
    command is unchanged from before this fix."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        done_work(work_test_state=""), oracle=oracle,
        verifier=FakeVerifier(head_sha=TRUST_GATE_SHA), gate_checker=FakeGateChecker(),
    )
    assert action.command == (
        "acceptance", "record", "--repo", REPO, "--issue", str(ISSUE),
        "--sha", TRUST_GATE_SHA,
    )


def test_trust_gate_dies_when_for_path_cannot_be_resolved():
    """An ambiguous/unresolvable routed config must report and stop — not
    dispatch a `coord acceptance record` the CLI will reject anyway
    (coord.acceptance.ForPathResolutionError)."""
    from coord.acceptance import ForPathResolutionError

    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(
        for_path_error=ForPathResolutionError("no route matched")
    )
    action = step(
        done_work(work_test_state=""), oracle=oracle,
        verifier=FakeVerifier(head_sha=TRUST_GATE_SHA), gate_checker=checker,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "no route matched" in action.message


def test_no_test_verdict_yet_waits_for_coord_to_dispatch_the_stage():
    """#1426: coord's own dispatch_pending_smoke runs the Test stage. Two
    drivers racing to dispatch the same thing is the #476/#477 incident."""
    action = step(done_work(work_test_state=""))
    assert action.kind == WAIT
    assert action.command == ()


def test_skip_test_records_the_verdict_through_the_test_cli():
    """The CLI, never record_test_verdict() — see #1384."""
    action = step(done_work(), DriveOptions(machine="precision", skip_test=True))
    assert action.command == (
        "test", "--skipped", "--reason", "coord drive --skip-test", "w1",
    )
    assert action.sleep_after == 5.0


def test_a_running_test_stage_just_waits():
    action = step(done_work(work_test_state="running"))
    assert action.kind == WAIT


def test_a_running_test_stage_with_no_smoke_child_still_just_waits():
    """A plain in-flight Test stage (smoke child still running, or none
    dispatched yet) is not the #1605 contradiction — must not be confused
    with a stranded one."""
    action = step(done_work(work_test_state="running", smoke_aid="s1", smoke_status="running"))
    assert action.kind == WAIT


def test_stuck_test_state_with_a_terminal_smoke_child_is_actionable_not_a_loop():
    """#1605: the Test-stage CHILD assignment (`type="smoke"`) already
    finished FAILED (a dead agent, a killed process group, a terminal API
    error — the #1598 incident's exact shape) but `test_state` was never
    resolved off it — stuck at `"running"` forever. Before this, `_decide_test`
    only ever looked at `work_test_state` and returned an unbounded `_wait()`
    here, which is exactly how #1598 polled a phantom Test stage for 2.5
    hours against three idle machines. This must terminate the drive loop
    with an actionable message instead."""
    action = step(done_work(
        work_test_state="running",
        smoke_aid="smoke-1605",
        smoke_status="failed",
        smoke_failure_reason="api_error: aborted_streaming",
    ))
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "smoke-1605" in action.message
    assert "api_error: aborted_streaming" in action.message
    assert "coord diagnose" in action.message


def test_stuck_test_state_with_a_cancelled_smoke_child_is_also_actionable():
    action = step(done_work(
        work_test_state="running", smoke_aid="smoke-2", smoke_status="cancelled",
    ))
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE


def test_a_done_smoke_child_with_lagging_test_state_still_just_waits():
    """A fresh `done` smoke completion has an expected, bounded propagation
    lag before `coord notify` records its verdict on the parent — that is
    NOT the #1605 bug and must not trip the contradiction check."""
    action = step(done_work(
        work_test_state="running", smoke_aid="smoke-3", smoke_status="done",
    ))
    assert action.kind == WAIT


def test_a_failed_test_loops_through_coord_fix_on_the_same_branch():
    """`coord fix` gates on the legacy smoke_test field and dispatches with
    inherit_branch=True — the same branch, model escalated (#1445)."""
    counters = DriveCounters()
    action = step(
        done_work(work_test_state="failed", work_test_reason="3 failed"),
        counters=counters,
    )
    assert action.command == ("fix", "w1")
    assert counters.fix_rounds == 1
    assert "smoke_test" in action.error_message  # the diagnosis if it won't dispatch
    assert action.on_error == "die"


def test_the_test_fix_loop_is_bounded_by_max_fix_rounds():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=2)
    s = done_work(work_test_state="failed", work_test_reason="3 failed")

    assert step(s, opts, counters=counters).command == ("fix", "w1")
    assert step(s, opts, counters=counters).command == ("fix", "w1")
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_TERMINAL_FAILURE
    assert "after 2 fix round(s)" in exhausted.message
    assert "3 failed" in exhausted.message


def test_max_fix_rounds_zero_never_dispatches_a_fix():
    action = step(
        done_work(work_test_state="failed", work_test_reason="3 failed"),
        DriveOptions(machine="precision", max_fix_rounds=0),
    )
    assert action.is_exit


def test_a_failed_test_with_no_reason_refuses_to_dispatch_a_fix():
    """#2596: `test_state == 'failed'` with an EMPTY reason is not a graded
    failure — every real failure path (a worker's own `SMOKE: fail` text,
    `coord.confirm_test`'s "REFUTED by an independent re-run" wording)
    populates `test_reason`. An empty reason means the gate flipped red
    without ever extracting what failed — the 2026-08-22 incident's shape,
    which dispatched a worker (escalated to opus on retry) that ran 20
    minutes finding nothing because there was nothing to find. This must
    refuse to dispatch `coord fix` and surface the suspicious verdict
    instead of burning a round."""
    counters = DriveCounters()
    action = step(
        done_work(work_test_state="failed", work_test_reason=""), counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert counters.fix_rounds == 0, "must not consume a fix round on nothing"
    assert "NO reason" in action.message
    assert "#2596" in action.message
    assert "coord diagnose" in action.message


def test_a_failed_test_with_whitespace_only_reason_also_refuses():
    """Whitespace is not a reason either — a stray newline must not slip
    past the #2596 guard the way it would past a bare truthiness check."""
    action = step(
        done_work(work_test_state="failed", work_test_reason="   \n  "),
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert action.command == ()


def test_an_unexpected_test_state_warns_and_waits_rather_than_guessing():
    action = step(done_work(work_test_state="weird"))
    assert action.kind == WAIT
    assert any("weird" in w for w in action.warnings)


@pytest.mark.parametrize("verdict", ["passed", "skipped"])
def test_a_passed_or_skipped_test_falls_through_to_the_review_gate(verdict):
    action = step(done_work(work_test_state=verdict))
    assert action.kind == WAIT  # no review row yet
    assert action.command == ()


# ═══════════════════════════════════════════════════════════════════════════
# the REVIEW gate
# ═══════════════════════════════════════════════════════════════════════════


def work_tested(**kw) -> IssueState:
    return done_work(work_test_state="passed", **kw)


def test_no_review_row_yet_waits_for_coords_auto_dispatch():
    action = step(work_tested())
    assert action.kind == WAIT
    assert action.command == ()


def test_a_review_that_finished_with_no_verdict_is_terminal():
    action = step(work_tested(review_aid="r1", work_review_state="done"))
    assert action.is_exit
    assert "NO verdict" in action.message


# ═══════════════════════════════════════════════════════════════════════════
# the dead-end predicate (#2019)
#
# `coord drive` could not tell "still working" from "finished in a state I
# cannot act on". Both rendered as `no state change`, and the second looped
# forever. claude-coordinator#1956, 2026-08-08: 140 minutes of a live drive
# session, a held queue slot and a held per-repo capacity slot (#1972),
# producing nothing, with `active=0` printed on every single line.
#
# WHY the pre-existing "review finished with no verdict" die above did not
# catch it: it keys on `work_review_state` — the WORK row's projected
# `review_state` — while the incident's board line read `review=done/-`,
# which is `review_status`, the REVIEW row's own status. Advancing the work
# row's `review_state` is exactly what recording a verdict does, so on the
# one board shape where the verdict is missing, the field the die reads is
# guaranteed to be stale. Two readings of "the review is done"; only one was
# ever checked.
# ═══════════════════════════════════════════════════════════════════════════


def test_a_terminal_review_with_no_verdict_escalates_instead_of_looping():
    """#2019 acceptance: "a board state of work=done test=passed
    review=done/verdict=None drives to escalation, not to a `no state change`
    loop", and "exits non-zero within one poll rather than looping".

    This is the #1956 incident state, verbatim, in ONE decide() call.
    """
    action = step(
        work_tested(review_aid="c9b489b2333e", review_status="done", review_verdict="")
    )
    assert action.is_exit
    assert action.exit_code == EXIT_DEAD_END
    # Distinguishable from a crash (1) and from a #1844 guard refusal (5) by
    # the exit code alone — that code is the only thing `drive_queue`'s tick
    # can read once the process is gone.
    assert action.exit_code not in (EXIT_TERMINAL_FAILURE, EXIT_DISPATCH_REFUSED)


def test_the_dead_end_message_names_the_dead_end_and_the_recovery_command():
    """#2019 ask 3. `no state change in 140.558m` is not actionable; the
    documented `coord report-result` relay is. And the reason must not send an
    operator to CLOSED #812 for a headless review that ran to completion."""
    action = step(
        work_tested(review_aid="c9b489b2333e", review_status="done", review_verdict="")
    )
    assert "review_terminal_no_verdict" in action.message
    assert "coord report-result --assignment c9b489b2333e" in action.message
    assert "--verdict-source recovered" in action.message
    assert "#812" not in action.message
    assert "no state change" not in action.message


def test_the_dead_end_records_a_board_visible_escalation_through_the_cli():
    """Same contract every other board mutation in this module honours: the
    write goes out as a `coord` subcommand argv (run by `_loop`'s exit
    handling), never a direct internal call. Without it the reason dies with
    the tmux pane — which is exactly how the 2026-07-27/28 run produced three
    unexplained deaths (#1526)."""
    action = step(
        work_tested(review_aid="c9b489b2333e", review_status="done", review_verdict="")
    )
    assert action.command[:4] == ("escalate", "record", REPO, str(ISSUE))
    assert "--stage" in action.command
    assert action.command[action.command.index("--stage") + 1] == "review"
    proposed = action.command[action.command.index("--command") + 1]
    assert proposed.startswith("coord report-result --assignment c9b489b2333e")
    assert action.command[action.command.index("--assignment") + 1] == "c9b489b2333e"


def test_a_long_running_stage_never_dead_ends_however_long_it_runs():
    """#2019 acceptance: "a genuinely long-running work stage (active=1) does
    NOT escalate, however long it runs."

    "However long" is enforced structurally rather than by a threshold: the
    predicate takes no clock at all (ask 4 — elapsed time must NOT be the
    trigger), so this is byte-for-byte the same call on poll 1 and poll
    10,000. The state is otherwise the full #1956 dead-end shape, so
    `active_count` is carrying the whole decision.
    """
    s = work_tested(
        review_aid="c9b489b2333e",
        review_status="done",
        review_verdict="",
        active_count=1,
        active_types=("work",),
    )
    for _ in range(3):  # identical result, poll after poll after poll
        action = step(s)
        assert action.kind == WAIT
        assert action.exit_code == 0


def test_a_failed_review_worker_is_still_retried_not_dead_ended():
    """Blast-radius bar. #1584's bounded `coord review` re-dispatch is a move
    that genuinely can succeed; a dead end must never steal it."""
    action = step(
        work_tested(
            review_aid="r1", review_status="failed",
            review_failure_reason="529 Overloaded",
        ),
        DriveOptions(machine="precision", max_work_retries=1),
    )
    assert action.kind == RUN
    assert action.command == ("review", "w1")


def test_a_blocked_test_stage_escalates_instead_of_warning_every_poll():
    """#1672 stamps `test_state="blocked"` when no capability-matched machine
    could run the suite, then deliberately never re-probes. Before #2019 this
    fell through `_decide_test` to a bare WAIT carrying an "unexpected
    test_state" warning — a spin with a note attached."""
    action = step(
        done_work(
            work_test_state="blocked",
            work_test_reason="no machine advertises capability 'gtk'",
        )
    )
    assert action.is_exit
    assert action.exit_code == EXIT_DEAD_END
    assert "test_stage_blocked" in action.message
    assert f"coord diagnose {REPO} {ISSUE} --stage test --reset" in action.message


def test_a_dead_end_exit_code_reaches_the_drive_exited_audit_row(
    driver_factory, monkeypatch, coord_db,
):
    """The end-to-end seam, through `Driver.run()`'s audit boundary (#1499).

    `details.exit_code == EXIT_DEAD_END` is the ONE fact
    `coord/commands/drive_queue.py`'s `_fetch_exit_reasons` reads to block the
    queue entry without spending an attempt — everything else about the run
    is gone by the time the tick looks.
    """
    monkeypatch.setattr(
        "coord.drive.Driver._post_escalation_comment", lambda *a, **kw: None
    )
    # Pin `coord_argv()`'s prefix to a single element so the `c[1:3]` slice
    # below is decided by the code under test, not by whether `coord` is on
    # the *host's* $PATH (#2564) — off-PATH, `coord_argv()`'s documented
    # fallback is `[sys.executable, "-m", "coord.cli"]`, three elements, which
    # shifts every recorded argv's ["escalate", "record"] out of [1:3].
    monkeypatch.setenv("COORD_DRIVE_COORD_BIN", "coord")
    payload = board(status="done", test_state="passed")
    payload["assignments"].append({
        "repo_name": REPO,
        "issue_number": ISSUE,
        "type": "review",
        "assignment_id": "c9b489b2333e",
        "review_of_assignment_id": "w1",
        "dispatched_at": 2.0,
        "status": "done",
        "review_verdict": None,
    })
    driver = driver_factory([payload])
    assert driver.run() == EXIT_DEAD_END

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    details = json.loads(rows[1]["details_json"])
    assert details["exit_code"] == EXIT_DEAD_END
    assert "review_terminal_no_verdict" in rows[1]["summary"]
    # ...and the board-visible escalation went out as a `coord` argv, once.
    escalations = [c for c in driver.recorded if c[1:3] == ["escalate", "record"]]
    assert len(escalations) == 1


def test_a_review_worker_that_died_retries_through_the_cli_then_stops_at_the_cap():
    """#1584: the review WORKER itself failed (transient API error, network
    drop, ...) before ever producing a verdict — `review_status="failed"`
    with `review_verdict=""`. Before #1584's reconcile-side fix, this could
    not be told apart from "no review dispatched yet" and silently waited
    out the full 240-minute deadline. Mirrors the WORK failed-retry bounded
    loop, but re-dispatches via `coord review <work_aid>` (NOT `coord retry
    <review_aid>` — that command's `_reassign` hardcodes `type="work"` on
    every re-dispatch and would silently create a bogus work assignment
    instead of a review) up to `max_work_retries`, then dies with the reason.
    """
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1", review_status="failed",
        review_failure_reason="529 Overloaded",
    )

    first = step(s, opts, counters=counters)
    assert first.command == ("review", "w1")
    assert counters.review_retries == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "529 Overloaded" in second.message


def test_a_usage_limit_killed_review_waits_instead_of_retrying_or_dying():
    """#1461/#1584: a review worker killed by the account's usage limit must
    WAIT like the work-side case — retrying before the reset just burns the
    same exhausted budget again."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1", review_status="failed",
        review_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
    )
    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert counters.review_retries == 0
    assert any("usage-limit" in w for w in action.warnings)


def requested_changes(**kw) -> IssueState:
    base = dict(
        review_aid="r1",
        review_verdict="request-changes",
        work_review_iter=1,
        max_review_iterations=5,
    )
    base.update(kw)
    return work_tested(**base)


def test_request_changes_dispatches_coord_fix_against_the_REVIEW_id():
    """#1692: this arm used to `_wait()` on a comment reading "the auto-loop
    dispatches the fix". That stopped being true when #1616 replaced the
    `coord notify` timer with the daemon drain — the drain deliberately
    excludes fix dispatch (#476/#477), `run_for_review_transition` never sees
    a transition the drain already consumed, and the #1478 stalled sweeper is
    off by default. The observed cost was a 50-minute park to the deadline
    with nothing dispatched (drive-batch 2026-08-02, #1630).

    The REVIEW id is the whole point: `coord fix <work_aid>` gates on the
    legacy `smoke_test == "fail"` field and would be refused here; `coord fix
    <review_aid>` is the #1622 door, built for exactly this and never wired up.
    """
    counters = DriveCounters()
    action = step(requested_changes(), counters=counters)
    assert action.kind == RUN
    assert action.command == ("fix", "r1")  # the REVIEW id, never work_aid
    assert counters.fix_rounds == 1
    assert action.on_error == "die"
    assert "--fix-of" in action.error_message  # the manual door, named


def test_the_review_fix_arm_shares_one_fix_budget_with_the_test_arm():
    """Not a parallel counter (#1692): a failing test and a request-changes
    review are two shapes of one loop, so a drive that bounces between them
    spends ONE budget. Two rounds of mixed kinds exhaust --max-fix-rounds 2."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=2)

    first = step(
        done_work(work_test_state="failed", work_test_reason="3 failed"),
        opts, counters=counters,
    )
    assert first.command == ("fix", "w1")  # test arm, round 1

    second = step(requested_changes(), opts, counters=counters)
    assert second.command == ("fix", "r1")  # review arm, round 2
    assert counters.fix_rounds == 2

    # A THIRD round of either kind is over budget.
    exhausted = step(requested_changes(review_aid="r2"), opts, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_TERMINAL_FAILURE
    assert "after 2 fix round(s)" in exhausted.message
    assert "NOT exhausted" in exhausted.message  # says WHICH cap was hit

    still_exhausted = step(
        done_work(work_test_state="failed", work_test_reason="3 failed"),
        opts, counters=counters,
    )
    assert still_exhausted.is_exit


def test_a_second_decide_on_an_unchanged_board_does_not_dispatch_a_second_fix():
    """THE guard against re-opening #476/#477 in a new dispatcher.

    `coord fix` returns as soon as the fix worker is dispatched, but the board
    this driver polls needs a beat to show the new row. Until it does, the
    state is byte-for-byte the one that triggered the dispatch — and a driver
    that re-fires on it puts a SECOND fix worker on the SAME branch. That is
    the #476/#477 incident shape (two uncoordinated dispatchers, conflicting
    branches, real money), and `max_fix_rounds` alone does not prevent it: it
    only decides how many duplicates get spawned before the drive gives up.

    Delete `counters.review_fix_dispatched_for` and this test must fail.
    """
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=3)
    s = requested_changes()  # one board snapshot, reused verbatim

    assert step(s, opts, counters=counters).command == ("fix", "r1")
    assert counters.fix_rounds == 1

    for _ in range(3):
        again = step(s, opts, counters=counters)
        assert again.kind == WAIT, "re-dispatched a duplicate fix worker"
        assert again.command == ()
        assert counters.fix_rounds == 1, "burned a fix round on a no-op"
        assert "already dispatched" in again.label


def test_the_next_review_round_is_a_new_row_so_the_latch_does_not_wedge():
    """The de-dup latch keys on the review's assignment id, and a fix round
    produces a new work row and therefore a new review row (`drive_state.
    project` keys the review on the current work id). So the latch clears
    itself — it must not turn the second genuine round into a permanent wait.
    """
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=3)

    assert step(requested_changes(), opts, counters=counters).command == ("fix", "r1")
    second_round = step(
        requested_changes(review_aid="r2", work_review_iter=2),
        opts,
        counters=counters,
    )
    assert second_round.command == ("fix", "r2")
    assert counters.fix_rounds == 2


def test_an_in_flight_fix_row_parks_on_the_active_guard_not_a_second_dispatch():
    """Once the dispatched fix row DOES appear on the board, `decide()`'s
    `active_count > 0` guard takes over before the review gate is reached."""
    counters = DriveCounters()
    action = step(
        requested_changes(active_count=1, active_types=("work",)), counters=counters
    )
    assert action.kind == WAIT
    assert counters.fix_rounds == 0


def test_request_changes_with_no_review_id_refuses_rather_than_guessing():
    """`review_verdict` and `review_aid` come off the same board row, so this
    is impossible today — assert it anyway. Everything past this point spends
    money keyed on that id, and `coord fix ""` is not a refusal this arm
    should have to interpret."""
    counters = DriveCounters()
    action = step(requested_changes(review_aid=""), counters=counters)
    assert action.is_exit
    assert action.command == ()
    assert counters.fix_rounds == 0


def test_request_changes_with_the_auto_loop_off_reports_and_stops():
    """`coord fix` routes through `auto_loop.process_review_completion`, whose
    first line refuses when `pipeline.auto_loop` is off. Dispatching a
    subprocess that can only fail would report a subprocess error; the
    preflight warning already promises "report the verdict and stop"."""
    counters = DriveCounters()
    action = step(requested_changes(auto_loop=False), counters=counters)
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "auto_loop is OFF" in action.message
    assert counters.fix_rounds == 0


def test_request_changes_stops_when_the_review_fix_loop_is_exhausted():
    action = step(
        work_tested(
            review_aid="r1",
            review_verdict="request-changes",
            work_review_iter=5,
            max_review_iterations=5,
        )
    )
    assert action.is_exit
    assert "fix loop is exhausted" in action.message


def test_max_review_iterations_dies_BEFORE_any_fix_is_dispatched():
    """#1692: the outer cap stays first. With the whole fix budget untouched
    the driver must still refuse — `max_review_iterations` bounds the ISSUE's
    review loop across every drive that ever touches it, and
    `_dispatch_fix_for_review` would refuse this dispatch anyway
    (`next_iteration > max_iter`), turning a clear cap message into an opaque
    subprocess failure. Nothing is spawned and no round is spent."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=99)
    action = step(
        requested_changes(work_review_iter=5, max_review_iterations=5),
        opts,
        counters=counters,
    )
    assert action.is_exit
    assert action.command == ()
    assert counters.fix_rounds == 0
    assert counters.review_fix_dispatched_for == ""
    assert "fix loop is exhausted" in action.message


def test_max_fix_rounds_zero_never_dispatches_a_review_fix():
    """The test arm's `max_fix_rounds=0` guard, mirrored: a drive told to
    spend nothing must spend nothing on the review arm either."""
    counters = DriveCounters()
    action = step(
        requested_changes(),
        DriveOptions(machine="precision", max_fix_rounds=0),
        counters=counters,
    )
    assert action.is_exit
    assert action.command == ()
    assert counters.fix_rounds == 0


def test_interactive_work_requests_its_review_exactly_once_under_force_review():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", force_review=True)
    s = work_tested(work_provider="claude-pty")

    first = step(s, opts, counters=counters)
    assert first.command == ("review", "w1")
    assert counters.review_dispatches == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert "none appeared on the board" in second.message


def test_interactive_work_without_force_review_is_terminal_at_the_review_gate():
    action = step(work_tested(work_provider="claude-pty"))
    assert action.is_exit
    assert "#555" in action.message


def test_an_unexpected_review_verdict_warns_and_waits():
    action = step(work_tested(review_aid="r1", review_verdict="maybe"))
    assert action.kind == WAIT
    assert any("maybe" in w for w in action.warnings)


# ═══════════════════════════════════════════════════════════════════════════
# the MERGE stage
# ═══════════════════════════════════════════════════════════════════════════


def approved_work(**kw) -> IssueState:
    return work_tested(review_aid="r1", review_verdict="approve", **kw)


def test_no_merge_stops_after_the_review_approves():
    action = step(approved_work(), DriveOptions(machine="precision", do_merge=False))
    assert action.is_exit
    assert action.exit_code == EXIT_OK
    assert "coord merge --only w1" in action.message


def test_an_approved_review_merges_through_the_cli_under_the_merge_lock():
    action = step(approved_work())
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert action.serialize_merge is True
    # Tolerant: the first attempt often precedes enqueue_approved_work.
    assert action.on_error == "warn"


def test_the_merge_method_is_honoured():
    action = step(
        approved_work(), DriveOptions(machine="precision", merge_method="squash")
    )
    assert action.command[-1] == "squash"


def test_merge_uses_the_queue_entrys_assignment_id_when_it_differs():
    """A fix chain enqueues under an earlier work row; --only must match it."""
    action = step(approved_work(merge_aid="w0", merge_status=""))
    assert action.command == ("merge", "--only", "w0", "--method", "rebase")


def test_merge_retries_are_bounded():
    """Unbounded merge retries until the deadline was a real bug."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    assert step(s, opts, counters=counters).kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "merge attempted 2 times without landing" in exhausted.message


def test_exhausted_merge_attempts_quotes_the_last_captured_diagnostic():
    """#2078: the give-up message used to echo the board's empty
    `merge_status`/`merge_reason` fields verbatim — 'status=none reason=none',
    carrying no diagnosis at all. `Driver._loop` now captures each `coord
    merge --only` attempt's own output (`_explain_missing_only_entry`'s
    diagnosis) into `counters.last_merge_diagnostic`; the die message must
    quote it instead of just the bare fields."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    # Simulate what Driver._loop does after the first attempt's subprocess
    # returns — captures its combined stdout+stderr, which for a row that
    # cannot be enqueued at all (no board row matches the identifier) reads
    # roughly like this.
    counters.last_merge_diagnostic = (
        "merge-queue: no entry found for 'w1' "
        "(tried assignment_id, repo#issue, issue number, and branch name)\n"
        "  no done work row on the board matches that identifier either — "
        "the identifier did not resolve."
    )
    assert step(s, opts, counters=counters).kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "merge attempted 2 times without landing" in exhausted.message
    assert "no entry found for 'w1'" in exhausted.message
    assert "identifier did not resolve" in exhausted.message


def test_exhausted_merge_attempts_says_so_when_nothing_was_ever_captured():
    """`Driver._loop` failing to wire the capture up at all (or a test
    driving `decide()` directly, with no Driver in the loop) must not print
    a blank diagnostic block — say plainly that nothing was captured."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=1)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "no output captured from the merge attempts" in exhausted.message


# ═══════════════════════════════════════════════════════════════════════════
# #2157: an ALREADY-MERGED entry costs zero attempts
#
# coord-portal#51: the acceptance slice's PR landed 12 seconds into the
# drive's second attempt. Every `coord merge --only` after that exited 1 with
# "entry '84b8207f9660' is in state 'merged' (not PENDING) — cannot merge"
# while the board still projected `status=''`; the driver counted each as a
# failed merge attempt, exhausted the cap, and blocked the drive-queue entry
# (and the `after=`-dependent #55) for 5h47m. A successful merge reported as
# a failure, and the failure hard-blocking the queue.
# ═══════════════════════════════════════════════════════════════════════════

# The two `coord merge --only` wordings that mean "already merged": post-#2157
# (exit 0) and pre-#2157 (exit 1, still emitted by an older installed `coord`).
MERGED_DIAGNOSTIC = (
    "merge-queue: entry 'w1' already merged (PR #60) — nothing to do"
)
LEGACY_MERGED_DIAGNOSTIC = (
    "merge-queue: entry 'w1' is in state 'merged' (not PENDING) — cannot merge"
)


def test_an_already_merged_diagnostic_waits_instead_of_spending_an_attempt():
    """The incident in one assertion: the merge landed, so the next poll must
    not be attempt N+1 of `--max-merge-attempts`."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 1
    counters.last_merge_diagnostic = MERGED_DIAGNOSTIC

    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert not action.is_exit
    assert counters.merge_attempts == 1, "an already-merged entry must cost 0 attempts"
    assert "ALREADY" in action.label
    assert "PR #60" in action.label


def test_an_already_merged_diagnostic_never_exhausts_the_cap():
    """Ten more polls, still no `exhausted` exit — because none of them is an
    attempt. Pre-#2157 the 2nd poll here died with `merge attempted 1 times
    without landing` and the drive-queue entry went `blocked`."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=1)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    counters.last_merge_diagnostic = MERGED_DIAGNOSTIC

    for _ in range(10):
        action = step(s, opts, counters=counters)
        assert action.kind == WAIT, action.message
    assert counters.merge_attempts == 1


def test_the_legacy_not_pending_merged_wording_is_recognised_too():
    """`coord drive` shells out to whatever `coord` is installed on the box,
    so a drive running against a pre-#2157 install must reach the same
    conclusion from the old exit-1 wording — otherwise a version skew
    reintroduces the exact incident."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=1)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    counters.last_merge_diagnostic = LEGACY_MERGED_DIAGNOSTIC

    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert counters.merge_attempts == 1


def test_a_conflict_diagnostic_still_spends_an_attempt():
    """The narrowing is to MERGED only. A CONFLICT entry's identically-shaped
    'not PENDING' refusal must keep counting against the cap — that is the
    #1474 behaviour the attempt-cap comment calls out on purpose."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    counters.last_merge_diagnostic = (
        "merge-queue: entry 'w1' is in state 'conflict' (not PENDING) "
        "— cannot merge"
    )
    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 2


def test_an_already_merged_entry_does_not_escalate_on_a_terminal_status():
    """Checked before the `_RETRYABLE_MERGE_STATUSES` escalation, so a stale
    NEEDS_ATTENTION left on the board by a conflict the merge then resolved
    cannot escalate a merge that landed."""
    counters = DriveCounters(last_merge_diagnostic=MERGED_DIAGNOSTIC)
    action = step(
        approved_work(merge_status="NEEDS_ATTENTION"), counters=counters
    )
    assert action.kind == WAIT
    assert not action.is_exit


def test_an_already_merged_slice_costs_the_slice_budget_nothing():
    """The slice lane goes through the same `_decide_merge`, so #2157 lands
    there too — and the slice lane is where the incident actually happened."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=1)

    assert landing_step(landing_state(), opts, counters=counters).kind == RUN
    assert counters.acceptance is not None
    counters.acceptance.last_merge_diagnostic = (
        "merge-queue: entry 'ta1' already merged (PR #60) — nothing to do"
    )

    action = landing_step(landing_state(), opts, counters=counters)
    assert action.kind == WAIT
    assert not action.is_exit
    assert counters.acceptance.merge_attempts == 1
    assert "ACCEPTANCE/" in action.label


def test_empty_status_with_a_known_gate_block_waits_instead_of_retrying_blind():
    """#2078 core fix: once a prior `coord merge --only` attempt's captured
    diagnostic already names an apparently un-satisfied review/smoke gate, a
    board `merge_status` of "" (no queue entry at all) is no longer treated
    as blindly retryable — it behaves like a BLOCKED entry (wait, re-check),
    the same as if the board itself had rendered BLOCKED with this reason.
    No merge attempt is spent on the FIRST few polls chasing a gate a retry
    is unlikely to change (bounded by #2149 below — it isn't a wait forever)."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    counters.last_merge_diagnostic = (
        "merge-queue: no entry found for 'w1', but 1 done work row(s) on the "
        "board match it:\n"
        "  claude-coordinator #1392 (assignment w1, branch fix-1392) — "
        "enqueue blocked by smoke gate — test verdict stale (recorded "
        "against base 5f34a46, base now ff1bd1f) (waive with --skip-smoke)"
    )
    s = approved_work()

    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert "not yet enqueued" in action.label
    assert "test verdict stale" in action.label
    assert counters.merge_attempts == 0
    assert counters.gate_wait_rounds == 1
    assert "1 check" in action.label


# ═══════════════════════════════════════════════════════════════════════════
# #2149: a cached gate block reason is a snapshot, not live state — the wait
# on it must be bounded and must eventually re-attempt for real, or a gate
# that clears on its own (coord-portal#50: a stale "review required" line
# reprinted ~145 times over 2h33m) is never noticed.
# ═══════════════════════════════════════════════════════════════════════════


def _blocked_diagnostic(gate: str = "review") -> str:
    return (
        "merge-queue: no entry found for 'w1', but 1 done work row(s) on the "
        "board match it:\n"
        "  claude-coordinator #1392 (assignment w1, branch fix-1392) — "
        f"enqueue blocked by {gate} gate — review required but not approved "
        "(waive with --skip-review)"
    )


def test_gate_wait_is_bounded_and_eventually_retries_for_real():
    """The exact regression: a gate reason cached from a past attempt must
    not be waited on forever. After `_MAX_GATE_WAIT_ROUNDS` cheap waits, the
    driver must fall through to a REAL `coord merge --only` attempt instead
    of reprinting the frozen reason again."""
    from coord.drive import _MAX_GATE_WAIT_ROUNDS

    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    counters.last_merge_diagnostic = _blocked_diagnostic()
    s = approved_work()

    for round_ in range(1, _MAX_GATE_WAIT_ROUNDS + 1):
        action = step(s, opts, counters=counters)
        assert action.kind == WAIT, f"round {round_} should still be a cheap wait"
        assert counters.merge_attempts == 0
        assert counters.gate_wait_rounds == round_

    # The next poll must stop waiting and spend a real attempt — this is what
    # notices a gate that has since cleared.
    action = step(s, opts, counters=counters)
    assert action.kind == RUN
    assert counters.merge_attempts == 1
    assert counters.gate_wait_rounds == 0


def test_gate_wait_never_spins_to_the_deadline():
    """coord-portal#50 in one assertion: many, many more polls than the
    incident saw must never all come back WAIT — the drive has to actually
    try the merge again well before any deadline."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    counters.last_merge_diagnostic = _blocked_diagnostic()
    s = approved_work()

    kinds = [step(s, opts, counters=counters).kind for _ in range(20)]
    assert RUN in kinds, "must eventually re-attempt instead of waiting forever"


def test_a_fresh_attempt_after_the_gate_wait_bound_resets_the_round_counter():
    """Once the bounded wait forces a real attempt, and that attempt's fresh
    diagnostic still names the same gate, the driver gets a fresh
    `_MAX_GATE_WAIT_ROUNDS`-sized budget of cheap waits again — a persistent
    but slow-clearing gate degrades to periodic re-checks, not a tight loop
    of real attempts."""
    from coord.drive import _MAX_GATE_WAIT_ROUNDS

    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    counters.last_merge_diagnostic = _blocked_diagnostic()
    s = approved_work()

    for _ in range(_MAX_GATE_WAIT_ROUNDS):
        step(s, opts, counters=counters)
    retry_action = step(s, opts, counters=counters)
    assert retry_action.kind == RUN
    assert counters.merge_attempts == 1

    # Simulate `Driver._loop` capturing the fresh attempt's output — still
    # blocked by the same gate.
    counters.last_merge_diagnostic = _blocked_diagnostic()
    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert counters.gate_wait_rounds == 1


def test_gate_wait_round_bound_lets_a_cleared_gate_merge_without_a_human():
    """The acceptance criterion from #2149, directly: seed a refused `coord
    merge --only`, then have the gate clear (the next real attempt lands),
    and assert the drive proceeds — no operator intervention, no waiting to
    a deadline."""
    from coord.drive import _MAX_GATE_WAIT_ROUNDS

    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    counters.last_merge_diagnostic = _blocked_diagnostic()
    s = approved_work()

    for _ in range(_MAX_GATE_WAIT_ROUNDS):
        action = step(s, opts, counters=counters)
        assert action.kind == WAIT

    # The gate cleared in the meantime — the bounded wait forces a real
    # attempt, which is what notices that.
    retry_action = step(s, opts, counters=counters)
    assert retry_action.kind == RUN
    assert retry_action.command == ("merge", "--only", "w1", "--method", "rebase")


def test_empty_status_still_retries_blind_when_the_diagnostic_names_no_gate():
    """The companion case: when the last captured diagnostic reports that
    every gate already passes (a genuine "not enqueued yet" timing gap) or
    that no board row matched at all, there is nothing to wait on — the
    bounded `--only` retry is still the right move, unchanged from before
    #2078."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    counters.last_merge_diagnostic = (
        "merge-queue: no entry found for 'w1', but 1 done work row(s) on the "
        "board match it:\n"
        "  claude-coordinator #1392 (assignment w1, branch fix-1392) — all "
        "merge gates pass; it was skipped for a non-gate reason (issue "
        "closed, branch missing from origin, or PR already merged), or the "
        "auto-enqueue scan has not run yet."
    )
    s = approved_work()

    action = step(s, opts, counters=counters)
    assert action.kind == RUN
    assert counters.merge_attempts == 1


def test_extract_gate_block_reason_parses_the_explain_missing_only_entry_line():
    from coord.drive import _extract_gate_block_reason

    assert _extract_gate_block_reason("") is None
    assert (
        _extract_gate_block_reason(
            "merge-queue: no entry found for 'w1', but 1 done work row(s) "
            "match it:\n"
            "  repo #1 (assignment w1, branch b) — enqueue blocked by "
            "review gate — review required but not approved "
            "(waive with --skip-review)"
        )
        == "review gate — review required but not approved "
        "(waive with --skip-review)"
    )
    assert (
        _extract_gate_block_reason(
            "  repo #1 (assignment w1, branch b) — all merge gates pass; "
            "it was skipped for a non-gate reason ..."
        )
        is None
    )


def test_human_required_merge_is_terminal_with_the_override_recipe():
    action = step(approved_work(merge_status="HUMAN_REQUIRED", merge_reason="semantic"))
    assert action.is_exit
    assert "--override-human-required" in action.message
    assert "semantic" in action.message


@pytest.mark.parametrize("status", ["HUMAN_REQUIRED", "human_required"])
def test_human_required_is_matched_case_insensitively(status):
    assert step(approved_work(merge_status=status)).is_exit


def test_a_conflict_runs_coord_merge_rather_than_waiting_forever():
    """#1474: `dispatch_conflict_fix` has exactly two sanctioned callers — an
    actual `coord merge` run, and the semantic-escalation variant reachable
    only from `coord resume` (human-invoked). A bare `_wait()` here means
    NOTHING ever dispatches the fix worker — the exact deadlock that stalled
    #1453/#1461 for ~14 hours. The regression test that would have caught
    it: CONFLICT must yield a RUN action (the `coord merge --only <aid>`
    that actually runs `classify_conflict` + `dispatch_conflict_fix`), not a
    WAIT with nothing behind it.
    """
    action = step(approved_work(merge_status="CONFLICT"))
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")


def test_conflict_retries_are_bounded_by_the_same_merge_attempt_cap():
    """CONFLICT falls through to the same bounded retry as every other
    non-terminal merge status — a `coord merge --only` that keeps landing
    back on CONFLICT must still terminate, not spin until the deadline."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="CONFLICT")

    assert step(s, opts, counters=counters).kind == RUN
    assert step(s, opts, counters=counters).kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "merge attempted 2 times without landing" in exhausted.message


def test_a_conflict_with_an_active_conflict_fix_waits_instead_of_re_dispatching():
    """Once a conflict-fix worker is actually dispatched, it is a
    `type="conflict-fix"` row scoped to this same issue — `decide()`'s own
    ``active_count`` gate (checked before the merge stage is ever reached)
    must park the run there, never re-attempt `coord merge --only` while one
    is already in flight. This is what makes the #1474 fix safe: RUN once
    to dispatch, then the board itself — not a flag `_decide_merge` has to
    track — is what prevents a duplicate dispatch on the next poll.
    """
    action = step(
        approved_work(
            merge_status="CONFLICT",
            active_count=1,
            active_types=("conflict-fix",),
        )
    )
    assert action.kind == WAIT


def test_a_blocked_merge_waits_and_reports_the_gate():
    action = step(approved_work(merge_status="BLOCKED", merge_reason="CI running"))
    assert action.kind == WAIT
    assert "CI running" in action.label


# ═══════════════════════════════════════════════════════════════════════════
# #1891: a CI verdict that has not arrived must not consume merge budget —
# checked off `merge_reason` (which falls back to the raw queue row's own
# persisted `error` when the board's live re-evaluation comes back empty),
# NOT off `merge_status` — so this fires regardless of whatever `merge_status`
# happens to read: "", "PENDING", "READY", or "BLOCKED" are all real values
# the board can show for the exact same still-pending checks, depending on
# whether `_gate_refresher`'s periodic snapshot caught up with a live `coord
# merge` attempt's own fresher read. See `coord.merge_queue.CI_PENDING_PREFIX`.
#
# #2814: budget-safe no longer means inert. The wait used to be a bare
# `_wait()` that made no `coord merge` call at all — fine on the HTTP
# `/board` path (the daemon's own `_gate_refresher` tick keeps the board
# fresh underneath it), but `BoardFetcher._fetch_local`'s standalone
# daemon-host path never computes a `merge_plan`, so nothing was ever
# rewriting the persisted `error` once CI actually resolved — a drive could
# park on a byte-identical line for the full 240m deadline, hours after CI
# went green. It is now a real, `--only`-scoped `coord merge` RUN action
# every poll, still exempt from `counters.merge_attempts` (see below), so it
# stays budget-safe while actually re-observing CI each time.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", ["", "PENDING", "READY", "BLOCKED"])
def test_checks_pending_retries_for_real_regardless_of_which_status_the_board_shows(
    status,
):
    """The board can legitimately show any of these for the SAME still-pending
    checks (see the module comment above) — every one of them dispatches the
    same real, attempt-exempt `coord merge --only` re-check, as long as
    `merge_reason` names the pending checks."""
    action = step(
        approved_work(
            merge_status=status,
            merge_reason="CI running: build, lint",
        )
    )
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert action.serialize_merge is True
    assert "CI running: build, lint" in action.label
    assert "not spending an attempt" in action.label


def test_checks_pending_retries_for_real_without_spending_an_attempt():
    """Acceptance (#1891/#2814): `merge_attempts` does not increase across
    several polls while checks remain pending — the exact accounting bug the
    GitHub Actions outage of 2026-08-06 hit: three attempts, then `_die()`,
    for an entry whose checks were merely starved of runners, not failing —
    but each poll still dispatches a real `coord merge --only` RUN (#2814),
    so the persisted `error` (and, once CI resolves, the merge itself) keeps
    getting refreshed instead of freezing on a stale snapshot."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="", merge_reason="CI running: build")

    for _ in range(5):
        action = step(s, opts, counters=counters)
        assert action.kind == RUN
        assert action.command == ("merge", "--only", "w1", "--method", "rebase")
        assert counters.merge_attempts == 0


def test_checks_pending_reason_is_recognised_via_the_shared_predicate_not_ad_hoc_text():
    """Guards against a future edit accidentally narrowing the match to the
    board-render wording only — `process()`'s live `entry.error` uses the
    identical `CI_PENDING_PREFIX`, and both must keep working."""
    from coord.merge_queue import CI_PENDING_PREFIX, is_ci_pending_reason

    assert is_ci_pending_reason(f"{CI_PENDING_PREFIX} build")
    assert not is_ci_pending_reason("checks failed: build (failure)")
    assert not is_ci_pending_reason("")
    assert not is_ci_pending_reason(None)


def test_a_genuinely_failed_check_still_walks_the_bounded_retry_path():
    """Regression guard: this fix must not be readable as "ignore CI
    failures". A `checks_failed` reason — even reaching the drive through the
    SAME empty/PENDING/READY `merge_status` gap `checks_pending` can — is not
    an `is_ci_pending_reason` match, so it falls through unchanged to the
    existing bounded retry (RUN, capped, then exhausted)."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="", merge_reason="checks failed: build (failure)")

    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 1
    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 2
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "merge attempted 2 times without landing" in exhausted.message


def test_a_genuinely_failed_check_reported_as_blocked_still_just_waits_like_before():
    """When the board DOES correctly render `BLOCKED` for a failed check (the
    common case), behaviour is byte-for-byte unchanged from before #1891 —
    same as `test_a_blocked_merge_waits_and_reports_the_gate`, just with the
    CI-failed wording instead of CI-running."""
    action = step(approved_work(merge_status="BLOCKED", merge_reason="CI failed: build (failure)"))
    assert action.kind == WAIT
    assert "CI failed" in action.label


# ═══════════════════════════════════════════════════════════════════════════
# #1892: the sibling case — a CI verdict DID arrive, but every failing check
# carried no verdict about the code (never assigned a runner, or died before
# checkout). `coord merge`'s own live attempt auto-reruns CI for this case;
# the drive must not retry `coord merge` in a way that spends its
# merge-attempt budget on a no-op observation.
#
# #2814: on `BoardFetcher._fetch_local` (the standalone `coord drive` path)
# nothing periodic ever makes that "own live attempt" — see the #2814 module
# comment above `_decide_merge`'s CI-reason block — so this now dispatches
# the same real, attempt-exempt `coord merge --only` re-check every poll
# instead of a bare `_wait()`, exactly like #1891/#2347/#2252.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", ["", "PENDING", "READY", "BLOCKED"])
def test_ci_infra_failure_retries_for_real_regardless_of_which_status_the_board_shows(
    status,
):
    action = step(
        approved_work(
            merge_status=status,
            merge_reason=(
                "CI infra: no-gh-on-path (cancelled) — no verdict about the "
                "code (never assigned a runner, or died before checkout)"
            ),
        )
    )
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert action.serialize_merge is True
    assert "auto-rerunning" in action.label
    assert "not spending an attempt" in action.label


def test_ci_infra_failure_retries_for_real_without_spending_an_attempt():
    """Acceptance (#1892/#2814): a PR whose failures are ALL verdictless does
    not consume a drive merge attempt across several polls, but — unlike the
    old bare `_wait()` — each poll still dispatches a real `coord merge
    --only` RUN, so `ci_infra_reruns` (and the persisted `error`) keep
    advancing on the standalone path instead of freezing on a stale
    snapshot (see the #2814 module comment above `_decide_merge`'s CI-reason
    block)."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(
        merge_status="",
        merge_reason="CI infra: e2e (failure) — no verdict about the code",
    )

    for _ in range(5):
        action = step(s, opts, counters=counters)
        assert action.kind == RUN
        assert action.command == ("merge", "--only", "w1", "--method", "rebase")
        assert counters.merge_attempts == 0


def test_ci_infra_reason_is_recognised_via_the_shared_predicate_not_ad_hoc_text():
    from coord.merge_queue import CI_INFRA_PREFIX, is_ci_infra_reason

    assert is_ci_infra_reason(f"{CI_INFRA_PREFIX} e2e (failure)")
    assert not is_ci_infra_reason("checks failed: build (failure)")
    assert not is_ci_infra_reason("CI running: build")
    assert not is_ci_infra_reason("")
    assert not is_ci_infra_reason(None)


def test_a_genuinely_failed_check_is_not_read_as_ci_infra():
    """Regression guard (acceptance criterion): a PR with ANY genuinely
    failed check must behave exactly as today — a plain 'checks failed: ...'
    reason (no CI_INFRA_PREFIX) still walks the bounded retry path, exactly
    like test_a_genuinely_failed_check_still_walks_the_bounded_retry_path."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(
        merge_status="",
        merge_reason=(
            "checks failed: build (failure) — auto-rerun budget exhausted "
            "(2/2); needs a human"
        ),
    )

    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 1


# ═══════════════════════════════════════════════════════════════════════════
# #2347: a THIRD sibling case, checked right after #1891 — the check-list
# FETCH itself failed (GitHub unreachable: a transient `gh pr checks` HTTP
# 5xx, an auth blip), so there is no CI verdict of ANY shape yet, not even a
# genuine "still running" one. `coord merge`'s own live attempt tracks a
# bounded count of consecutive fetch failures for this and keeps waiting
# either way — there is no CI to rerun and no gate to re-test, only more real
# time (GitHub answering again). The drive must not retry `coord merge` in a
# way that spends its merge-attempt budget on a no-op observation.
#
# #2814: same standalone-path gap as #1891/#1892/#2252 — nothing periodic
# makes that "own live attempt" on `BoardFetcher._fetch_local` (see the
# #2814 module comment above `_decide_merge`'s CI-reason block), so this now
# dispatches the same real, attempt-exempt `coord merge --only` re-check
# every poll instead of a bare `_wait()`.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", ["", "PENDING", "READY", "BLOCKED"])
def test_ci_unreadable_retries_for_real_regardless_of_which_status_the_board_shows(
    status,
):
    action = step(
        approved_work(
            merge_status=status,
            merge_reason=(
                "CI unreadable: coord: could not read CI status for "
                "acme/api#99 (HTTP 503) (unknown) — GitHub could not be "
                "reached to read CI status; this is not a CI result"
            ),
        )
    )
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert action.serialize_merge is True
    assert "GitHub could not be reached" in action.label
    assert "not spending an attempt" in action.label


def test_ci_unreadable_retries_for_real_without_spending_an_attempt():
    """Acceptance (#2347/#2814): a PR whose CI status could not be read from
    GitHub does not consume a drive merge attempt across several polls, but
    each poll still dispatches a real `coord merge --only` RUN — mirroring
    the identical #1891/#1892/#2252 guarantees, so `ci_unreadable_reruns`
    (and the persisted `error`) keep advancing instead of freezing on a
    stale snapshot on the standalone path."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(
        merge_status="",
        merge_reason=(
            "CI unreadable: coord: could not read CI status for "
            "acme/api#99 (HTTP 503) (unknown) — GitHub could not be "
            "reached to read CI status; this is not a CI result"
        ),
    )

    for _ in range(5):
        action = step(s, opts, counters=counters)
        assert action.kind == RUN
        assert action.command == ("merge", "--only", "w1", "--method", "rebase")
        assert counters.merge_attempts == 0


def test_ci_unreadable_reason_is_recognised_via_the_shared_predicate_not_ad_hoc_text():
    from coord.merge_queue import CI_UNREADABLE_PREFIX, is_ci_unreadable_reason

    assert is_ci_unreadable_reason(f"{CI_UNREADABLE_PREFIX} build (unknown)")
    assert not is_ci_unreadable_reason("checks failed: build (failure)")
    assert not is_ci_unreadable_reason("CI running: build")
    assert not is_ci_unreadable_reason("CI infra: build (cancelled)")
    assert not is_ci_unreadable_reason("")
    assert not is_ci_unreadable_reason(None)


def test_a_genuinely_failed_check_is_not_read_as_ci_unreadable():
    """Regression guard: a plain 'checks failed: ...' reason (no
    CI_UNREADABLE_PREFIX) still walks the bounded retry path, exactly like
    test_a_genuinely_failed_check_still_walks_the_bounded_retry_path."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(
        merge_status="",
        merge_reason="checks failed: build (failure)",
    )

    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 1


# ═══════════════════════════════════════════════════════════════════════════
# #2704: the branch head itself could not be read (GitHub unreachable, `gh`
# unauthenticated, or a rate limit) — `coord.merge_queue.
# UNKNOWN_BRANCH_HEAD_REASON`. Before #2704 this fabricated a "review
# required but not approved" refusal (fails CLOSED with the WRONG reason) or
# a silently-passing smoke gate (fails OPEN). Now it is its own gate kind:
# the drive waits for GitHub to answer again rather than retry `coord merge`
# (a no-op — nothing about GitHub's reachability changes by re-running it) or
# escalate a re-review/Test re-run for a gate nothing here actually refused.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", ["", "PENDING", "READY", "BLOCKED"])
def test_unknown_branch_head_waits_regardless_of_which_status_the_board_shows(status):
    from coord.merge_queue import UNKNOWN_BRANCH_HEAD_REASON

    action = step(
        approved_work(merge_status=status, merge_reason=UNKNOWN_BRANCH_HEAD_REASON)
    )
    assert action.kind == WAIT
    assert "branch head unknown" in action.label
    assert "not retrying" in action.label


def test_unknown_branch_head_never_spends_an_attempt():
    from coord.merge_queue import UNKNOWN_BRANCH_HEAD_REASON

    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="", merge_reason=UNKNOWN_BRANCH_HEAD_REASON)

    for _ in range(5):
        action = step(s, opts, counters=counters)
        assert action.kind == WAIT
        assert counters.merge_attempts == 0


def test_unknown_branch_head_does_not_escalate_a_fabricated_review_refusal():
    """#2704's core repro: the driver's OWN cached view already shows
    `review_verdict='approve'` (`approved_work`'s default) — exactly the
    incident's shape, where the approval WAS for the current head and
    `coord merge` simply couldn't confirm it. Before the fix this reason
    would have been classified as the "review" gate kind, and — because the
    driver's view contradicts a plain "review" refusal — escalated via
    `_merge_gate_divergence` proposing `coord review-reaffirm` for a review
    nothing actually refused. It must instead be its own kind and just
    wait."""
    from coord.merge_queue import UNKNOWN_BRANCH_HEAD_REASON

    action = step(
        approved_work(merge_status="BLOCKED", merge_reason=UNKNOWN_BRANCH_HEAD_REASON)
    )
    assert action.kind == WAIT
    assert not action.is_exit


def test_merge_gate_kind_recognises_unknown_branch_head_as_its_own_kind():
    from coord.drive import _merge_gate_kind
    from coord.merge_queue import UNKNOWN_BRANCH_HEAD_REASON

    assert _merge_gate_kind(UNKNOWN_BRANCH_HEAD_REASON) == "unknown_head"
    # Regression guard: must never be swallowed into "review" just because
    # `merge_gate_failures` reports it under `gate="review"`.
    assert _merge_gate_kind(UNKNOWN_BRANCH_HEAD_REASON) != "review"


# ═══════════════════════════════════════════════════════════════════════════
# #2947 (follow-up to #2687): the UAT gate. `coord/merge_queue.py` reports a
# missing/failed UAT verdict with the same `MergeGateFailure` shape as smoke
# and review, but before this fix `_merge_gate_kind` had no marker for it —
# every UAT block fell through the "real, persistent gate, wait cheaply" arm
# in `_effective_merge_gate_reason`, and the drive spent real `coord merge`
# attempts against a gate only a human can clear via `coord uat <id>
# --passed`, hit `max_merge_attempts`, and died with a terminal `blocked`
# drive-queue entry nothing re-evaluates.
# ═══════════════════════════════════════════════════════════════════════════

UAT_VERDICT_MISSING = (
    "uat verdict missing — preview: https://pr-123.natal-chart.pages.dev "
    "— run: coord uat w1 --passed|--failed"
)
UAT_VERDICT_FAILED = (
    "uat verdict FAILED: layout broke on mobile — preview: "
    "https://pr-123.natal-chart.pages.dev — run: coord uat w1 --passed|--failed"
)


def test_merge_gate_kind_recognises_uat_as_its_own_kind():
    from coord.drive import _merge_gate_kind

    assert _merge_gate_kind(UAT_VERDICT_MISSING) == "uat"
    assert _merge_gate_kind(UAT_VERDICT_FAILED) == "uat"
    # Regression guard: must never be swallowed into "review"/"smoke" — a UAT
    # block names neither.
    assert _merge_gate_kind(UAT_VERDICT_MISSING) not in ("review", "smoke")


@pytest.mark.parametrize("status", ["", "PENDING", "READY", "BLOCKED"])
def test_uat_gate_waits_regardless_of_which_status_the_board_shows(status):
    action = step(approved_work(merge_status=status, merge_reason=UAT_VERDICT_MISSING))
    assert action.kind == WAIT
    assert "UAT" in action.label
    assert "not retrying" in action.label
    # #2687: the exact clearing command and the resolved preview URL must
    # reach this driver's own STATUS:/coord status surface verbatim, not be
    # summarised away.
    assert "coord uat w1 --passed|--failed" in action.label
    assert "https://pr-123.natal-chart.pages.dev" in action.label


def test_uat_gate_never_spends_a_merge_attempt():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="", merge_reason=UAT_VERDICT_MISSING)

    for _ in range(5):
        action = step(s, opts, counters=counters)
        assert action.kind == WAIT
        assert counters.merge_attempts == 0


def test_uat_gate_does_not_escalate_a_fabricated_review_or_smoke_refusal():
    """The driver's own cached view already shows `review_verdict='approve'`
    and `work_test_state='passed'` (`approved_work`'s defaults). A UAT block
    must not be misread as a smoke/review divergence and escalate a
    re-test/re-review for a gate that was never actually about either —
    `_merge_gate_divergence` deliberately has no `"uat"` arm, so this only
    stays true if `_decide_merge` intercepts `"uat"` before that check runs."""
    action = step(approved_work(merge_status="BLOCKED", merge_reason=UAT_VERDICT_FAILED))
    assert action.kind == WAIT
    assert not action.is_exit


def test_uat_gate_reached_via_the_diagnostic_fallback_still_waits():
    """#2229 shape: the board's live reason names no gate (`merge_status=
    'READY'`, `merge_reason=''`), but this driver's OWN last `coord merge
    --only` attempt already captured the UAT refusal verbatim. Must still
    classify and wait, not fall into the bounded retry that burns attempts —
    mirroring `test_stale_smoke_only_in_the_captured_diagnostic_takes_the_
    retest_arm`'s shape, but for a gate with no automated retest arm at all."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="READY", merge_reason="")
    counters.last_merge_diagnostic = (
        f"  gate uat: {UAT_VERDICT_MISSING} — will block this merge\n"
    )

    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert "UAT" in action.label
    assert counters.merge_attempts == 0


# ═══════════════════════════════════════════════════════════════════════════
# #2252: the OTHER sibling case — a CI verdict DID arrive AND said something
# real about the code, but `coord merge`'s own live attempt has only
# observed it fail ONCE so far and is already re-running the failed job(s)
# to rule out a flake before spending a drive attempt. The drive must not
# retry `coord merge` in a way that spends its merge-attempt budget on a
# no-op observation.
#
# #2814: same standalone-path gap as #1891/#1892/#2347 — nothing periodic
# makes that "own live attempt" on `BoardFetcher._fetch_local` (see the
# #2814 module comment above `_decide_merge`'s CI-reason block), so this now
# dispatches the same real, attempt-exempt `coord merge --only` re-check
# every poll instead of a bare `_wait()`.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", ["", "PENDING", "READY", "BLOCKED"])
def test_ci_flaky_recheck_retries_for_real_regardless_of_which_status_the_board_shows(
    status,
):
    action = step(
        approved_work(
            merge_status=status,
            merge_reason=(
                "CI re-checking: build (failure) — re-running once before "
                "treating as broken (1/1, #2252)"
            ),
        )
    )
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert action.serialize_merge is True
    assert "rule out a flake" in action.label
    assert "not spending an attempt" in action.label


def test_ci_flaky_recheck_retries_for_real_without_spending_an_attempt():
    """Acceptance (#2252/#2814): a PR whose one re-check is still pending an
    answer does not consume a drive merge attempt across several polls, but
    each poll still dispatches a real `coord merge --only` RUN — mirroring
    the identical #1891/#1892/#2347 guarantees, so `ci_flaky_reruns` (and
    the persisted `error`) keep advancing instead of freezing on a stale
    snapshot on the standalone path."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(
        merge_status="",
        merge_reason="CI re-checking: build (failure) — re-running once (1/1, #2252)",
    )

    for _ in range(5):
        action = step(s, opts, counters=counters)
        assert action.kind == RUN
        assert action.command == ("merge", "--only", "w1", "--method", "rebase")
        assert counters.merge_attempts == 0


def test_ci_flaky_reason_is_recognised_via_the_shared_predicate_not_ad_hoc_text():
    from coord.merge_queue import CI_FLAKY_PREFIX, is_ci_flaky_reason

    assert is_ci_flaky_reason(f"{CI_FLAKY_PREFIX} build (failure)")
    assert not is_ci_flaky_reason("checks failed: build (failure)")
    assert not is_ci_flaky_reason("CI running: build")
    assert not is_ci_flaky_reason("CI infra: build (cancelled)")
    assert not is_ci_flaky_reason("")
    assert not is_ci_flaky_reason(None)


def test_a_confirmed_failure_after_the_recheck_walks_the_bounded_retry_path():
    """Regression guard (acceptance criterion): a check that fails TWICE
    behaves exactly as today once #2252's one re-check is exhausted — a
    plain 'checks failed: ...' reason (no CI_FLAKY_PREFIX) still walks the
    bounded retry path."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(
        merge_status="",
        merge_reason="checks failed: build (failure)",
    )

    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 1


# ── #1505: escalate on a status retrying can't fix ──────────────────────────


def test_needs_attention_escalates_on_the_first_encounter_instead_of_retrying():
    """The #1477 bug: NEEDS_ATTENTION used to fall through to the same
    bounded retry as PENDING/CONFLICT, burning the whole merge-attempt
    budget on a status no retry could ever change. It must escalate
    immediately — `counters.merge_attempts` never even increments."""
    counters = DriveCounters()
    action = step(approved_work(merge_status="NEEDS_ATTENTION"), counters=counters)
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.merge_attempts == 0
    assert action.command[:2] == ("escalate", "record")
    assert "NEEDS_ATTENTION" in action.message


def test_an_unrecognised_merge_status_also_escalates_rather_than_spinning():
    """Acceptance: "a driver reaching NEEDS_ATTENTION (or an unrecognised
    merge status) escalates" — not just the one named value."""
    action = step(approved_work(merge_status="SOME_FUTURE_STATUS"))
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED


def test_escalation_command_proposes_the_gh_pr_merge_recipe_when_a_pr_is_known():
    """Mirrors the #1477 resolution this issue was opened over: `gh pr merge
    --rebase` + `coord reconcile-merges` when a PR number is on the board."""
    action = step(
        approved_work(
            merge_status="NEEDS_ATTENTION",
            merge_pr_url="https://github.com/john/claude-coordinator/pull/1496",
        )
    )
    command_str = " ".join(action.command)
    assert "--command" in action.command
    idx = action.command.index("--command")
    assert action.command[idx + 1] == "gh pr merge 1496 --rebase && coord reconcile-merges"
    assert "--gate" in action.command
    assert "pr_url=https://github.com/john/claude-coordinator/pull/1496" in command_str


def test_escalation_command_falls_back_to_the_plan_view_with_no_known_pr():
    action = step(approved_work(merge_status="NEEDS_ATTENTION", merge_pr_url=""))
    idx = action.command.index("--command")
    assert "coord merge --plan --repo" in action.command[idx + 1]


def test_escalation_carries_the_assignment_id_and_gate_readings():
    action = step(
        approved_work(
            merge_status="NEEDS_ATTENTION",
            merge_reason="review not approved",
        )
    )
    assert "--assignment" in action.command
    idx = action.command.index("--assignment")
    assert action.command[idx + 1] == "w1"
    command_str = " ".join(action.command)
    assert "merge_reason=review not approved" in command_str
    assert "review_verdict=approve" in command_str


def test_a_conflict_status_still_retries_rather_than_escalating():
    """CONFLICT keeps its own #1474 dispatch path — it must NOT be swept
    into the new escalate branch alongside NEEDS_ATTENTION."""
    action = step(approved_work(merge_status="CONFLICT"))
    assert action.kind == RUN


def test_the_escalate_branch_runs_before_the_attempt_cap_is_checked():
    """Even with the cap already exhausted, NEEDS_ATTENTION escalates
    (distinct message/exit code) rather than reporting a generic
    'merge attempted N times' exhaustion."""
    counters = DriveCounters(merge_attempts=5)
    opts = DriveOptions(machine="precision", max_merge_attempts=1)
    action = step(approved_work(merge_status="NEEDS_ATTENTION"), opts, counters=counters)
    assert action.exit_code == EXIT_ESCALATED
    assert "attempted" not in action.message


# ═══════════════════════════════════════════════════════════════════════════
# #1526: driver/gate divergence — `coord merge`'s own reason overrides a
# stale-green `work_test_state`/`review_verdict` reading instead of being
# retried against blind.
# ═══════════════════════════════════════════════════════════════════════════


def test_smoke_required_with_a_passed_test_state_escalates_instead_of_retrying():
    """#1526 instance 1 (#1412): board shows test=passed, but `coord merge`
    left 'smoke test required but no verdict recorded' as the merge_reason
    (`merge_queue.process()`'s wording when `has_smoke_verdict` fails closed
    on a fresher check than `work_test_state` reflects). Retrying `coord
    merge --only` unchanged reproduces the identical refusal every time — the
    driver must name the gate and stop, not spend the retry budget on it.
    """
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="smoke test required but no verdict recorded",
        ),
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.merge_attempts == 0  # never even tried the doomed merge
    assert "smoke" in action.message.lower()
    assert "coord test w1 --passed" in " ".join(action.command)


def test_smoke_gate_agreeing_with_a_missing_verdict_still_retries():
    """Sanity check for the divergence gate: when `work_test_state` is
    genuinely blank (no verdict at all — the OTHER, non-divergent way to see
    a smoke_required reason), `_decide_test` — called earlier in `decide()`
    — already parks the run on a wait; `_decide_merge` is never even
    reached, so this never becomes an escalate-vs-retry question at all."""
    action = step(done_work(work_test_state=""))
    assert action.kind == WAIT


def test_stale_smoke_divergence_redispatches_the_test_stage_instead_of_escalating():
    """#1738: unlike the "missing verdict" divergence above, a STALE verdict
    (recorded, but against a base/branch that has since moved) has a safe,
    bounded self-service fix — re-run the Test stage — so the very first
    encounter must NOT escalate; it must clear the verdict via `coord
    diagnose --stage test --reset` (which `dispatch_pending_smoke` then
    re-dispatches on its own next tick)."""
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason=(
                "smoke test verdict is stale: recorded against base "
                "23acfbb, base is now b263929 — re-verify against the "
                "current base, then `coord test w1 --passed`"
            ),
        ),
        counters=counters,
    )
    assert action.kind == RUN
    assert not action.is_exit
    assert action.command == (
        "diagnose", REPO, str(ISSUE), "--stage", "test", "--reset",
    )
    assert counters.fix_rounds == 1
    assert counters.merge_attempts == 0  # never even tried the doomed merge


def test_stale_smoke_divergence_also_matches_the_plan_wording():
    """`merge_queue.plan()`'s board-render wording ("test verdict stale
    (...)") names the identical stale-verdict case in different words — same
    remedy."""
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="BLOCKED",
            merge_reason="test verdict stale (base moved b263929)",
        ),
        counters=counters,
    )
    assert action.kind == RUN
    assert action.command == (
        "diagnose", REPO, str(ISSUE), "--stage", "test", "--reset",
    )


def test_stale_smoke_redispatch_is_bounded_by_max_fix_rounds():
    """The re-test arm shares the SAME `fix_rounds` budget as the test-failed
    and review-fix arms (#1738) — it must converge to an escalation, not spin
    forever, if the verdict keeps going stale."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=2)
    s = approved_work(
        merge_status="READY",
        merge_reason="smoke test verdict is stale: recorded against base X, base is now Y",
    )

    first = step(s, opts, counters=counters)
    assert first.kind == RUN
    second = step(s, opts, counters=counters)
    assert second.kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_ESCALATED
    assert "smoke" in exhausted.message.lower()


def test_stale_smoke_redispatch_respects_max_fix_rounds_zero():
    """A drive told to spend zero fix rounds escalates on the very first
    stale-smoke encounter rather than dispatching a re-test it isn't allowed
    to spend budget on."""
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="smoke test verdict is stale: recorded against base X, base is now Y",
        ),
        DriveOptions(machine="precision", max_fix_rounds=0),
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED


def test_missing_smoke_verdict_divergence_still_escalates_immediately():
    """Restates the #1526 "missing verdict" case side by side with the new
    #1738 "stale verdict" arm above so the two can't silently drift onto the
    same (wrong) behaviour — only staleness gets the automated re-test."""
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="smoke test required but no verdict recorded",
        ),
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.fix_rounds == 0


def test_review_required_with_an_approved_verdict_escalates_instead_of_retrying():
    """#1526 instance 2 (#1483): board shows review=approve, but a rebase
    onto a moved `main` correctly voided the approval (#1475's patch-id
    gate) — `coord merge` leaves 'review required but not approved' as the
    merge_reason. Retrying cannot reconcile the two readings; the driver
    must name the gate and propose the safe corrective action (a scoped
    reaffirm or a full re-review) instead of burning the merge-attempt
    budget three times over.
    """
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="review required but not approved",
        ),
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.merge_attempts == 0
    assert "review" in action.message.lower()
    command_str = " ".join(action.command)
    assert "review-reaffirm w1" in command_str
    assert "coord review w1" in command_str


def test_divergence_is_named_even_when_plans_own_gate_check_already_blocked():
    """The divergence can hide behind BLOCKED too — when `merge_queue.
    plan()`'s OWN render-time gate check caught the same disagreement (see
    `_entry_gate_status`) — not only behind a nominally-retryable status
    like READY. Either way this must escalate, never fall into the passive
    `_wait()` the plain BLOCKED branch uses for a merge-unrelated reason
    like 'CI running' (see `test_a_blocked_merge_waits_and_reports_the_gate`).
    """
    action = step(
        approved_work(
            merge_status="BLOCKED",
            merge_reason="test verdict missing",
        )
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED


def test_two_identical_merge_refusals_in_a_row_escalate_without_a_third_attempt():
    """#1526 black-box scenario (c): simulates the actual polling sequence —
    attempt 1 runs (merge_reason is still empty going in, so the divergence
    can't be seen yet), then `coord merge` leaves its refusal on the board.
    The SECOND `decide()` call, reading that refusal back against an
    unchanged 'passed' test_state, must escalate rather than spend a second
    (of three) attempts retrying the identical command."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)

    # Poll 1: nothing has run yet — merge_reason is empty, so there is
    # nothing to diverge from. A real attempt is still the right call.
    first = step(
        approved_work(merge_status="READY", merge_reason=""),
        opts,
        counters=counters,
    )
    assert first.kind == RUN
    assert counters.merge_attempts == 1

    # Poll 2: that attempt's own refusal is now on the board, and it
    # contradicts this same state's work_test_state="passed" — escalate
    # instead of burning attempt 2 (or, worse, all the way to 3).
    second = step(
        approved_work(
            merge_status="READY",
            merge_reason="smoke test required but no verdict recorded",
        ),
        opts,
        counters=counters,
    )
    assert second.is_exit
    assert second.exit_code == EXIT_ESCALATED
    assert counters.merge_attempts == 1  # unchanged — no second attempt spent


def test_merge_gate_kind_recognises_both_process_and_plan_wordings():
    from coord.drive import _merge_gate_kind

    assert _merge_gate_kind("smoke test required but no verdict recorded") == "smoke"
    assert _merge_gate_kind("test verdict missing") == "smoke"
    assert _merge_gate_kind("review required but not approved") == "review"
    assert _merge_gate_kind("review not approved") == "review"
    assert _merge_gate_kind("checks failed: build (failure)") is None
    assert _merge_gate_kind("") is None


def test_is_stale_smoke_reason_distinguishes_stale_from_missing():
    """#1738: the narrower predicate the re-test arm gates on — only the two
    STALE wordings qualify; "no verdict at all" wordings (still `_merge_gate_
    kind`'s "smoke") must not."""
    from coord.drive import _is_stale_smoke_reason

    assert _is_stale_smoke_reason(
        "smoke test verdict is stale: recorded against base X, base is now Y"
    )
    assert _is_stale_smoke_reason("test verdict stale (base moved)")
    assert not _is_stale_smoke_reason("smoke test required but no verdict recorded")
    assert not _is_stale_smoke_reason("test verdict missing")
    assert not _is_stale_smoke_reason("review required but not approved")
    assert not _is_stale_smoke_reason("")
    assert not _is_stale_smoke_reason(None)


# ═══════════════════════════════════════════════════════════════════════════
# #2229: the driver captured the exact merge refusal, printed it into its own
# death message, and never classified it.
#
# quadraui#309 sat blocked for 11h on a merge that landed first try by hand.
# `merge_queue.plan()` reported READY with NO reason (`_entry_gate_status`
# re-derives freshness at board-build time and degrades to a no-op without
# live SHA data — the #1640 door / #1566 "plan says ready, --only refuses"
# split), so `_merge_gate_divergence` had nothing to classify. Meanwhile every
# `coord merge --only` attempt captured "smoke test verdict is stale" into
# `counters.last_merge_diagnostic` — the exact string the #1738 auto-repair
# arm keys on — and used it for nothing but the give-up message.
#
# #2078's diagnostic fallback did not cover this: it lives inside the
# `status == ""` arm (#309's entry WAS enqueued) and matches only the
# not-yet-enqueued `enqueue blocked by <gate>` wording.
# ═══════════════════════════════════════════════════════════════════════════


def _stale_smoke_diagnostic() -> str:
    """The quadraui#309 diagnostic, verbatim in shape: `coord merge --only`'s
    own pre-`process()` gate line plus the `smoke_required` event."""
    return (
        "  gate smoke: test verdict stale (recorded against base f6216e4, "
        "base now c50e30c) — will block this merge\n"
        "  quadraui #309 (issue-309-drive-queue-bot): smoke_required — smoke "
        "test verdict is stale: recorded against base f6216e4, base is now "
        "c50e30c — re-verify against the current base, then `coord test "
        "41ae4f893239 --passed`"
    )


def _missing_smoke_diagnostic() -> str:
    return (
        "  gate smoke: smoke test required but no verdict recorded — will "
        "block this merge"
    )


def test_stale_smoke_only_in_the_captured_diagnostic_takes_the_retest_arm():
    """THE #2229 regression test. Board: `merge_status='READY'`,
    `merge_reason=''` — nothing to classify. Diagnostic: this driver's own
    last `coord merge --only`, saying the smoke verdict is stale. The #1738
    re-test must arm off that, instead of the three blind retries that ended
    in `exit_code=1` and an 11h stall."""
    counters = DriveCounters(last_merge_diagnostic=_stale_smoke_diagnostic())
    action = step(
        approved_work(merge_status="READY", merge_reason=""),
        counters=counters,
    )
    assert action.kind == RUN
    assert action.command == (
        "diagnose", REPO, str(ISSUE), "--stage", "test", "--reset",
    )
    assert counters.fix_rounds == 1
    assert counters.merge_attempts == 0  # no blind retry spent


def test_missing_verdict_in_the_captured_diagnostic_escalates_and_never_repairs():
    """`_STALE_SMOKE_MARKERS` stays a STRICT subset of `_SMOKE_GATE_MARKERS`
    on the diagnostic path too: a MISSING verdict is the #1640 lost-write
    shape, which a re-test cannot safely paper over. It escalates to a human
    on first encounter, exactly as it does off a board reason."""
    counters = DriveCounters(last_merge_diagnostic=_missing_smoke_diagnostic())
    action = step(
        approved_work(merge_status="READY", merge_reason=""),
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.fix_rounds == 0
    assert "smoke" in action.message.lower()


def test_diagnostic_derived_escalation_quotes_the_recovered_reason():
    """An escalation that says `reports ''` is unactionable — the whole #2229
    complaint. The recovered gate text has to reach the recorded reason."""
    action = step(
        approved_work(merge_status="READY", merge_reason=""),
        counters=DriveCounters(last_merge_diagnostic=_missing_smoke_diagnostic()),
    )
    assert action.is_exit
    assert "smoke test required but no verdict recorded" in " ".join(action.command)


def test_diagnostic_derived_retest_fires_once_per_captured_snapshot():
    """#2149's lesson as a latch: the diagnostic is a SNAPSHOT that nothing
    but a real attempt refreshes. `coord diagnose --stage test --reset` clears
    `test_state`, but the board needs a beat to show it — until then the state
    is byte-for-byte identical. One re-test per snapshot, then a REAL attempt
    has to re-validate; otherwise a lagging board burns the whole
    `max_fix_rounds` budget off one frozen line."""
    counters = DriveCounters(last_merge_diagnostic=_stale_smoke_diagnostic())
    opts = DriveOptions(machine="precision", max_fix_rounds=3)
    s = approved_work(merge_status="READY", merge_reason="")

    first = step(s, opts, counters=counters)
    assert first.command[0] == "diagnose"
    assert counters.fix_rounds == 1

    # Same frozen diagnostic, same board — must NOT spend a second fix round.
    second = step(s, opts, counters=counters)
    assert second.kind == RUN
    assert second.command == ("merge", "--only", "w1", "--method", "rebase")
    assert counters.fix_rounds == 1
    assert counters.merge_attempts == 1


def test_diagnostic_derived_retest_is_bounded_by_max_fix_rounds():
    """The re-test arm shares the same `fix_rounds` budget when it arms off
    the diagnostic — a board that never catches up must converge on an
    escalation, never spin to the deadline."""
    counters = DriveCounters(last_merge_diagnostic=_stale_smoke_diagnostic())
    opts = DriveOptions(machine="precision", max_fix_rounds=2, max_merge_attempts=99)
    s = approved_work(merge_status="READY", merge_reason="")

    actions = []
    for _ in range(12):
        action = step(s, opts, counters=counters)
        actions.append(action)
        if action.is_exit:
            break

    assert actions[-1].is_exit
    assert actions[-1].exit_code == EXIT_ESCALATED
    assert counters.fix_rounds == 2
    retests = [a for a in actions if a.command and a.command[0] == "diagnose"]
    assert len(retests) == 2


def test_a_board_reason_still_wins_over_the_captured_diagnostic():
    """The fallback is a fallback: `merge_reason` is live and re-read every
    poll, the diagnostic is only as fresh as the last attempt. When the board
    names a gate, that is what gets classified."""
    counters = DriveCounters(last_merge_diagnostic=_stale_smoke_diagnostic())
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="review required but not approved",
        ),
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert "review-reaffirm w1" in " ".join(action.command)
    assert counters.fix_rounds == 0


def test_an_enqueued_status_with_no_gate_in_the_diagnostic_still_retries_blind():
    """The companion case: an enqueued entry whose captured diagnostic names
    no gate at all has nothing to classify — the bounded `--only` retry is
    still the right move, unchanged."""
    counters = DriveCounters(
        last_merge_diagnostic="merge-queue: 1 entry processed; 0 merged"
    )
    action = step(
        approved_work(merge_status="READY", merge_reason=""),
        counters=counters,
    )
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert counters.merge_attempts == 1


def test_the_first_merge_poll_never_classifies_off_an_empty_diagnostic():
    """Before any attempt has run there is no diagnostic, so #2229 can never
    escalate (or re-test) a merge this drive has not even tried once."""
    counters = DriveCounters()
    action = step(
        approved_work(merge_status="READY", merge_reason=""), counters=counters
    )
    assert action.kind == RUN
    assert action.command[0] == "merge"


def test_the_empty_status_enqueue_blocked_path_is_unchanged_by_2229():
    """`status == ""` is #2078's shape and keeps #2078's bounded-wait arm:
    "no queue entry at all" is a different fact from "enqueued and refused",
    and the cheap wait is what lets a not-yet-enqueued row self-heal without
    spending a re-test on it."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)
    counters.last_merge_diagnostic = (
        "merge-queue: no entry found for 'w1', but 1 done work row(s) on the "
        "board match it:\n"
        "  claude-coordinator #1392 (assignment w1, branch fix-1392) — "
        "enqueue blocked by smoke gate — test verdict stale (recorded "
        "against base 5f34a46, base now ff1bd1f) (waive with --skip-smoke)"
    )
    action = step(approved_work(merge_status=""), opts, counters=counters)
    assert action.kind == WAIT
    assert "not yet enqueued" in action.label
    assert counters.fix_rounds == 0
    assert counters.gate_wait_rounds == 1


def test_a_live_ci_wait_still_wins_over_a_captured_gate_line():
    """#1891/#1892's CI-reason checks sit right after the divergence check
    and must keep winning: `merge_reason` carries a live, recognised signal
    whose only resolution is more real time. A snapshot from an older
    attempt must not divert the drive into a re-test while CI is still
    reporting — #2814's attempt-exempt `coord merge --only` re-check (not a
    stale-smoke re-test dispatch) is the correct arm to fire instead."""
    counters = DriveCounters(last_merge_diagnostic=_stale_smoke_diagnostic())
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="CI running: build, windows",
        ),
        counters=counters,
    )
    assert action.kind == RUN
    assert action.command[0] == "merge"
    assert counters.fix_rounds == 0
    assert counters.merge_attempts == 0


def test_a_live_ci_unreadable_wait_still_wins_over_a_captured_gate_line():
    """#2347's re-check (#2814: now a real attempt-exempt `coord merge
    --only`, not a bare wait) gets the SAME treatment as #1891/#1892 above: a
    snapshot from an older attempt must not divert the drive into a re-test
    while GitHub itself could not be reached to read CI status."""
    counters = DriveCounters(last_merge_diagnostic=_stale_smoke_diagnostic())
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="CI unreadable: coord: could not read CI status (unknown)",
        ),
        counters=counters,
    )
    assert action.kind == RUN
    assert counters.fix_rounds == 0
    assert counters.merge_attempts == 0


def test_a_conflict_status_still_reaches_its_own_conflict_fix_dispatch():
    """CONFLICT is a live merge-mechanics block with its own resolution path
    (#1474/#241, reached through the bounded `--only` retry). A re-test does
    not rebase a branch — diverting to one is this issue's stall, inverted."""
    counters = DriveCounters(last_merge_diagnostic=_stale_smoke_diagnostic())
    action = step(
        approved_work(merge_status="CONFLICT", merge_reason=""),
        counters=counters,
    )
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert counters.fix_rounds == 0


def test_extract_gate_refusal_reason_reads_either_wording():
    from coord.drive import _extract_gate_refusal_reason

    assert "test verdict stale" in _extract_gate_refusal_reason(
        _stale_smoke_diagnostic()
    )
    assert _extract_gate_refusal_reason(
        "  gate review: review required but not approved — will block this merge"
    ) == "gate review: review required but not approved — will block this merge"
    # No gate named — nothing to classify.
    assert _extract_gate_refusal_reason("merge-queue: 1 merged") == ""
    assert _extract_gate_refusal_reason("") == ""
    assert _extract_gate_refusal_reason(None) == ""


def test_extract_gate_refusal_reason_ignores_a_gate_this_run_waived():
    """`coord merge --only --skip-smoke` prints the SAME gate line with
    "waived by this run" instead of "will block this merge". A waived gate
    refused nothing and must never be read back as a refusal."""
    from coord.drive import _extract_gate_refusal_reason

    waived = (
        "  gate smoke: test verdict stale (base moved) — waived by this run\n"
        "  --skip-smoke: interactive smoke-test gate bypassed (#465)"
    )
    assert _extract_gate_refusal_reason(waived) == ""


def test_a_waived_gate_in_the_diagnostic_leaves_the_merge_retryable():
    """End to end: the waiver guard above must not turn a merge that this run
    explicitly un-gated into an escalation or a re-test."""
    counters = DriveCounters(
        last_merge_diagnostic=(
            "  gate smoke: test verdict stale (base moved) — waived by this run"
        )
    )
    action = step(
        approved_work(merge_status="READY", merge_reason=""), counters=counters
    )
    assert action.kind == RUN
    assert action.command[0] == "merge"


# ── terminal: merged, verified ───────────────────────────────────────────────


def test_a_merged_board_row_is_verified_before_reporting_success():
    verifier = FakeVerifier(merged=True)
    action = step(approved_work(work_status="merged"), verifier=verifier)
    assert action.is_exit
    assert action.exit_code == EXIT_OK
    assert "MERGED" in action.message
    assert verifier.merged_calls == 1


def test_a_merged_board_row_that_did_not_actually_land_fails_loudly():
    action = step(
        approved_work(work_status="merged"), verifier=FakeVerifier(merged=False)
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "has NOT landed" in action.message


def test_merge_status_merged_is_also_a_terminal_success():
    action = step(approved_work(merge_status="MERGED"), verifier=FakeVerifier(merged=True))
    assert action.exit_code == EXIT_OK


def test_a_merged_row_with_no_branch_cannot_be_verified():
    action = step(approved_work(work_status="merged", work_branch=""))
    assert action.exit_code == EXIT_TERMINAL_FAILURE


# ═══════════════════════════════════════════════════════════════════════════
# GitMergeVerifier — never `merge-base --is-ancestor`
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def recorded_git(monkeypatch):
    """Capture every subprocess argv and script the return values.

    #1483 moved the ``gh pr view`` call in ``verify_merged`` behind the
    ``github_ops`` seam. Both ``coord.drive`` and ``coord.github_ops`` do a
    plain ``import subprocess``, so they share the same module-level
    ``subprocess.run`` attribute — patching it once via ``coord.drive.
    subprocess.run`` patches it for both call sites, and a single
    ``scripted`` dict drives both the git and gh sides of a scenario.

    #2437's ``_base()`` checkout-validation probes (``rev-parse
    --is-inside-work-tree`` and ``remote get-url origin``) default here to
    "yes, a real checkout of john/claude-coordinator" — the shape almost
    every test in this file wants — so only the handful of tests that
    exercise the validation itself need to override them.
    """
    calls: list[list[str]] = []
    scripted: dict[tuple[str, ...], tuple[int, str]] = {}

    def fake_run(argv, **kw):
        calls.append(list(argv))
        for needle, (rc, out) in scripted.items():
            if all(token in argv for token in needle):
                return subprocess.CompletedProcess(argv, rc, out, "")
        if "rev-parse" in argv and "--is-inside-work-tree" in argv:
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        if "remote" in argv and "get-url" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "https://github.com/john/claude-coordinator.git\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("coord.drive.subprocess.run", fake_run)
    return calls, scripted


def test_verify_merged_never_uses_merge_base_is_ancestor(recorded_git, tmp_path):
    """`--is-ancestor` is ALWAYS wrong under --method rebase/squash: both
    rewrite the commits, so a landed branch's tip is never an ancestor of the
    target. Verified against #1344 (merged via PR #1355, still says no)."""
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc123\n- def456\n")

    verifier = GitMergeVerifier(repo_path=str(tmp_path))
    s = state(work_branch="issue-1392-x", repo_github="", repo_default_branch="main")
    assert verifier.verify_merged(s) is True

    flat = [" ".join(c) for c in calls]
    assert not any("--is-ancestor" in c for c in flat), flat
    assert any("cherry" in c for c in flat), flat


def test_verify_merged_reports_unmerged_when_cherry_shows_a_plus(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc123\n+ def456\n")
    s = state(work_branch="b", repo_github="", repo_default_branch="main")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is False


def test_verify_merged_prefers_the_github_pr_state(recorded_git, tmp_path):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("pr", "view")] = (0, "MERGED\n")

    s = state(work_branch="b", repo_github="john/x")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is True
    # Authoritative: no git fallback needed.
    assert not any("cherry" in " ".join(c) for c in calls)


def test_verify_merged_rejects_a_closed_pr(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("pr", "view")] = (0, "CLOSED\n")

    warned: list[str] = []
    verifier = GitMergeVerifier(repo_path=str(tmp_path), warn=warned.append)
    assert verifier.verify_merged(state(work_branch="b", repo_github="john/x")) is False
    assert any("CLOSED" in w for w in warned)


def test_verify_merged_falls_back_to_cherry_when_gh_finds_no_pr(
    recorded_git, tmp_path
):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("pr", "view")] = (1, "")
    scripted[("cherry",)] = (0, "- abc\n")
    scripted[("remote", "get-url", "origin")] = (0, "git@github.com:john/x.git\n")
    s = state(work_branch="b", repo_github="john/x")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is True
    assert any("cherry" in " ".join(c) for c in calls)


def test_verify_merged_cleans_up_its_verify_ref(recorded_git, tmp_path):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc\n")
    s = state(work_branch="b", repo_github="", repo_default_branch="main")
    GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s)
    assert any("update-ref -d" in " ".join(c) for c in calls)


def test_branch_has_commits_counts_commits_the_default_branch_lacks(
    recorded_git, tmp_path
):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (0, "3\n")
    s = state(work_branch="b", repo_default_branch="main")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is True
    assert any("origin/main..FETCH_HEAD" in " ".join(c) for c in calls)


def test_branch_has_commits_is_false_for_zero(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (0, "0\n")
    s = state(work_branch="b")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is False


def test_branch_has_commits_is_none_when_rev_list_fails(recorded_git, tmp_path):
    """#2426: both fetches succeeded but `rev-list` itself failed — still no
    completed count, so still `None`, not `False`."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (128, "")
    s = state(work_branch="b")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is None


def test_branch_has_commits_is_none_when_rev_list_output_is_unparsable(
    recorded_git, tmp_path
):
    """#2426: a malformed count is the same "learned nothing" shape as a
    failed command — `None`, not `False`."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (0, "not-a-number\n")
    s = state(work_branch="b")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is None


def test_branch_has_commits_is_none_when_a_fetch_fails(recorded_git, tmp_path):
    """#2426: a `git fetch` failure (returncode 128 here covers both "branch
    genuinely doesn't exist on the remote" and "transient network/auth
    blip" — the two are indistinguishable from the exit code alone) must NOT
    collapse into `False`. `False` is reserved for a check that actually
    completed and counted zero commits; a failed fetch produced no such
    count, so it is `None` — "couldn't verify" — not "verified empty"."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("fetch", "b")] = (128, "")
    s = state(work_branch="b")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is None


def test_branch_has_commits_is_none_when_the_target_fetch_fails(
    recorded_git, tmp_path
):
    """Same #2426 distinction, for the OTHER fetch (`origin/<target>`)."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("fetch", "main")] = (128, "")
    s = state(work_branch="b", repo_default_branch="main")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is None


def test_the_verifiers_are_inert_without_a_local_checkout(tmp_path):
    """#2426: no local checkout means no evidence either way — `None`, not
    `False`. `verify_merged` is untouched by #2426 (GitHub is its primary
    source of truth, not this local checkout) and keeps its `False`."""
    verifier = GitMergeVerifier(repo_path=str(tmp_path / "nope"))
    s = state(work_branch="b", repo_github="")
    assert verifier.branch_has_commits(s) is None
    assert verifier.verify_merged(s) is False


def test_branch_has_commits_is_false_with_no_branch(tmp_path):
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(state()) is False


# ═══════════════════════════════════════════════════════════════════════════
# GitMergeVerifier._base — checkout validation (#2437)
#
# A `.git` directory existing is necessary but not sufficient: an
# interrupted/stub `git init` (bare `.git/info/exclude`, no `HEAD`/
# `objects`/`refs`/`config` — the exact claude-coordinator#2286 incident)
# satisfies `(base / ".git").exists()` while being completely unusable for
# `git fetch`. `_base()` must actually probe the checkout and warn loudly
# (not just silently return `None`) the first time it finds one unusable.
# ═══════════════════════════════════════════════════════════════════════════


def test_base_rejects_a_stub_git_dir_that_is_not_a_real_work_tree(
    recorded_git, tmp_path
):
    """The exact #2437 incident: `.git` exists (so the old check passed) but
    it's not an actual git repository — `rev-parse --is-inside-work-tree`
    fails. Must be treated the same as no checkout at all: `None`, plus a
    loud warning naming the reason, not a silent retry-forever."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-parse", "--is-inside-work-tree")] = (128, "")
    warned: list[str] = []
    verifier = GitMergeVerifier(repo_path=str(tmp_path), warn=warned.append)
    s = state(work_branch="b", repo_default_branch="main")
    assert verifier.branch_has_commits(s) is None
    assert any("not a usable git working tree" in w for w in warned), warned


def test_base_rejects_a_checkout_with_no_origin_remote(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("remote", "get-url", "origin")] = (2, "")
    warned: list[str] = []
    verifier = GitMergeVerifier(repo_path=str(tmp_path), warn=warned.append)
    s = state(work_branch="b", repo_default_branch="main")
    assert verifier.branch_has_commits(s) is None
    assert any("no 'origin' remote configured" in w for w in warned), warned


def test_base_rejects_a_checkout_of_the_wrong_repo(recorded_git, tmp_path):
    """A perfectly valid checkout — just of the wrong project. Must not be
    trusted to answer merge-verification questions about *this* repo."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("remote", "get-url", "origin")] = (
        0,
        "https://github.com/john/some-other-repo.git\n",
    )
    warned: list[str] = []
    verifier = GitMergeVerifier(repo_path=str(tmp_path), warn=warned.append)
    s = state(work_branch="b", repo_default_branch="main")
    assert verifier.branch_has_commits(s) is None
    assert any(
        "does not match the expected repo" in w and "john/claude-coordinator" in w
        for w in warned
    ), warned


def test_base_skips_the_repo_match_check_when_repo_github_is_unset(
    recorded_git, tmp_path
):
    """Some callers (the ``verify_merged`` tests above) legitimately don't
    know the GitHub identifier yet — the match check must not fire and turn
    an otherwise-valid checkout into a false ``None``."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("remote", "get-url", "origin")] = (
        0,
        "https://github.com/someone/unrelated.git\n",
    )
    scripted[("rev-list",)] = (0, "2\n")
    s = state(work_branch="b", repo_github="", repo_default_branch="main")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is True


def test_base_warns_only_once_for_the_same_broken_checkout(recorded_git, tmp_path):
    """A checkout that's been broken for days (the #2437 incident: 3 days, 6
    drive-queue attempts) must warn once, not once per poll — the whole
    point is a signal an operator can act on, not log spam that gets
    filtered out."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-parse", "--is-inside-work-tree")] = (128, "")
    warned: list[str] = []
    verifier = GitMergeVerifier(repo_path=str(tmp_path), warn=warned.append)
    s = state(work_branch="b", repo_default_branch="main")
    for _ in range(6):
        assert verifier.branch_has_commits(s) is None
    assert len(warned) == 1, warned


def test_base_accepts_a_genuinely_valid_checkout(recorded_git, tmp_path):
    """The happy path: a real work tree, an `origin` matching the expected
    repo — no warning, and verification proceeds as normal."""
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (0, "1\n")
    warned: list[str] = []
    verifier = GitMergeVerifier(repo_path=str(tmp_path), warn=warned.append)
    s = state(work_branch="b", repo_default_branch="main")
    assert verifier.branch_has_commits(s) is True
    assert warned == []


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/john/claude-coordinator.git",
        "https://github.com/john/claude-coordinator",
        "git@github.com:john/claude-coordinator.git",
        "git@github.com:john/claude-coordinator",
        "ssh://git@github.com/john/claude-coordinator.git",
    ],
)
def test_remote_matches_repo_accepts_every_url_shape_git_produces(remote_url):
    assert _remote_matches_repo(remote_url, "john/claude-coordinator") is True


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/john/some-other-repo.git",
        "git@github.com:someone-else/claude-coordinator.git",
        "",
    ],
)
def test_remote_matches_repo_rejects_a_mismatch(remote_url):
    assert _remote_matches_repo(remote_url, "john/claude-coordinator") is False


# ═══════════════════════════════════════════════════════════════════════════
# locking
# ═══════════════════════════════════════════════════════════════════════════


_needs_real_flock = pytest.mark.skipif(
    sys.platform == "win32",
    reason="FileLock is backed by fcntl.flock() (coord/filelock.py) — POSIX-only "
    "advisory locking, no Windows lock backend implemented yet",
)


@pytest.mark.posix_only
@_needs_real_flock
def test_a_second_lock_holder_is_refused_immediately(tmp_path):
    first = FileLock(tmp_path / "l")
    first.acquire(timeout=0.0)
    try:
        with pytest.raises(LockBusy):
            FileLock(tmp_path / "l").acquire(timeout=0.0)
    finally:
        first.release()
    # Released: it can be taken again.
    second = FileLock(tmp_path / "l")
    second.acquire(timeout=0.0)
    second.release()


@pytest.mark.posix_only
@_needs_real_flock
def test_lock_is_released_on_context_exit(tmp_path):
    with FileLock(tmp_path / "l"):
        pass
    FileLock(tmp_path / "l").acquire(timeout=0.0)


# ═══════════════════════════════════════════════════════════════════════════
# the Driver loop (I/O shell)
# ═══════════════════════════════════════════════════════════════════════════


class FakeFetcher:
    """Serves a scripted sequence of board payloads, repeating the last."""

    def __init__(
        self,
        payloads: list[dict] | None = None,
        error: Exception | None = None,
        error_on_call: int = 1,
    ):
        self.payloads = payloads or []
        self.error = error
        self.error_on_call = error_on_call
        self.calls = 0

    def fetch(self) -> dict:
        self.calls += 1
        if self.error is not None and self.calls == self.error_on_call:
            raise self.error
        if not self.payloads:
            return {"assignments": []}
        idx = min(self.calls - 1, len(self.payloads) - 1)
        return self.payloads[idx]


def board(**kw) -> dict:
    a = {
        "repo_name": REPO,
        "issue_number": ISSUE,
        "type": "work",
        "assignment_id": "w1",
        "dispatched_at": 1.0,
        "status": "done",
        "branch": "issue-1392-x",
        "machine_name": "precision",
    }
    a.update(kw)
    return {"assignments": [a]}


@pytest.fixture
def driver_factory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("coord.drive_state.scratch_dir", lambda: tmp_path)
    monkeypatch.setattr("coord.drive.scratch_dir", lambda: tmp_path)
    # #2170-class isolation, fixture-wide: `run_notify()` takes a REAL flock
    # on `notify_lock_path()` (`Path.home()/".coord"/"notify.lock"`, resolved
    # at call time) whenever `DriveOptions(notify=True)` is in play. Any
    # driver test in this module can turn that on, and without isolating it
    # the lock contends with whatever else on the host legitimately holds it
    # — on a machine that actually runs the fleet, `coord-notify.timer`
    # (5-minute cadence) and the board daemon's own drain both hold it for
    # the length of a full notify pass. The test then burns up to the full
    # 5-minute `timeout=300` per acquire, logs "could not take
    # ~/.coord/notify.lock within 5m — skipping nudge", and fails with an
    # empty call list. That is a race with whatever else is running on the
    # machine, not a signal about the code under test — exactly the class
    # `tests/test_ambient_home_isolation.py` and the `populated-home` CI job
    # exist to keep out.
    #
    # Redirecting $HOME is the whole mechanism, and it subsumes the earlier
    # #2536 fix (which pinned `coord.drive.notify_lock_path` at
    # `tmp_path/"notify.lock"` instead): `notify_lock_path()` resolves
    # `Path.home()` at call time, so the lock lands at
    # `tmp_path/".coord"/"notify.lock"` either way — but $HOME also covers
    # everything *else* in the driver that reaches for `~/.coord`, and it
    # needs no upkeep as call sites move. Deliberately NOT belt-and-braces:
    # re-pinning `notify_lock_path` on top of this would move the lock back
    # out of the redirected $HOME and silently defeat
    # `test_notify_nudges_coord_notify_under_the_shared_lock`, whose whole
    # job is to assert the redirection took.
    monkeypatch.setenv("HOME", str(tmp_path))

    def make(
        payloads, *, opts=None, verifier=None, config=None, oracle_gate=None,
        usage_prober=None, self_head_probe=None, ticks=200,
    ):
        clock = {"t": 0.0}
        recorded: list[list[str]] = []

        def fake_run(argv, **kw):
            recorded.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")

        monkeypatch.setattr("coord.drive.subprocess.run", fake_run)

        driver = Driver(
            repo=REPO,
            issue=ISSUE,
            opts=opts or DriveOptions(machine="precision", poll=1.0),
            config=config or make_config(),
            fetcher=FakeFetcher(payloads),
            verifier=verifier or FakeVerifier(),
            oracle_gate=oracle_gate,
            # #1466: never let a Driver test shell out to a real `claude -p
            # "/usage"` — default to a stub reporting "unknown" (same as no
            # probe at all), which the gate always treats as "proceed,
            # silently". Tests exercising the gate itself pass their own.
            usage_prober=usage_prober or (lambda: PlanLimits(status="unknown")),
            # #2443: default to a stub reporting a constant, never-moving
            # HEAD — same "opt in only the test that needs it" posture as
            # *usage_prober* above. Without this, every driver test would
            # exercise the REAL probe (a handful of local `git` reads
            # against this actual checkout, routed through `fake_run` above
            # since `coord.self_health` imports the same `subprocess`
            # module object) — harmless, but tying every other test's
            # behaviour to this repo's own git state is exactly the kind of
            # incidental coupling worth avoiding.
            self_head_probe=self_head_probe or (lambda: "unchanging-sha"),
            sleeper=lambda secs: clock.__setitem__("t", clock["t"] + secs),
            clock=lambda: clock["t"],
        )
        driver.recorded = recorded  # type: ignore[attr-defined]
        return driver

    return make


def test_driver_exits_zero_on_a_verified_merge(driver_factory, capsys):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    assert "MERGED" in capsys.readouterr().out


def _mixed_fleet_drive_config() -> Config:
    """#1906 acceptance fixture: one claude-only machine, one that also
    advertises `provider:opencode`, wired through `providers.labels` so a
    `harness:opencode` label resolves the effective provider."""
    return Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator", test_command="pytest -q")],
        machines=[
            Machine(name="claude-only", host="claude-only", repos=[REPO]),
            Machine(
                name="opencode-box", host="opencode-box", repos=[REPO],
                capabilities=["provider:opencode"],
            ),
        ],
        providers=ProvidersConfig(
            definitions={"opencode": ProviderDef(type="opencode")},
            labels={"harness:opencode": "opencode"},
        ),
    )


def test_driver_auto_picks_the_capable_machine_for_an_opencode_labelled_issue(
    driver_factory, capsys,
):
    """#1906 end-to-end acceptance: no `--machine`, the issue's cached label
    resolves to `opencode`, and the dispatched `coord assign` argv — the
    actual dispatch target, not just the absence of a #1711 exception —
    names the capable machine, never the incapable one."""
    payload = {
        "assignments": [],
        "issues": [{"repo_name": REPO, "number": ISSUE, "labels": ["harness:opencode"]}],
    }
    driver = driver_factory(
        [payload],
        opts=DriveOptions(machine="", poll=1.0, deadline_mins=0.5 / 60.0),
        config=_mixed_fleet_drive_config(),
    )
    assert driver.run() == EXIT_DEADLINE
    assert driver.recorded, "expected at least one dispatched coord command"
    dispatched = driver.recorded[0]
    assert "assign" in dispatched
    assert "opencode-box" in dispatched
    assert "claude-only" not in dispatched
    assert "opencode" in capsys.readouterr().out  # the provider provenance log line


def test_driver_reports_the_distinct_no_capable_machine_error(driver_factory):
    """The fleet hosts the repo but nobody advertises `opencode` — must not
    read as the generic 'no unpaused machine hosts' message (#1906)."""
    config = _mixed_fleet_drive_config()
    config.machines = [config.machines[0]]  # claude-only survives; opencode-box doesn't
    payload = {
        "assignments": [],
        "issues": [{"repo_name": REPO, "number": ISSUE, "labels": ["harness:opencode"]}],
    }
    driver = driver_factory(
        [payload], opts=DriveOptions(machine=""), config=config,
    )
    with pytest.raises(DriveError) as exc:
        driver.run()
    message = str(exc.value)
    assert "no unpaused machine advertises" in message
    assert "opencode" in message
    assert exc.value.exit_code == EXIT_USAGE


def test_driver_warns_loudly_when_the_pause_set_is_unreadable(driver_factory, capsys, monkeypatch):
    """#2807: an unreadable pause set must not silently degrade to "nothing
    is paused" with no trace anywhere. `pick_machine_choice` still fails
    open (auto-picks "precision" — the sole host), but the driver's own
    startup log must carry a loud warning about it, the same way it already
    surfaces the #1906 provider provenance line."""

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr("coord.machine_pause.paused_set", _boom)
    driver = driver_factory(
        [board(status="running")],
        opts=DriveOptions(machine="", poll=1.0, deadline_mins=0.5 / 60.0),
    )
    assert driver.run() == EXIT_DEADLINE
    err = capsys.readouterr().err
    assert "pause set unreadable" in err
    assert "permission denied" in err


def test_driver_stays_quiet_about_pause_reads_when_an_explicit_machine_is_given(
    driver_factory, capsys, monkeypatch,
):
    """An explicit `--machine` bypasses the auto-pick entirely (#1906's own
    `opts.machine` short-circuit) — the pause-read warning is only
    meaningful for an auto-pick, so it must not fire here."""

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr("coord.machine_pause.paused_set", _boom)
    driver = driver_factory(
        [board(status="running")],
        opts=DriveOptions(machine="precision", poll=1.0, deadline_mins=0.5 / 60.0),
    )
    assert driver.run() == EXIT_DEADLINE
    assert "pause set unreadable" not in capsys.readouterr().err


# ═══════════════════════════════════════════════════════════════════════════
# #1499: audit events at the driver's own boundaries
# ═══════════════════════════════════════════════════════════════════════════


def _drive_audit_rows(coord_db):
    rows = coord_db.execute(
        "SELECT event_type, actor, category, repo, issue, summary, details_json "
        "FROM audit_log WHERE category = 'drive' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def test_run_records_drive_started_and_drive_exited_on_a_clean_finish(
    driver_factory, coord_db, capsys
):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    capsys.readouterr()

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    for r in rows:
        assert r["actor"] == "drive"
        assert r["repo"] == REPO
        assert r["issue"] == ISSUE
    exit_details = json.loads(rows[1]["details_json"])
    assert exit_details["exit_code"] == EXIT_OK


def test_run_records_drive_exited_with_the_terminal_failure_reason(
    driver_factory, coord_db, capsys
):
    # A work assignment that failed outright is a terminal DriveError exit —
    # the exact "why did it stop?" the audit trail needs to answer after the
    # driver process is long gone.
    driver = driver_factory([board(status="failed")])
    code = driver.run()
    capsys.readouterr()
    assert code == EXIT_TERMINAL_FAILURE

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    exit_details = json.loads(rows[1]["details_json"])
    assert exit_details["exit_code"] == EXIT_TERMINAL_FAILURE
    assert "failed" in rows[1]["summary"]


# ── #1453: the preflight banner never leaves oracle mode unstated ───────────


def test_driver_banner_reports_normal_drive_when_no_acceptance_driver_is_configured(
    driver_factory, capsys,
):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "acceptance" in out
    assert "no acceptance.drivers entry" in out


def test_driver_banner_reports_oracle_drive_when_the_gate_is_satisfied(
    driver_factory, capsys,
):
    payload = board(status="merged")
    payload["issues"] = [{"repo_name": REPO, "number": ISSUE, "milestone_number": 38}]
    payload["milestone_work_orders"] = [
        {"repo_name": REPO, "tracking_issue": 1120, "nodes": [{"issue_number": ISSUE}]}
    ]
    driver = driver_factory(
        [payload],
        config=make_config_with_acceptance_driver(),
        oracle_gate=FakeGateChecker(exists=True),
    )
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "ORACLE DRIVE" in out
    assert "ms-38" in out


def test_driver_banner_reports_normal_drive_under_no_acceptance(driver_factory, capsys):
    payload = board(status="merged")
    payload["issues"] = [{"repo_name": REPO, "number": ISSUE, "milestone_number": 38}]
    payload["milestone_work_orders"] = [
        {"repo_name": REPO, "tracking_issue": 1120, "nodes": [{"issue_number": ISSUE}]}
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(machine="precision", poll=1.0, no_acceptance=True),
        config=make_config_with_acceptance_driver(),
        oracle_gate=FakeGateChecker(exists=True),
    )
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "--no-acceptance set" in out


def test_driver_shells_out_to_coord_and_never_calls_internals(driver_factory):
    """The CLI boundary, end to end: the merge really is a `coord` argv."""
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=1, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_TERMINAL_FAILURE  # cap reached, never landed
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("merge --only w1 --method rebase" in a for a in argvs), argvs


def test_driver_captures_the_merge_attempts_own_diagnostic_into_the_die_message(
    driver_factory, monkeypatch, capsys,
):
    """#2078 end to end: `Driver._loop` must actually capture each `coord
    merge --only` attempt's stdout/stderr (what `_explain_missing_only_entry`
    prints when there's no queue entry) and thread it through to the final
    give-up message — not just leave the pure-decide()-level plumbing
    (`counters.last_merge_diagnostic`) unwired.

    The diagnostic here deliberately reports "identifier did not resolve"
    (no board row matched at all) rather than a named gate — a named-gate
    diagnostic exercises the WAIT-instead-of-retry arm (covered at the pure
    `decide()` level by `test_empty_status_with_a_known_gate_block_waits_
    instead_of_retrying_blind`), which would make this run stall out at the
    deadline instead of reaching the exhausted-attempts die this test wants.
    """
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=1, deadline_mins=1.0
        ),
    )

    def fake_run(argv, **kw):
        if "merge" in argv and "--only" in argv:
            return subprocess.CompletedProcess(
                argv, 1,
                "merge-queue: no entry found for 'w1' (tried assignment_id, "
                "repo#issue, issue number, and branch name)\n"
                "  no done work row on the board matches that identifier "
                "either — the identifier did not resolve.\n",
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr("coord.drive.subprocess.run", fake_run)

    assert driver.run() == EXIT_TERMINAL_FAILURE
    err = capsys.readouterr().err
    assert "no entry found for 'w1'" in err
    assert "identifier did not resolve" in err


def _oracle_slice_payload(*, author_status: str, work: dict | None = None) -> dict:
    """#2157 fixture: an oracle-mode board whose JIT acceptance slice is
    authored, tested, approved — with *author_status* deciding whether its
    row has reconciled to 'merged' yet — and NO merge-queue entry visible at
    all (`merge_status == ''`), which is exactly what coord-portal#51's board
    reported while the slice's queue entry already read 'merged'."""
    return {
        "assignments": [
            {
                "repo_name": REPO,
                "issue_number": 1120,          # the milestone TRACKING issue
                "for_issue_number": ISSUE,     # ...scoped to THIS issue
                "type": "test-author",
                "assignment_id": "ta1",
                "dispatched_at": 1.0,
                "status": author_status,
                "branch": "test-author-ms-38-slice-1392",
                "machine_name": "precision",
                "test_state": "passed",
            },
            {
                "repo_name": REPO,
                "issue_number": 1120,
                "type": "review",
                "assignment_id": "rv1",
                "dispatched_at": 2.0,
                "status": "done",
                "review_of_assignment_id": "ta1",
                "review_verdict": "approve",
            },
            *([work] if work else []),
        ],
        "issues": [{"repo_name": REPO, "number": ISSUE, "milestone_number": 38}],
        "milestone_work_orders": [
            {"repo_name": REPO, "tracking_issue": 1120, "nodes": [{"issue_number": ISSUE}]}
        ],
    }


def test_driver_drives_on_when_the_slice_merges_mid_run(driver_factory, monkeypatch):
    """#2157 end to end — the coord-portal#51 regression.

    The slice's PR lands mid-drive, so `coord merge --only ta1` reports it as
    already merged. Pre-#2157 that reading was a failed attempt: with the cap
    at 1 the very next poll died `EXIT_TERMINAL_FAILURE` ("merge attempted 1
    times without landing"), which `coord drive-queue` turns into a `blocked`
    entry — for an issue whose merge had SUCCEEDED. It must instead wait out
    the board reconciling and then dispatch the work it was gating.
    """
    payloads = [
        # The preflight banner's own fetch.
        _oracle_slice_payload(author_status="done"),
        # Poll 1: the slice is authored/tested/approved with no visible queue
        # entry — the driver spends its one attempt on `coord merge --only`,
        # which reports the entry has already merged.
        _oracle_slice_payload(author_status="done"),
        # Poll 2: the board STILL has not reconciled. With max_merge_attempts=1
        # this is exactly where the old code died with `exhausted`.
        _oracle_slice_payload(author_status="done"),
        # Poll 3: the board reconciles — the #1138 gate is satisfied and the
        # work this whole run exists to dispatch finally goes out.
        _oracle_slice_payload(author_status="merged"),
        # Poll 4: that work lands.
        _oracle_slice_payload(
            author_status="merged",
            work={
                "repo_name": REPO,
                "issue_number": ISSUE,
                "type": "work",
                "assignment_id": "w1",
                "dispatched_at": 3.0,
                "status": "merged",
                "branch": "issue-1392-x",
                "machine_name": "precision",
            },
        ),
    ]
    driver = driver_factory(
        payloads,
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=1, deadline_mins=1.0
        ),
        config=make_config_with_acceptance_driver(),
        oracle_gate=FakeGateChecker(exists=True),
    )

    recorded: list[list[str]] = []

    def fake_run(argv, **kw):
        recorded.append(list(argv))
        if "merge" in argv and "--only" in argv:
            # Post-#2157 `coord merge --only` on a merged entry: exit 0.
            return subprocess.CompletedProcess(
                argv, 0,
                "merge-queue: entry 'ta1' already merged (PR #60) "
                "— nothing to do\n",
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr("coord.drive.subprocess.run", fake_run)

    assert driver.run() == EXIT_OK
    argvs = [" ".join(a) for a in recorded]
    # It really did reach the work-dispatch step the slice was gating...
    assert any("assign precision" in a for a in argvs), argvs
    # ...and it spent exactly ONE merge attempt doing so, not the cap.
    assert sum("merge --only ta1" in a for a in argvs) == 1, argvs


def test_driver_still_gives_up_when_the_merge_genuinely_never_lands(
    driver_factory, monkeypatch,
):
    """The other half of #2157: narrowing the guard to MERGED must not turn a
    slice that never lands into an unbounded spin. A CONFLICT refusal still
    burns the cap and still exits terminally."""
    driver = driver_factory(
        [_oracle_slice_payload(author_status="done")],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=1, deadline_mins=1.0
        ),
        config=make_config_with_acceptance_driver(),
        oracle_gate=FakeGateChecker(exists=True),
    )

    recorded: list[list[str]] = []

    def fake_run(argv, **kw):
        recorded.append(list(argv))
        if "merge" in argv and "--only" in argv:
            return subprocess.CompletedProcess(
                argv, 1,
                "merge-queue: entry 'ta1' is in state 'conflict' (not PENDING) "
                "— cannot merge\n",
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr("coord.drive.subprocess.run", fake_run)

    assert driver.run() == EXIT_TERMINAL_FAILURE
    argvs = [" ".join(a) for a in recorded]
    assert not any("assign precision" in a for a in argvs), argvs


def test_driver_escalates_and_writes_the_record_via_the_cli(driver_factory):
    """#1505 end to end: a NEEDS_ATTENTION merge status escalates instead of
    burning `max_merge_attempts` on `coord merge --only`, and the write goes
    out as a `coord escalate record` argv — the CLI-is-the-contract rule,
    executed by the I/O shell (`Driver.run`), never `decide()` directly."""
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    payload["merge_plan"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "status": "NEEDS_ATTENTION",
            "assignment_id": "w1",
            "pr_url": "https://github.com/john/claude-coordinator/pull/1496",
        }
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=3, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_ESCALATED
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("escalate record" in a for a in argvs), argvs
    assert any("gh pr merge 1496 --rebase" in a for a in argvs), argvs
    assert not any(" merge --only" in a for a in argvs), argvs


def test_driver_escalates_a_gate_divergence_without_ever_attempting_the_merge(
    driver_factory,
):
    """#1526 end to end: `/board`'s `merge_plan` reads READY (a normal
    daemon-backed board build — `merge_queue.plan()`'s own render-time gate
    check didn't have the live SHA data to catch the staleness a REAL `coord
    merge` attempt would), but its `reason` already carries a smoke refusal
    left over from state persisted on the raw queue row. `work_test_state`
    reads 'passed'. `coord merge --only` must never even run — the
    divergence escalates on the very first poll.
    """
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    payload["merge_plan"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "status": "READY",
            "reason": "smoke test required but no verdict recorded",
            "assignment_id": "w1",
        }
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=3, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_ESCALATED
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("escalate record" in a for a in argvs), argvs
    assert any("coord test w1 --passed" in a for a in argvs), argvs
    assert not any(" merge --only" in a for a in argvs), argvs


def test_driver_posts_a_durable_comment_when_a_gate_divergence_escalates(
    driver_factory, monkeypatch,
):
    """#1526: the tmux pane and the `coord escalate` board row are not
    enough — both disappear the moment the drive session ends unless an
    operator already knows to look. The escalation must also reach the
    issue itself. Stubs `github_ops.post_issue_comment` so this never
    shells out to a real `gh`.
    """
    posted: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        "coord.github_ops.post_issue_comment",
        lambda repo, issue, body: posted.append((repo, issue, body)),
    )
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    payload["merge_plan"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "status": "READY",
            "reason": "smoke test required but no verdict recorded",
            "assignment_id": "w1",
        }
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=3, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_ESCALATED
    assert len(posted) == 1
    repo_github, issue_number, body = posted[0]
    assert repo_github == "john/claude-coordinator"
    assert issue_number == ISSUE
    assert "smoke" in body.lower()


def test_driver_does_not_post_a_comment_on_a_normal_merge(driver_factory, monkeypatch):
    """The new #1526 comment channel is scoped to escalations only — a
    normal verified merge must not grow a GitHub side-effect it never had
    before."""
    posted: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        "coord.github_ops.post_issue_comment",
        lambda repo, issue, body: posted.append((repo, issue, body)),
    )
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    assert posted == []


def test_driver_retries_a_conflict_originated_needs_attention_instead_of_escalating(
    driver_factory,
):
    """#1505 review fix, end to end: a normal daemon-backed board populates
    `merge_plan` on nearly every `/board` build, and `merge_queue.plan()`
    collapses a fresh CONFLICT into "NEEDS_ATTENTION" for display — that is
    the value `_decide_merge` actually receives for a still-auto-fixable
    conflict, NOT the literal string "CONFLICT". If the raw `merge_queue`
    row isn't cross-checked (`drive_state._merge_entry`), this escalates
    immediately and never gives `coord merge --only` (and the
    `classify_conflict`/`dispatch_conflict_fix` machinery it runs, #1474)
    another poll to clear the conflict — the same failure shape #1505 was
    opened to fix, just moved from HUMAN_REQUIRED onto every ordinary
    conflict. The board here never actually changes state (no fake merge
    lands), so the run must exhaust its bounded attempt cap and die with
    the generic exhaustion message — never the escalate branch.
    """
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    payload["merge_plan"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "status": "NEEDS_ATTENTION",
            "assignment_id": "w1",
        }
    ]
    payload["merge_queue"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "state": "conflict",
            "error": "rebase failed",
            "assignment_id": "w1",
        }
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=2, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_TERMINAL_FAILURE  # cap reached, never escalated
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("merge --only w1 --method rebase" in a for a in argvs), argvs
    assert not any("escalate record" in a for a in argvs), argvs


def test_driver_returns_the_deadline_code_when_time_runs_out(driver_factory, capsys):
    driver = driver_factory(
        [board(status="done", test_state="")],
        opts=DriveOptions(machine="precision", poll=30.0, deadline_mins=1.0),
    )
    assert driver.run() == EXIT_DEADLINE
    assert "deadline" in capsys.readouterr().err


def test_driver_self_heals_when_code_moves_underneath_a_stuck_wait(
    driver_factory, capsys,
):
    """#2443: the claude-coordinator#2286 shape end to end. `_decide_
    advisory`'s "could not verify branch commits (git fetch failed),
    retrying" WAIT (#2426) repeats byte-for-byte every poll as long as
    nothing about the underlying condition changes — exactly what a stuck
    `git fetch` looks like, and exactly what a session running STALE
    in-memory `coord/drive.py` code can never resolve on its own (Python
    never reloads an already-imported module). If this session's own
    on-disk checkout moves while it is stuck in that loop, it must self-exit
    (`EXIT_SELF_STALE`) instead of polling the same dead logic to
    `--deadline` — the recovery is `coord drive-queue` relaunching fresh,
    not this process outliving its own bug fix."""
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        # Call 1 is `_loop`'s own start-of-run baseline capture. Every call
        # after that is a self-heal re-check, made only once the WAIT
        # streak crosses the threshold — return a DIFFERENT sha from the
        # second call onward to simulate a fix landing mid-run.
        return "aaaaaaaaaaaa" if calls["n"] == 1 else "bbbbbbbbbbbb"

    driver = driver_factory(
        [board(status="advisory", branch="issue-1392-x")],
        opts=DriveOptions(machine="precision", poll=1.0, deadline_mins=60.0),
        verifier=FakeVerifier(has_commits=None),
        self_head_probe=probe,
    )
    assert driver.run() == EXIT_SELF_STALE
    err = capsys.readouterr().err
    assert "code changed underneath this session" in err
    assert "aaaaaaaa" in err  # the recorded start sha, truncated to 8 chars
    assert "bbbbbbbb" in err  # the recorded moved-to sha, truncated
    # It genuinely waited out the streak threshold instead of firing on the
    # very first repeat of the label — the start capture plus AT LEAST one
    # re-check, never on call 1 alone.
    assert calls["n"] >= 2


def test_driver_keeps_waiting_when_the_same_wait_repeats_but_code_has_not_moved(
    driver_factory, capsys,
):
    """The other half of #2443: an identical WAIT label repeating is not, on
    its own, grounds to self-exit — only a MOVED on-disk HEAD is. The
    ordinary, overwhelmingly common case (the underlying condition just
    genuinely has not resolved yet) must keep polling exactly as before,
    all the way to `--deadline` — this must never fire for a
    slow-but-progressing wait."""
    driver = driver_factory(
        [board(status="advisory", branch="issue-1392-x")],
        opts=DriveOptions(machine="precision", poll=1.0, deadline_mins=0.2),
        verifier=FakeVerifier(has_commits=None),
        self_head_probe=lambda: "unchanging-sha",
    )
    assert driver.run() == EXIT_DEADLINE
    assert "deadline" in capsys.readouterr().err


def test_driver_tolerates_a_transport_blip_mid_loop_and_retries(driver_factory, capsys):
    """A daemon blip must be a retry next poll, never a traceback."""
    driver = driver_factory([board(status="merged")])
    driver.fetcher = FakeFetcher(
        [board(status="merged")],
        error=RuntimeError("connection reset"),
        error_on_call=2,  # call 1 is the preflight read
    )
    assert driver.run() == EXIT_OK
    assert "state read failed" in capsys.readouterr().err


def test_a_blip_on_the_preflight_read_is_a_usage_error(driver_factory):
    """Preflight has nothing to resume from — mirrors the bash `die ... 2`."""
    driver = driver_factory([board(status="merged")])
    driver.fetcher = FakeFetcher([board()], error=RuntimeError("no route to host"))
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_USAGE


@pytest.mark.posix_only
@_needs_real_flock
def test_driver_refuses_a_second_run_on_the_same_issue(driver_factory, tmp_path):
    """A per-issue lock: two drivers on the same issue would double-dispatch."""
    held = FileLock(tmp_path / f"lock-{REPO}-{ISSUE}")
    held.acquire(timeout=0.0)
    (tmp_path / f"holder-{REPO}-{ISSUE}").write_text(
        "someone else (pid 1)\n", encoding="utf-8"
    )
    try:
        driver = driver_factory([board(status="merged")])
        with pytest.raises(DriveError) as exc:
            driver.run()
        assert "already driving" in str(exc.value)
        assert "someone else" in str(exc.value)
        assert exc.value.exit_code == EXIT_USAGE
    finally:
        held.release()


@pytest.mark.posix_only
@_needs_real_flock
def test_driver_allows_a_concurrent_run_on_a_different_issue(driver_factory, tmp_path):
    held = FileLock(tmp_path / f"lock-{REPO}-999")
    held.acquire(timeout=0.0)
    try:
        driver = driver_factory([board(status="merged")])
        assert driver.run() == EXIT_OK
    finally:
        held.release()


@pytest.mark.posix_only
@_needs_real_flock
def test_driver_releases_the_lock_and_removes_the_holder_file(driver_factory, tmp_path):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    assert not (tmp_path / f"holder-{REPO}-{ISSUE}").exists()
    FileLock(tmp_path / f"lock-{REPO}-{ISSUE}").acquire(timeout=0.0)


def test_dry_run_prints_the_state_and_exits_without_dispatching(driver_factory, capsys):
    driver = driver_factory(
        [board(status="done", test_state="")],
        opts=DriveOptions(machine="precision", dry_run=True),
    )
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "WORK_AID" in out
    body = out[out.index("{") :]
    parsed = json.loads(body[: body.rindex("}") + 1])
    assert parsed["WORK_AID"] == "w1"
    assert driver.recorded == []  # type: ignore[attr-defined]


def test_driver_reports_an_unconfigured_repo_as_a_usage_error(driver_factory):
    driver = driver_factory([board()])
    driver.repo = "not-a-repo"
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_USAGE


def test_driver_writes_the_per_issue_run_log(driver_factory, tmp_path):
    payload = board(status="done", test_state="")
    driver = driver_factory(
        [payload], opts=DriveOptions(machine="precision", skip_test=True, poll=1.0,
                                     deadline_mins=0.05)
    )
    driver.run()
    log = tmp_path / f"{REPO}-{ISSUE}.log"
    assert log.exists() and "ok" in log.read_text(encoding="utf-8")


def test_die_exit_message_is_written_to_the_run_log_not_just_the_pane(
    driver_factory, tmp_path, monkeypatch
):
    """#2712 second defect: `_die()`'s message reached `self.warn` only —
    stderr, which for a `--tmux` drive is the tmux pane, destroyed the
    instant the session exits (the exact moment this fires). Without also
    appending it to `_run_log` (the file `launch_drive_in_tmux`'s own
    docstring calls out as the thing that survives the pane), a drive that
    dies mid-merge leaves `/tmp/coord-drive-issue-<uid>/<repo>-<issue>.log`
    simply stopping after its last narrated action with no explanation
    recorded anywhere — reading as "stalled" instead of "failed"."""
    driver = driver_factory([board(status="done", test_state="")])

    def fake_decide(*a, **kw):
        return _die(
            "merge attempted 3 times without landing.\n"
            "   Last board state: status='CONFLICT' reason='rebase failed'"
        )

    monkeypatch.setattr("coord.drive.decide", fake_decide)
    exit_code = driver.run()
    assert exit_code == EXIT_TERMINAL_FAILURE
    log_text = (tmp_path / f"{REPO}-{ISSUE}.log").read_text(encoding="utf-8")
    assert "merge attempted 3 times without landing." in log_text
    assert "Last board state: status='CONFLICT' reason='rebase failed'" in log_text


def test_driver_writes_a_start_marker_even_when_the_loop_never_spawns_anything(
    driver_factory, tmp_path
):
    """#1606: `decide()`'s very first branch after "merged" is a pure `WAIT`
    with no command whenever `state.active_count > 0` — the ordinary,
    majority-case shape of attaching to an issue that already has another
    assignment active (a review or merge dispatched via the TUI's
    interactive-agent flow, or a drive re-attached after a previous tmux
    session died mid-run). That loop never calls `_spawn` (the only other
    writer of the run log), so it must sit alive-but-log-silent for the
    whole `--poll` interval — UNLESS `Driver.run()` itself stamps a start
    marker the instant its per-issue lock is acquired, which is what
    `launch_drive_in_tmux`'s post-launch verification (~8s window) actually
    relies on to avoid killing this exact healthy session."""
    payload = board(status="dispatched")  # non-terminal → counted in `active`
    driver = driver_factory(
        [payload],
        opts=DriveOptions(machine="precision", poll=1.0, deadline_mins=0.001),
    )
    exit_code = driver.run()
    assert exit_code == EXIT_DEADLINE
    assert driver.recorded == []  # never spawned a `coord` subcommand
    log = tmp_path / f"{REPO}-{ISSUE}.log"
    assert log.exists()
    assert "drive loop started" in log.read_text(encoding="utf-8")


def test_a_die_on_error_action_raises_a_drive_error(driver_factory, monkeypatch):
    driver = driver_factory([board(status="failed", failure_reason="boom")])

    def failing_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "nope")

    monkeypatch.setattr("coord.drive.subprocess.run", failing_run)
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_TERMINAL_FAILURE


# ═══════════════════════════════════════════════════════════════════════════
# #2618: surviving a `coord-serve` restart mid-drive. `_spawn` is the ONE
# place every daemon-mutating `coord` subcommand this driver runs actually
# executes — see its own #2618 comment in coord/drive.py for the full
# investigation. These exercise `_spawn` directly (not through the whole
# `decide()`/`_loop()` machinery) so the retry/give-up boundary is pinned
# precisely, independent of which Action happened to trigger it.
# ═══════════════════════════════════════════════════════════════════════════


def test_spawn_retries_a_clean_connection_refusal_and_recovers(
    driver_factory, monkeypatch,
):
    driver = driver_factory([board(status="merged")])
    calls: list[list[str]] = []

    def flaky_run(argv, **kw):
        calls.append(list(argv))
        if len(calls) <= 2:
            # httpx's own wording for an ECONNREFUSED, as seen in a `coord`
            # subcommand's captured stderr when the daemon isn't listening
            # yet (a `coord-serve` restart in progress).
            return subprocess.CompletedProcess(
                argv, 1, "",
                "httpx.ConnectError: [Errno 111] Connection refused",
            )
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr("coord.drive.subprocess.run", flaky_run)
    rc = driver._spawn(["coord", "test", "--passed", "w1"])
    assert rc == 0
    assert len(calls) == 3  # two refusals ridden out, then the real attempt
    assert driver._last_run_output == "ok"


def test_spawn_gives_up_after_the_retry_budget_on_a_connection_refusal(
    driver_factory, monkeypatch,
):
    from coord.drive import _DAEMON_CONN_REFUSED_RETRIES

    driver = driver_factory([board(status="merged")])
    calls: list[list[str]] = []

    def always_refused(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 1, "",
            "requests.exceptions.ConnectionError: Failed to establish a new "
            "connection: [Errno 111] Connection refused",
        )

    monkeypatch.setattr("coord.drive.subprocess.run", always_refused)
    rc = driver._spawn(["coord", "test", "--passed", "w1"])
    assert rc == 1  # a genuinely-down daemon still surfaces as a failure
    assert len(calls) == _DAEMON_CONN_REFUSED_RETRIES + 1  # bounded, not infinite


def test_spawn_does_not_retry_an_ordinary_command_failure(driver_factory, monkeypatch):
    """A ValueError from bad input, a guard refusal, a stack trace — none of
    these carry the connection-refused signature, so retrying would just
    delay a real failure's diagnosis for no reason (and, for a
    non-idempotent RUN action, risk a double-dispatch on top)."""
    driver = driver_factory([board(status="merged")])
    calls: list[list[str]] = []

    def failing_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 1, "", "ValueError: no such machine 'elitebook'",
        )

    monkeypatch.setattr("coord.drive.subprocess.run", failing_run)
    rc = driver._spawn(["coord", "assign", "precision", REPO, str(ISSUE)])
    assert rc == 1
    assert len(calls) == 1  # not retried


def test_looks_like_daemon_connection_refused_matches_known_signatures():
    from coord.drive import _looks_like_daemon_connection_refused as looks

    assert looks("httpx.ConnectError: [Errno 111] Connection refused")
    assert looks(
        "requests.exceptions.ConnectionError: Failed to establish a new "
        "connection: [Errno 111] Connection refused"
    )
    assert looks("Connection refused")  # bare OS-level wording
    assert not looks("ValueError: no such machine 'elitebook'")
    assert not looks("")
    # A reset or timeout mid-request is ambiguous (the daemon may already
    # have processed it) — deliberately NOT matched, see the #2618 comment.
    assert not looks("httpx.ReadTimeout: timed out")
    assert not looks("ConnectionResetError: [Errno 104] Connection reset by peer")


# ═══════════════════════════════════════════════════════════════════════════
# #1844: a permanent pre-dispatch refusal is NOT a generic RUN-action
# failure. `coord assign`/`coord approve-plan`/`coord fix` exit
# EXIT_DISPATCH_REFUSED (not the generic 1) when `enforce_oracle_readiness`/
# `enforce_epic_dispatch_guard` refuse deterministically; `_loop`'s RUN
# handling must re-raise with THAT exit code, carrying the child's own
# captured output (the guard's remedy, verbatim) rather than a synthesised
# "coord ... exited 5" — that captured text is what `_drive_exit_summary`
# folds into the `drive_exited` audit row, which is the only thing
# `coord/drive_queue.py`'s tick can read once this process is gone.
# ═══════════════════════════════════════════════════════════════════════════


def test_a_dispatch_refusal_raises_a_drive_error_with_the_distinct_exit_code(
    driver_factory, monkeypatch,
):
    driver = driver_factory([board(status="failed", failure_reason="boom")])
    refusal_text = (
        "  dispatch failed: Issue #1817 is part of oracle-opted-in milestone "
        "ms-51 (Gate A satisfied) but has no acceptance slice yet — run "
        "`coord acceptance author claude-coordinator <tracking_issue> "
        "--issue 1817` first."
    )

    def refused_run(argv, **kw):
        return subprocess.CompletedProcess(argv, EXIT_DISPATCH_REFUSED, "", refusal_text)

    monkeypatch.setattr("coord.drive.subprocess.run", refused_run)
    with pytest.raises(DriveError) as exc:
        driver.run()
    # THE two things #1844 exists for: the exit code is distinguishable from
    # a crash (EXIT_TERMINAL_FAILURE), and the message is the guard's OWN
    # text — including its remedy — not a generic "exited 5".
    assert exc.value.exit_code == EXIT_DISPATCH_REFUSED
    assert exc.value.exit_code != EXIT_TERMINAL_FAILURE
    assert "acceptance author" in str(exc.value)
    assert refusal_text.strip() in str(exc.value)


def test_a_dispatch_refusals_reason_reaches_the_drive_exited_audit_row(
    driver_factory, monkeypatch, coord_db,
):
    """End-to-end through `Driver.run()`'s audit boundary (#1499): the
    `drive_exited` row's `details.exit_code` must be EXIT_DISPATCH_REFUSED —
    the ONE fact `coord/commands/drive_queue.py`'s `_fetch_exit_reasons`
    reads to tell a refusal apart from a genuine death — and its `summary`
    must carry the refusal's own text.
    """
    driver = driver_factory([board(status="failed", failure_reason="boom")])
    refusal_text = "refusing: no acceptance slice yet — run `coord acceptance author ...`"

    def refused_run(argv, **kw):
        return subprocess.CompletedProcess(argv, EXIT_DISPATCH_REFUSED, "", refusal_text)

    monkeypatch.setattr("coord.drive.subprocess.run", refused_run)
    with pytest.raises(DriveError):
        driver.run()

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    details = json.loads(rows[1]["details_json"])
    assert details["exit_code"] == EXIT_DISPATCH_REFUSED
    assert refusal_text in rows[1]["summary"]


# ═══════════════════════════════════════════════════════════════════════════
# #2274: a non-refusal RUN-action failure (`coord assign` dying for a reason
# that is NOT the deterministic EXIT_DISPATCH_REFUSED — an API blip, a bad
# briefing file, a stack trace) used to discard the child's own captured
# stdout+stderr entirely, raising a bare "coord assign ... exited 1". That
# string — with zero diagnostic content — is what `drive_queue.last_reason`
# and `drive_escalations.reason` are stuck showing forever once the tmux
# session is gone (quadraui#508, coord-portal#83: ~20 minutes spent
# diagnosing a parked entry, ending only by re-running the command by hand).
# ═══════════════════════════════════════════════════════════════════════════


def test_a_non_refusal_run_failure_carries_the_childs_captured_output(
    driver_factory, monkeypatch,
):
    driver = driver_factory([board(status="failed", failure_reason="boom")])
    failure_text = "Traceback (most recent call last):\nValueError: no such machine 'elitebook'"

    def failing_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", failure_text)

    monkeypatch.setattr("coord.drive.subprocess.run", failing_run)
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_TERMINAL_FAILURE
    # The action's own header (static, chosen when the Action was built) is
    # still there, but the child's own diagnostic text is now attached too —
    # the whole point.
    message = str(exc.value)
    assert message != failure_text  # a header still leads
    assert failure_text in message


def test_a_non_refusal_run_failures_output_reaches_the_drive_exited_audit_row(
    driver_factory, monkeypatch, coord_db,
):
    """Same end-to-end path as the dispatch-refusal audit test above: the
    captured output must survive into the `drive_exited` row's `summary` —
    the ONE thing `coord/drive_queue.py`'s `_fetch_exit_reasons` can read
    once the tmux session and its scrollback are gone."""
    driver = driver_factory([board(status="failed", failure_reason="boom")])
    failure_text = "assign: dispatch to elitebook failed: connection refused"

    def failing_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", failure_text)

    monkeypatch.setattr("coord.drive.subprocess.run", failing_run)
    with pytest.raises(DriveError):
        driver.run()

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    assert failure_text in rows[1]["summary"]


def test_a_non_refusal_run_failures_output_is_bounded(driver_factory, monkeypatch):
    """#2274 asks for "a bounded tail ... a few KB is plenty" — this sits in
    a DB column the tick reads on every poll, not a log file. A runaway
    stderr (a giant repeated traceback, a misbehaving child dumping a whole
    file) must not blow that column up without limit."""
    from coord.drive import _CAPTURED_OUTPUT_LIMIT

    driver = driver_factory([board(status="failed", failure_reason="boom")])
    huge = "E" * (_CAPTURED_OUTPUT_LIMIT * 5) + "TAIL_MARKER_END"

    def failing_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", huge)

    monkeypatch.setattr("coord.drive.subprocess.run", failing_run)
    with pytest.raises(DriveError) as exc:
        driver.run()
    message = str(exc.value)
    # The tail (where the actionable line lives) survives...
    assert "TAIL_MARKER_END" in message
    # ...but the captured blob itself is nowhere near the full 5x size.
    assert len(message) < _CAPTURED_OUTPUT_LIMIT * 2


def test_a_warn_on_error_action_keeps_looping(driver_factory, monkeypatch, capsys):
    """A failing `coord merge` is "try again next poll", not an abort."""
    payload = board(status="done", test_state="passed")
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=2, deadline_mins=1.0
        ),
    )
    monkeypatch.setattr(
        "coord.drive.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "queue empty"),
    )
    assert driver.run() == EXIT_TERMINAL_FAILURE
    assert "returned non-zero" in capsys.readouterr().err


def test_notify_is_off_by_default(driver_factory, monkeypatch):
    """Two drivers racing to dispatch is #476/#477 — --notify is opt-in."""
    driver = driver_factory([board(status="merged")])
    called: list[str] = []
    monkeypatch.setattr(driver, "run_coord", lambda *a, **k: called.append("x") or 0)
    driver.run_notify()
    assert called == []


def test_notify_nudges_coord_notify_under_the_shared_lock(driver_factory, tmp_path):
    # #2170-class isolation: `run_notify()` takes a REAL flock on
    # `notify_lock_path()` — i.e. `Path.home()/".coord"/"notify.lock"`,
    # resolved at call time. `driver_factory` redirects $HOME to `tmp_path`
    # fixture-wide (see its definition) precisely so this doesn't contend
    # with whatever else on the host legitimately holds that lock (a live
    # `coord notify` / `run_drain` on a daemon host, or simply another
    # pytest worker running the sibling drain tests) and burn the full
    # 5-minute `timeout=300` before failing with an empty `seen`. This
    # assertion just confirms the redirection actually took.
    driver = driver_factory(
        [board(status="merged")], opts=DriveOptions(machine="precision", notify=True)
    )
    seen: list[tuple] = []
    driver.run_coord = lambda args, **kw: seen.append(args) or 0  # type: ignore[assignment]
    driver.run_notify()
    assert seen == [("notify",)]
    # The lock really was taken under the redirected $HOME, not the host's.
    assert (tmp_path / ".coord" / "notify.lock").exists()


def test_stalled_stage_gets_re_nudged_on_every_stall_window(driver_factory, capsys):
    """#1593: a stage stuck at the same fingerprint (the worker is still
    running) must be nudged repeatedly, not just once. The old one-shot latch
    (``nudged = False``/``True``, cleared only on a fingerprint change) fired
    a single nudge near the start of the stall and then went silent for the
    rest of it — `coord notify` correctly finds nothing to settle while the
    worker is mid-run, and nothing else ever re-checks. Observed live as
    30-40 minute dead-air gaps at every stage boundary."""
    driver = driver_factory(
        [board(status="running")],
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            stall_mins=1.0 / 60.0,  # 1 "second" in the fake clock's units
            deadline_mins=8.0 / 60.0,  # 8 units — spans several stall windows
            notify=True,
        ),
        # #2649: isolate the flat-`--stall` mechanism under test from the
        # new built-in per-type `work` override — see
        # `make_config_no_stall_overrides`'s docstring.
        config=make_config_no_stall_overrides(),
    )
    notify_calls: list[tuple] = []
    driver.run_coord = lambda args, **kw: notify_calls.append(args) or 0  # type: ignore[assignment]

    assert driver.run() == EXIT_DEADLINE
    assert notify_calls.count(("notify",)) > 1, notify_calls

    err = capsys.readouterr().err
    assert err.count("no state change in") > 1, err


def test_smaller_stall_never_produces_fewer_nudges(driver_factory):
    """Regression pin for the #1593 inversion: a SMALLER ``--stall`` must
    never yield fewer nudges than a larger one over the same run. Under the
    one-shot latch, lowering ``--stall`` made things actively worse — the
    single available nudge fired earlier, while the worker was reliably still
    busy, guaranteeing no follow-up ever recorded the completion."""

    def nudge_count(stall_mins: float) -> int:
        driver = driver_factory(
            [board(status="running")],
            opts=DriveOptions(
                machine="precision",
                poll=1.0,
                stall_mins=stall_mins,
                deadline_mins=12.0 / 60.0,
                notify=True,
            ),
            # #2649: see `test_stalled_stage_gets_re_nudged_on_every_stall_window`.
            config=make_config_no_stall_overrides(),
        )
        calls: list[tuple] = []
        driver.run_coord = lambda args, **kw: calls.append(args) or 0  # type: ignore[assignment]
        driver.run()
        return calls.count(("notify",))

    small = nudge_count(1.0 / 60.0)
    large = nudge_count(5.0 / 60.0)
    assert small >= large > 0, (small, large)


def test_fingerprint_change_clears_the_published_stall_nudge(driver_factory, monkeypatch):
    """#2648: once the board fingerprint moves on, a nudge published for the
    stage that just finished must not go on convicting the NEXT stage's own
    assignment of the same stall — the notifier's `nudged_at` is per-ISSUE,
    not per-assignment, so a record left behind would re-fire
    ``stall_nudged`` for every later leg of this same issue.

    Spies on `_publish_stall_nudge`/`_clear_stall_nudge` directly (rather
    than reading final store state) because a long enough run can
    legitimately re-nudge the NEW fingerprint too — that's a fresh, correct
    stall, not the bug — and because `_clear_stall_nudge` also fires
    harmlessly on the very first tick (the initial "" fingerprint sentinel
    "changing" to whatever the board actually says). What must hold is the
    ORDER: the publish for the stalled stage is followed, later, by a
    clear — proof the fingerprint-change branch that retracts it actually
    ran after the nudge, not just once at start-up.
    """
    import coord.drive as drive_mod  # noqa: PLC0415

    events: list[tuple] = []
    monkeypatch.setattr(
        drive_mod, "_publish_stall_nudge",
        lambda repo, issue, **kw: events.append(("publish", repo, issue)),
    )
    monkeypatch.setattr(
        drive_mod, "_clear_stall_nudge",
        lambda repo, issue: events.append(("clear", repo, issue)),
    )

    stalled = board(status="running")
    advanced = board(status="running", test_state="passed")
    driver = driver_factory(
        # Long enough stalled to publish at least one nudge, then the
        # fingerprint moves — held there for the rest of the run.
        [stalled] * 6 + [advanced] * 20,
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            stall_mins=2.0 / 60.0,   # 2 "seconds" in the fake clock's units
            deadline_mins=6.0 / 60.0,
            notify=False,
        ),
        # #2649: see `test_stalled_stage_gets_re_nudged_on_every_stall_window`.
        config=make_config_no_stall_overrides(),
    )

    assert driver.run() == EXIT_DEADLINE
    assert ("publish", REPO, ISSUE) in events, events
    first_publish = events.index(("publish", REPO, ISSUE))
    # A clear for the SAME repo#issue must appear strictly after that
    # publish — the fingerprint-change branch retracting it once the
    # pipeline actually advances past the stalled stage.
    assert ("clear", REPO, ISSUE) in events[first_publish + 1:], events


def test_stall_threshold_per_type_override_suppresses_a_false_positive(
    driver_factory, capsys,
):
    """#2649: a `work` stage configured with its own (larger) stall override
    must not be nudged before THAT threshold elapses, even though the flat
    ``--stall`` fallback alone would have fired almost immediately. Proven
    by racing the override against the deadline: the run's whole life
    (4 fake-clock units) sits entirely under the 5-unit override, so if the
    flat 1-unit default were still governing, at least one nudge would have
    fired long before the deadline — none may.
    """
    cfg = make_config()
    cfg.pipeline.stall_thresholds = {"work": 5.0}
    driver = driver_factory(
        [board(status="running", type="work")],
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            stall_mins=1.0 / 60.0,  # flat fallback: 1 fake-clock unit
            deadline_mins=4.0 / 60.0,  # 4 units — under the 5-unit override
            notify=True,
        ),
        config=cfg,
    )
    notify_calls: list[tuple] = []
    driver.run_coord = lambda args, **kw: notify_calls.append(args) or 0  # type: ignore[assignment]

    assert driver.run() == EXIT_DEADLINE
    assert notify_calls.count(("notify",)) == 0, notify_calls
    err = capsys.readouterr().err
    assert "no state change" not in err, err


def test_stall_threshold_per_type_override_still_fires_once_exceeded(
    driver_factory,
):
    """The other half of the #2649 fix: a per-type override is a LARGER
    threshold, not a disabled one — once the stage genuinely outlives it,
    the nudge still fires."""
    cfg = make_config()
    cfg.pipeline.stall_thresholds = {"work": 2.0}
    driver = driver_factory(
        [board(status="running", type="work")],
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            stall_mins=1.0 / 60.0,
            deadline_mins=6.0 / 60.0,  # comfortably past the 2-unit override
            notify=True,
        ),
        config=cfg,
    )
    notify_calls: list[tuple] = []
    driver.run_coord = lambda args, **kw: notify_calls.append(args) or 0  # type: ignore[assignment]

    assert driver.run() == EXIT_DEADLINE
    assert notify_calls.count(("notify",)) >= 1, notify_calls


def test_stall_threshold_falls_back_to_flat_stall_for_an_unlisted_type(
    driver_factory,
):
    """A type with no ``stall_thresholds`` entry (e.g. ``review``) keeps
    exactly the pre-#2649 flat-``--stall`` behaviour — the per-type table
    must never silently apply to a type nobody configured."""
    cfg = make_config()
    cfg.pipeline.stall_thresholds = {"work": 999.0}  # would never fire in this run
    driver = driver_factory(
        [board(status="running", type="review")],
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            stall_mins=1.0 / 60.0,  # flat fallback: 1 fake-clock unit
            deadline_mins=4.0 / 60.0,
            notify=True,
        ),
        config=cfg,
    )
    notify_calls: list[tuple] = []
    driver.run_coord = lambda args, **kw: notify_calls.append(args) or 0  # type: ignore[assignment]

    assert driver.run() == EXIT_DEADLINE
    assert notify_calls.count(("notify",)) >= 1, notify_calls


def test_default_stall_thresholds_cover_the_2649_evidence():
    """Pin the #2649 built-in defaults: both measured false positives
    (a `work` stage at ~26m, the Test-stage `type="smoke"` at ~22m,
    claude-coordinator#2572) must sit comfortably under the built-in
    per-type threshold, and an unlisted type (`review`, measured at ~8m in
    that same run) must fall back to the caller's own default rather than
    picking up a `work`/`smoke`-sized threshold it never earned."""
    pipeline = make_config().pipeline
    assert pipeline.stall_threshold_secs(["work"], default_secs=1200.0) > 26 * 60.0
    assert pipeline.stall_threshold_secs(["smoke"], default_secs=1200.0) > 22 * 60.0
    assert pipeline.stall_threshold_secs(["review"], default_secs=1200.0) == 1200.0


def test_the_config_path_is_threaded_onto_every_coord_subprocess(driver_factory):
    """A `coord drive --config X` run must not dispatch against a different
    config than it reads. The bash driver ran a bare `coord` and had this gap."""
    driver = driver_factory(
        [board(status="failed", failure_reason="boom")],
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            deadline_mins=0.05,
            config_path="/tmp/custom.yml",
        ),
    )
    driver.run()
    argvs = driver.recorded  # type: ignore[attr-defined]
    assert argvs, "expected at least one coord subprocess"
    for argv in argvs:
        assert argv[-2:] == ["--config", "/tmp/custom.yml"], argv


def test_no_config_flag_is_added_when_none_was_given(driver_factory):
    driver = driver_factory(
        [board(status="failed", failure_reason="boom")],
        opts=DriveOptions(machine="precision", poll=1.0, deadline_mins=0.05),
    )
    driver.run()
    for argv in driver.recorded:  # type: ignore[attr-defined]
        assert "--config" not in argv


def test_coord_argv_is_overridable_for_tests(monkeypatch):
    monkeypatch.setenv("COORD_DRIVE_COORD_BIN", "/x/coord --config /y")
    assert coord_argv() == ["/x/coord", "--config", "/y"]


def test_coord_argv_falls_back_to_the_module_when_not_on_path(monkeypatch):
    monkeypatch.delenv("COORD_DRIVE_COORD_BIN", raising=False)
    monkeypatch.setattr("coord.drive.shutil.which", lambda name: None)
    assert coord_argv()[-2:] == ["-m", "coord.cli"]


# ── #1809: the fallback must actually run, not just be shaped right ─────────
#
# The two tests above assert on coord_argv()'s RETURN VALUE only. Nothing
# ever executed the argv it returns — so `coord/cli.py` shipped with no
# `if __name__ == "__main__":` guard, meaning `python -m coord.cli <args>`
# silently imported the module (building every click.group/add_command) and
# exited 0 having run nothing and printed nothing. That import-only exit is
# exactly the path `coord_argv()` falls back to whenever `coord` isn't on
# PATH — a venv whose bin isn't exported, a systemd user unit, a
# non-interactive ssh session (#402) — so on those hosts every `coord`
# subprocess the driver or the drive queue spawned (`coord assign`, `coord
# drive --tmux`, ...) was a silent no-op that reported success. Both tests
# below run a real subprocess and assert on its OUTPUT, not just its exit
# code, because the broken path also exits 0 — a bare `returncode == 0`
# assertion would pass against the very bug this guards against.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def test_python_dash_m_coord_cli_prints_version_and_exits_0():
    """The direct acceptance check: ``python -m coord.cli --version`` must
    actually run ``main()``, not just import the module and exit."""
    result = subprocess.run(
        [sys.executable, "-m", "coord.cli", "--version"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert _VERSION_RE.search(result.stdout), (
        f"expected a version string on stdout, got: {result.stdout!r} "
        f"(stderr={result.stderr!r})"
    )


def test_coord_argv_fallback_argv_is_actually_executable(monkeypatch):
    """The direct regression guard: with ``coord`` scrubbed from PATH (the
    #402 scenario), the argv ``coord_argv()`` hands to every driver/queue
    subprocess call must run a real command — invoked here exactly as
    ``Driver``/``launch_drive_in_tmux``/the drive queue invoke it (argv +
    extra args, no shell)."""
    monkeypatch.delenv("COORD_DRIVE_COORD_BIN", raising=False)
    monkeypatch.setattr("coord.drive.shutil.which", lambda name: None)
    argv = coord_argv()
    assert argv[-2:] == ["-m", "coord.cli"]

    result = subprocess.run(
        [*argv, "--version"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert _VERSION_RE.search(result.stdout), (
        f"expected a version string on stdout, got: {result.stdout!r} "
        f"(stderr={result.stderr!r})"
    )


# ═══════════════════════════════════════════════════════════════════════════
# #2024: a --fix-of round whose Test stage is human-attended by policy
#
# JDonaghy/vimcode#635, 2026-08-08. The issue carries `test-mode:smoke`, the
# per-issue policy (#685) that switches the HEADLESS Test stage off:
# `dispatch_pending_smoke` skips it on every tick by design. Review dispatch is
# meanwhile held until the CURRENT work row carries a passed/skipped verdict
# (`pipeline.test_precedes_review`). A fix round is a new work row with its own
# empty `test_state`, so an unattended round cannot advance at all — twice on
# one issue, 25 minutes then 160, each cleared within minutes of an operator
# running `coord test <fix_aid> --passed` by hand.
#
# Before this, `_decide_test` returned a bare `_wait()` for it — the same
# `no state change` counter #2019 is about, against an event that never comes.
# ═══════════════════════════════════════════════════════════════════════════


def fix_round_awaiting_attended_test(**kw) -> IssueState:
    base = dict(
        work_aid="78cfb47e0b99",
        work_test_state="",
        work_review_iter=1,
        issue_test_mode="smoke",
    )
    base.update(kw)
    return done_work(**base)


def test_a_fix_round_with_no_attended_test_stage_escalates_instead_of_waiting():
    action = step(fix_round_awaiting_attended_test())
    assert action.is_exit
    assert action.exit_code == EXIT_DEAD_END
    assert "test-mode:smoke" in action.message
    assert "coord test 78cfb47e0b99 --passed" in action.message
    assert "no state change" not in action.message


def test_the_attended_test_dead_end_records_a_test_stage_escalation():
    action = step(fix_round_awaiting_attended_test())
    assert action.command[:4] == ("escalate", "record", REPO, str(ISSUE))
    assert action.command[action.command.index("--stage") + 1] == "test"
    assert action.command[action.command.index("--assignment") + 1] == "78cfb47e0b99"


def test_an_unlabelled_issue_still_waits_for_coord_to_dispatch_the_stage():
    """The regression bar for #1426's wait: with no `test-mode:*` policy the
    daemon IS going to dispatch, so "no verdict yet" must stay a WAIT. This is
    the same assertion as
    `test_no_test_verdict_yet_waits_for_coord_to_dispatch_the_stage`, restated
    here because it is what stops the new shape from firing on every
    freshly-completed row on the fleet."""
    action = step(fix_round_awaiting_attended_test(issue_test_mode=""))
    assert action.kind == WAIT
    assert action.command == ()


def test_skip_test_still_records_a_verdict_under_test_mode_smoke():
    """`--skip-test` is a live Test-stage move the operator explicitly asked
    for; the dead end must not escalate past it."""
    action = step(
        fix_round_awaiting_attended_test(),
        DriveOptions(machine="precision", skip_test=True),
    )
    assert action.command == (
        "test", "--skipped", "--reason", "coord drive --skip-test", "78cfb47e0b99",
    )


def test_an_attended_test_stage_in_flight_is_not_a_dead_end():
    """The interactive smoke this policy asks for is a real board row, so it
    shows up as `active`. The predicate's hard precondition already refuses —
    asserted here through the driver, since this is the one thing standing
    between the shape and a false positive on the policy's happy path."""
    action = step(
        fix_round_awaiting_attended_test(active_count=1, active_types=("smoke",))
    )
    assert action.kind == WAIT
