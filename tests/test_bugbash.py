"""Tests for coord/bugbash.py — the per-platform find -> dedupe -> file ->
queue loop engine (#3487). Exercises dedupe (open match, closed-issue
regression, no match), filing/queueing with the `coord` seam faked,
loop-termination (zero-findings, round cap, cost cap), and `--dry-run`
filing nothing."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from coord.bugbash import (
    BugbashConfig,
    BugbashLane,
    DedupeVerdict,
    ExploreOutcome,
    Finding,
    _pick_lane_machine,
    build_exploration_briefing,
    compose_finding_issue_title,
    dedupe_finding,
    discover_lanes,
    file_finding,
    parse_findings_block,
    run_bugbash,
)


def _finding(
    title: str = "Extension install flow crashes",
    platform: str = "win-native",
    repo: str = "vimcode",
    **kwargs,
) -> Finding:
    defaults = dict(
        suspected_repo=repo,
        expected="extension installs without error",
        actual="app crashes on install",
        repro="open extensions panel, click install",
        evidence="capture.png",
    )
    defaults.update(kwargs)
    return Finding(title=title, platform=platform, repo=repo, **defaults)


# ── dedupe_finding ───────────────────────────────────────────────────────


class TestDedupeFinding:
    def test_no_match_is_new(self):
        finding = _finding(title="Terminal splits render with a 1px gap")
        result = dedupe_finding(finding, open_issues=[], closed_issues=[])
        assert result.verdict is DedupeVerdict.NEW
        assert result.matched_number is None

    def test_open_issue_match_is_duplicate(self):
        finding = _finding(title="Extension install flow crashes on Windows")
        open_issues = [
            {"number": 42, "title": "[bugbash:win-native] Extension install flow crashes on Windows"},
        ]
        result = dedupe_finding(finding, open_issues=open_issues, closed_issues=[])
        assert result.verdict is DedupeVerdict.DUPLICATE
        assert result.matched_number == 42

    def test_closed_issue_match_is_regression(self):
        finding = _finding(title="Extension install flow crashes on Windows")
        closed_issues = [
            {"number": 1583, "title": "[bugbash:win-native] Extension install flow crashes on Windows"},
        ]
        result = dedupe_finding(finding, open_issues=[], closed_issues=closed_issues)
        assert result.verdict is DedupeVerdict.REGRESSION
        assert result.matched_number == 1583

    def test_open_match_wins_over_closed_match(self):
        finding = _finding(title="Extension install flow crashes on Windows")
        open_issues = [{"number": 10, "title": "Extension install flow crashes on Windows"}]
        closed_issues = [{"number": 5, "title": "Extension install flow crashes on Windows"}]
        result = dedupe_finding(finding, open_issues=open_issues, closed_issues=closed_issues)
        assert result.verdict is DedupeVerdict.DUPLICATE
        assert result.matched_number == 10

    def test_unrelated_titles_do_not_match(self):
        finding = _finding(title="Terminal splits render with a 1px gap")
        open_issues = [{"number": 1, "title": "Dark theme uses wrong accent colour"}]
        result = dedupe_finding(finding, open_issues=open_issues, closed_issues=[])
        assert result.verdict is DedupeVerdict.NEW

    def test_platform_tagged_issue_on_different_platform_does_not_match(self):
        finding = _finding(title="Extension install flow crashes", platform="mac-native")
        open_issues = [
            {"number": 7, "title": "[bugbash:win-native] Extension install flow crashes"},
        ]
        result = dedupe_finding(finding, open_issues=open_issues, closed_issues=[])
        assert result.verdict is DedupeVerdict.NEW

    def test_untagged_open_issue_matches_regardless_of_platform(self):
        # A hand-filed issue carries no [bugbash:<platform>] tag — still
        # eligible to match on title alone.
        finding = _finding(title="Extension install flow crashes", platform="mac-native")
        open_issues = [{"number": 9, "title": "Extension install flow crashes"}]
        result = dedupe_finding(finding, open_issues=open_issues, closed_issues=[])
        assert result.verdict is DedupeVerdict.DUPLICATE
        assert result.matched_number == 9


# ── parse_findings_block ─────────────────────────────────────────────────


class TestParseFindingsBlock:
    def test_parses_valid_block(self):
        text = (
            "Here's what I found.\n\n"
            "```bugbash-findings\n"
            '[{"title": "Crash on install", "expected": "e", "actual": "a", '
            '"repro": "r", "evidence": "ev"}]\n'
            "```\n"
        )
        findings = parse_findings_block(text, platform="win-native", repo="vimcode")
        assert len(findings) == 1
        assert findings[0].title == "Crash on install"
        assert findings[0].platform == "win-native"
        assert findings[0].repo == "vimcode"

    def test_no_block_returns_empty(self):
        assert parse_findings_block("nothing to see here", platform="win-native", repo="vimcode") == []

    def test_malformed_json_returns_empty(self):
        text = "```bugbash-findings\nnot json\n```"
        assert parse_findings_block(text, platform="win-native", repo="vimcode") == []

    def test_entry_missing_required_field_is_dropped(self):
        text = (
            "```bugbash-findings\n"
            '[{"title": "x", "expected": "e", "actual": "a", "repro": "", "evidence": "ev"}]\n'
            "```"
        )
        assert parse_findings_block(text, platform="win-native", repo="vimcode") == []

    def test_empty_array_is_a_clean_round(self):
        text = "```bugbash-findings\n[]\n```"
        assert parse_findings_block(text, platform="win-native", repo="vimcode") == []


# ── discover_lanes ────────────────────────────────────────────────────────


@dataclass
class _FakeMachine:
    name: str
    repos: list
    capabilities: list = field(default_factory=list)


@dataclass
class _FakeDriverCfg:
    kind: str = ""
    capability: str = ""
    routes: list = field(default_factory=list)


@dataclass
class _FakeAcceptanceConfig:
    drivers: dict


@dataclass
class _FakeConfig:
    machines: list
    acceptance: _FakeAcceptanceConfig


class TestDiscoverLanes:
    def test_no_driver_returns_no_lanes(self):
        cfg = _FakeConfig(machines=[], acceptance=_FakeAcceptanceConfig(drivers={}))
        assert discover_lanes(cfg, "vimcode") == []

    def test_route_with_capable_machine_becomes_a_lane(self):
        machine = _FakeMachine(name="pc1", repos=["vimcode"], capabilities=["windows"])
        entry = _FakeDriverCfg(routes=[_FakeDriverCfg(kind="win-native", capability="windows")])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        lanes = discover_lanes(cfg, "vimcode", reference_backend="win-native")
        assert len(lanes) == 1
        assert lanes[0].platform == "win-native"
        assert lanes[0].machine == "pc1"
        assert lanes[0].reference is True

    def test_route_with_no_capable_machine_is_omitted(self):
        machine = _FakeMachine(name="pc1", repos=["vimcode"], capabilities=[])
        entry = _FakeDriverCfg(routes=[_FakeDriverCfg(kind="win-native", capability="windows")])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        assert discover_lanes(cfg, "vimcode") == []

    def test_non_lane_kind_is_ignored(self):
        machine = _FakeMachine(name="pc1", repos=["vimcode"], capabilities=[])
        entry = _FakeDriverCfg(routes=[_FakeDriverCfg(kind="cli-pytest", capability="")])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        assert discover_lanes(cfg, "vimcode") == []

    def test_top_level_driver_without_routes(self):
        machine = _FakeMachine(name="mac1", repos=["vimcode"], capabilities=["macos"])
        entry = _FakeDriverCfg(kind="mac-native", capability="macos", routes=[])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        lanes = discover_lanes(cfg, "vimcode")
        assert [l.platform for l in lanes] == ["mac-native"]

    def test_pick_lane_machine_requires_repo_membership(self):
        machine = _FakeMachine(name="pc1", repos=["other"], capabilities=["windows"])
        assert _pick_lane_machine(_FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig({})), "vimcode", "windows") is None


class TestBuildExplorationBriefing:
    def test_includes_reference_backend_and_checklist(self):
        lane = BugbashLane(platform="win-native", driver_kind="win-native", machine="pc1", capability="windows")
        out = build_exploration_briefing(lane, reference_backend="mac-native", checklist=("panels", "menus"))
        assert "mac-native" in out
        assert "panels" in out
        assert "menus" in out
        assert "bugbash-findings" in out


# ── filing (coord seam faked) ────────────────────────────────────────────


class FakeRunner:
    """Fakes the `coord` seam (`CoordRunner`) — records every call instead
    of touching GitHub or the drive queue."""

    def __init__(self, next_issue_number: int = 100):
        self.calls: list[list[str]] = []
        self._next_issue_number = next_issue_number

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["issue", "create"]:
            number = self._next_issue_number
            self._next_issue_number += 1
            slug = args[2]
            return f"#{number} ({slug}) created\n"
        if args[:2] == ["drive-queue", "add"]:
            return "queued\n"
        raise AssertionError(f"unexpected coord call: {args}")


def _lane(platform="win-native", machine="pc1"):
    return BugbashLane(platform=platform, driver_kind=platform, machine=machine, capability="")


class TestFileFinding:
    def test_new_finding_files_and_queues(self):
        finding = _finding()
        dedupe = dedupe_finding(finding, [], [])
        runner = FakeRunner(next_issue_number=200)
        result = file_finding(finding, dedupe, _lane(), runner, dry_run=False)

        assert result.filed is True
        assert result.queued is True
        assert result.issue_number == 200
        assert runner.calls[0][:3] == ["issue", "create", "vimcode"]
        assert runner.calls[1] == ["drive-queue", "add", "vimcode", "200", "--machine", "pc1"]

    def test_duplicate_finding_never_calls_runner(self):
        finding = _finding()
        open_issues = [{"number": 5, "title": finding.title}]
        dedupe = dedupe_finding(finding, open_issues, [])
        runner = FakeRunner()
        result = file_finding(finding, dedupe, _lane(), runner, dry_run=False)

        assert result.filed is False
        assert result.queued is False
        assert result.issue_number == 5
        assert runner.calls == []

    def test_regression_finding_files_with_regression_note(self):
        finding = _finding()
        closed_issues = [{"number": 1583, "title": finding.title}]
        dedupe = dedupe_finding(finding, [], closed_issues)
        runner = FakeRunner(next_issue_number=300)
        result = file_finding(finding, dedupe, _lane(), runner, dry_run=False)

        assert result.filed is True
        create_call = runner.calls[0]
        evidence_idx = create_call.index("--evidence") + 1
        assert "1583" in create_call[evidence_idx]

    def test_dry_run_files_nothing(self):
        finding = _finding()
        dedupe = dedupe_finding(finding, [], [])
        runner = FakeRunner()
        result = file_finding(finding, dedupe, _lane(), runner, dry_run=True)

        assert result.filed is False
        assert result.queued is False
        assert result.preview_title is not None
        assert runner.calls == []

    def test_filed_issue_title_carries_platform_tag(self):
        finding = _finding(platform="mac-native")
        assert compose_finding_issue_title(finding) == "[bugbash:mac-native] " + finding.title

    def test_create_call_carries_acceptance_line(self):
        finding = _finding()
        dedupe = dedupe_finding(finding, [], [])
        runner = FakeRunner()
        file_finding(finding, dedupe, _lane(), runner, dry_run=False)
        create_call = runner.calls[0]
        evidence_idx = create_call.index("--evidence") + 1
        assert "Tier-1 shared conformance scenario" in create_call[evidence_idx]

    def test_missing_lane_for_real_filing_raises(self):
        finding = _finding()
        dedupe = dedupe_finding(finding, [], [])
        runner = FakeRunner()
        with pytest.raises(RuntimeError):
            file_finding(finding, dedupe, None, runner, dry_run=False)

    def test_create_output_without_number_raises(self):
        finding = _finding()
        dedupe = dedupe_finding(finding, [], [])

        def bad_runner(args):
            return "something went wrong, no number here\n"

        with pytest.raises(RuntimeError):
            file_finding(finding, dedupe, _lane(), bad_runner, dry_run=False)


# ── run_bugbash: the round loop ──────────────────────────────────────────


def _make_explorer(rounds_findings: list[list[Finding]], costs: list[float] | None = None):
    """Builds an Explorer that returns `rounds_findings[round_num - 1]` for
    every lane call in that round (ignoring lane identity — fine for a
    single-lane test config), then an empty outcome forever after."""
    costs = costs or [0.0] * len(rounds_findings)

    def explorer(lane, round_num):
        idx = round_num - 1
        if idx < len(rounds_findings):
            return ExploreOutcome(findings=tuple(rounds_findings[idx]), cost=costs[idx])
        return ExploreOutcome(findings=(), cost=0.0)

    return explorer


def _config(lanes=None, **kwargs) -> BugbashConfig:
    lanes = lanes if lanes is not None else [_lane()]
    defaults = dict(repo="vimcode", lanes=lanes, reference_backend="mac-native", confirm_rounds=0)
    defaults.update(kwargs)
    return BugbashConfig(**defaults)


class TestRunBugbashTermination:
    def test_terminates_on_zero_findings(self):
        config = _config(max_rounds=10)
        explorer = _make_explorer([[_finding(title="Bug A")], []])
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "zero_findings"
        assert len(report.rounds) == 2
        assert report.total_filed == 1

    def test_terminates_on_round_cap(self):
        config = _config(max_rounds=3)
        # A fresh (still-new) finding every round, so it never naturally
        # goes to zero — round cap must be what stops it.
        explorer = _make_explorer(
            [[_finding(title="Bug A")], [_finding(title="Bug B")], [_finding(title="Bug C")]]
        )
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "round_cap"
        assert len(report.rounds) == 3

    def test_terminates_on_cost_cap(self):
        config = _config(max_rounds=10, cost_cap_total=5.0)
        explorer = _make_explorer(
            [[_finding(title="Bug A")], [_finding(title="Bug B")], [_finding(title="Bug C")]],
            costs=[3.0, 3.0, 3.0],
        )
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "cost_cap"
        assert report.total_cost >= 5.0
        # Stopped as soon as the cap was reached — not all 10 rounds ran.
        assert len(report.rounds) < 10

    def test_per_lane_cost_cap_skips_lane_in_later_rounds(self):
        expensive_lane = _lane(platform="win-native", machine="pc1")
        cheap_lane = _lane(platform="mac-native", machine="mac1")
        config = _config(
            lanes=[expensive_lane, cheap_lane], max_rounds=3, cost_cap_per_lane=2.0,
        )
        calls = []

        def explorer(lane, round_num):
            calls.append((lane.platform, round_num))
            cost = 5.0 if lane.platform == "win-native" else 0.5
            title = f"Bug {lane.platform} r{round_num}"
            return ExploreOutcome(findings=(_finding(title=title, platform=lane.platform),), cost=cost)

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        # win-native only ever explored once (round 1) before its cumulative
        # cost (5.0) blew past the 2.0 per-lane cap.
        win_calls = [c for c in calls if c[0] == "win-native"]
        assert len(win_calls) == 1
        assert report.rounds[1].skipped_lanes == ["win-native"]

    def test_duplicate_only_round_still_terminates_zero_findings(self):
        # A round whose only finding is a DUPLICATE of an already-open
        # issue must count as zero NEW findings, terminating the loop —
        # a duplicate is not "nothing happened," but it is not "something
        # new to file" either.
        config = _config(max_rounds=10)
        finding = _finding(title="Already tracked bug")
        explorer = _make_explorer([[finding]])
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [{"number": 1, "title": finding.title}],
            closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "zero_findings"
        assert report.total_filed == 0
        assert report.rounds[0].filings[0].verdict is DedupeVerdict.DUPLICATE


class TestRunBugbashDryRun:
    def test_dry_run_files_nothing_across_multiple_rounds(self):
        config = _config(max_rounds=3, dry_run=True, confirm_rounds=0)
        explorer = _make_explorer(
            [[_finding(title="Bug A")], [_finding(title="Bug B")], []]
        )
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.total_filed == 0
        assert runner.calls == []
        assert len(report.would_file) == 2
        assert report.termination_reason == "zero_findings"

    def test_dry_run_reported_findings_are_not_visible_to_later_round_dedupe(self):
        # A dry run never actually creates the issue, so the SAME finding
        # reappearing next round must NOT be treated as an open duplicate —
        # proof that dry-run previews never leak into the open-issues view.
        config = _config(max_rounds=2, dry_run=True, confirm_rounds=0)
        finding = _finding(title="Bug A")
        explorer = _make_explorer([[finding], [finding]])
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.rounds[1].filings[0].verdict is DedupeVerdict.NEW
        assert runner.calls == []


class TestRunBugbashConfirmGate:
    def test_confirm_declined_files_nothing_that_round(self):
        config = _config(max_rounds=2, confirm_rounds=1)
        explorer = _make_explorer([[_finding(title="Bug A")], []])
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
            confirm=lambda round_num, candidates: False,
        )
        assert runner.calls == []
        assert report.rounds[0].declined is True
        assert report.rounds[0].filings[0].preview_title is not None

    def test_confirm_accepted_files_normally(self):
        config = _config(max_rounds=2, confirm_rounds=1)
        explorer = _make_explorer([[_finding(title="Bug A")], []])
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
            confirm=lambda round_num, candidates: True,
        )
        assert report.total_filed == 1
        assert runner.calls

    def test_no_confirm_callback_during_confirm_rounds_declines_by_default(self):
        config = _config(max_rounds=1, confirm_rounds=1)
        explorer = _make_explorer([[_finding(title="Bug A")]])
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert runner.calls == []
        assert report.total_filed == 0

    def test_rounds_past_confirm_rounds_file_without_confirmation(self):
        config = _config(max_rounds=2, confirm_rounds=1)
        explorer = _make_explorer([[_finding(title="Bug A")], [_finding(title="Bug B")]])
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
            confirm=lambda round_num, candidates: False,
        )
        # Round 1 declined (confirm gate), round 2 files freely.
        assert report.rounds[0].filings[0].filed is False
        assert report.rounds[1].filings[0].filed is True
