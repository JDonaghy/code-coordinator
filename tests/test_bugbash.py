"""Tests for coord/bugbash.py — the per-platform find -> dedupe -> file ->
queue loop engine (#3487). Exercises dedupe (open match, closed-issue
regression, no match), filing/queueing with the `coord` seam faked,
loop-termination (zero-findings, round cap, cost cap, lane failure), and
`--dry-run` filing nothing. Also covers the production explorer
(`coord/commands/bugbash.py::_dispatch_and_await_lane`) against faked
`dispatch_with_retry`/`poll_until_terminal`/log-fetch seams (review fix
iteration 1, #3487: a lane's dispatch/poll/log failure must be
distinguishable from a genuine zero-findings pass, and the production
explorer must reuse the shared `poll_until_terminal` poller rather than a
third hand-rolled one).

#3569 adds: a fixed 30-minute lane timeout used to orphan a still-running
explorer and discard its findings. `TestDispatchAndAwaitLane` now also
covers the keep-polling/stall/cancel behaviour (a lane still producing
output and under its cost cap keeps waiting past `--lane-timeout`; a
genuine stall gets cancelled, never left running unattended; a cancel
racing a just-finished explorer harvests it inline instead of discarding
it); `TestHarvestOutcome` covers the `coord bugbash harvest` recovery path
(`harvest_outcome`) against the SAME dedupe/file logic `run_bugbash` uses;
`TestBugbashCli` covers the `--lane-timeout` CLI flag reaching
`_dispatch_and_await_lane`, and the `run`/`harvest` subcommand group
wiring via Click's `CliRunner`."""

from __future__ import annotations

import shlex
import threading
import time
from dataclasses import dataclass, field

import pytest

from coord.bugbash import (
    BugbashConfig,
    BugbashLane,
    CATALOGUE_PATH,
    COVERAGE_FENCE,
    CoverageSummary,
    DedupeVerdict,
    ExploreOutcome,
    Finding,
    Journey,
    JourneyOutcome,
    RoundReport,
    UNAVAILABLE_FENCE,
    UnavailableLane,
    _apply_outcome_to_round,
    _pick_lane_machine,
    build_exploration_briefing,
    compose_finding_issue_title,
    dedupe_finding,
    discover_lanes,
    file_finding,
    harvest_outcome,
    journeys_for_lane,
    parse_catalogue,
    parse_coverage_block,
    parse_findings_block,
    parse_unavailable_report,
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

    def test_short_title_matches_tagged_issue_despite_tag_word_dilution(self):
        # #3546: a short real-world title ("Caption buttons unclickable", 3
        # words) scored only 0.5 Jaccard similarity against the tagged issue
        # title BEFORE the tag's own 3 extra tokens ("bugbash", "win",
        # "native") were stripped for comparison — below the 0.6 threshold,
        # so the exact same bug reported twice was never recognised as a
        # duplicate. This is the "titles differing enough to defeat
        # matching" root cause behind vimcode#1656/#1660 being filed twice.
        finding = _finding(title="Caption buttons unclickable", platform="win-native")
        open_issues = [{"number": 1656, "title": "[bugbash:win-native] Caption buttons unclickable"}]
        result = dedupe_finding(finding, open_issues=open_issues, closed_issues=[])
        assert result.verdict is DedupeVerdict.DUPLICATE
        assert result.matched_number == 1656

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
        result = parse_findings_block(text, platform="win-native", repo="vimcode")
        assert result.protocol_error == ""
        assert len(result.findings) == 1
        assert result.findings[0].title == "Crash on install"
        assert result.findings[0].platform == "win-native"
        assert result.findings[0].repo == "vimcode"
        assert result.findings[0].incomplete is False
        assert result.findings[0].journey_id == ""

    def test_journey_id_is_parsed_through_from_the_entry(self):
        """#3580 requirement 2: a `journey_id` in the worker's JSON entry
        makes it onto the parsed Finding."""
        text = (
            "```bugbash-findings\n"
            '[{"title": "t", "expected": "e", "actual": "a", "repro": "r", '
            '"evidence": "ev", "journey_id": "vim-dd-deletes-line"}]\n'
            "```\n"
        )
        result = parse_findings_block(text, platform="win-native", repo="vimcode")
        assert result.findings[0].journey_id == "vim-dd-deletes-line"

    def test_no_block_with_no_clean_statement_is_a_protocol_error(self):
        # #3517: a dropped/forgotten fence must NOT silently read as a clean
        # (zero-findings) pass — it's indistinguishable from a real finding
        # that got lost, so it must come back as a protocol error instead.
        result = parse_findings_block("nothing to see here", platform="win-native", repo="vimcode")
        assert result.findings == ()
        assert result.protocol_error != ""

    def test_no_block_with_explicit_clean_statement_is_a_clean_pass(self):
        text = "I walked the full checklist and found zero findings this round."
        result = parse_findings_block(text, platform="win-native", repo="vimcode")
        assert result.findings == ()
        assert result.protocol_error == ""

    def test_malformed_json_is_a_protocol_error_not_empty(self):
        # #3517: this used to silently return `[]`, indistinguishable from a
        # genuine clean pass. A lane protocol slip must be reported, not
        # dropped.
        text = "```bugbash-findings\nnot json\n```"
        result = parse_findings_block(text, platform="win-native", repo="vimcode")
        assert result.findings == ()
        assert result.protocol_error != ""
        assert "JSON" in result.protocol_error

    def test_non_list_json_is_a_protocol_error(self):
        text = '```bugbash-findings\n{"title": "not a list"}\n```'
        result = parse_findings_block(text, platform="win-native", repo="vimcode")
        assert result.findings == ()
        assert result.protocol_error != ""

    def test_entry_missing_evidence_is_kept_with_derived_evidence_and_incomplete_flag(self):
        # #3517: the live finding this bug was filed from — a valid block,
        # one entry with every field EXCEPT `evidence`. It must survive as a
        # real finding, not get silently dropped to "zero findings".
        text = (
            "```bugbash-findings\n"
            '[{"title": "idle vcd emits a cursor-hide burst", "expected": "silent", '
            '"actual": "25-byte burst every ~2s", "repro": "idle for 10s", '
            '"captures": ["probe-dump-1.txt"]}]\n'
            "```"
        )
        result = parse_findings_block(text, platform="tui-pty", repo="vimcode")
        assert result.protocol_error == ""
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.incomplete is True
        assert finding.missing_fields == ("evidence",)
        assert "probe-dump-1.txt" in finding.evidence

    def test_entry_missing_evidence_and_captures_derives_from_actual(self):
        text = (
            "```bugbash-findings\n"
            '[{"title": "x", "expected": "e", "actual": "a", "repro": "r"}]\n'
            "```"
        )
        result = parse_findings_block(text, platform="win-native", repo="vimcode")
        [finding] = result.findings
        assert finding.incomplete is True
        assert "a" in finding.evidence

    def test_entry_missing_repro_is_kept_with_placeholder_and_incomplete_flag(self):
        text = (
            "```bugbash-findings\n"
            '[{"title": "x", "expected": "e", "actual": "a", "repro": "", "evidence": "ev"}]\n'
            "```"
        )
        result = parse_findings_block(text, platform="win-native", repo="vimcode")
        assert result.protocol_error == ""
        [finding] = result.findings
        assert finding.incomplete is True
        assert "repro" in finding.missing_fields
        assert finding.repro != ""

    def test_empty_array_is_a_clean_round(self):
        text = "```bugbash-findings\n[]\n```"
        result = parse_findings_block(text, platform="win-native", repo="vimcode")
        assert result.findings == ()
        assert result.protocol_error == ""


class TestParseUnavailableReport:
    """#3566: the lane-unavailable reporting contract — a worker that hit a
    missing permission/session and stopped (per the briefing's hard rule)
    rather than improvising a workaround."""

    def test_fenced_report_is_extracted(self):
        text = (
            "I checked and Accessibility is not granted for this identity.\n\n"
            f"```{UNAVAILABLE_FENCE}\n"
            "AXIsProcessTrusted() is False for this worker's identity\n"
            "```\n"
        )
        reason = parse_unavailable_report(text)
        assert reason == "AXIsProcessTrusted() is False for this worker's identity"

    def test_no_fence_and_no_signature_is_empty(self):
        assert parse_unavailable_report("zero findings this round.") == ""

    def test_findings_fence_alone_does_not_trigger_unavailable(self):
        text = (
            "```bugbash-findings\n"
            '[{"title": "x", "expected": "e", "actual": "a", "repro": "r", '
            '"evidence": "ev"}]\n'
            "```"
        )
        assert parse_unavailable_report(text) == ""

    def test_driver_session_unavailable_json_signature_is_detected_without_a_fence(self):
        """#3566 ask #5's "or a driver session/permission failure" —
        caught even if the worker forgot to write the fence, because the
        native driver's own JSON verdict is right there in a tool result."""
        text = (
            'tool_result: [{"id": "session", "status": "unavailable", '
            '"message": "the screen is locked"}]'
        )
        reason = parse_unavailable_report(text)
        assert "unavailable" in reason or "locked" in reason

    def test_ax_trust_denial_signature_is_detected(self):
        text = "driver precheck failed: AXIsProcessTrusted() is False"
        assert parse_unavailable_report(text) != ""

    def test_empty_fence_falls_back_to_signature_scan(self):
        text = f"```{UNAVAILABLE_FENCE}\n```\nthe screen is locked for this session"
        assert parse_unavailable_report(text) != ""


# ── discover_lanes ────────────────────────────────────────────────────────


@dataclass
class _FakeMachine:
    name: str
    repos: list
    capabilities: list = field(default_factory=list)
    host: str = "example.local"


@dataclass
class _FakeDriverCfg:
    kind: str = ""
    capability: str = ""
    routes: list = field(default_factory=list)
    platforms: list = field(default_factory=list)


@dataclass
class _FakeAcceptanceConfig:
    drivers: dict


@dataclass
class _FakeConfig:
    machines: list
    acceptance: _FakeAcceptanceConfig


class _FakeHealthResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._p


class _FakeHealthClient:
    """A scripted `/health` responder (#3566) — mirrors
    `tests/test_smoke.py`'s own `_FakeClient`, kept local so
    `coord.bugbash`'s probe cross-check can be exercised here without a real
    network call. Default (`health={}`) fails OPEN — no `tool_versions` key,
    same as an agent that predates the probe — so every pre-existing
    `discover_lanes`/`_pick_lane_machine` test that doesn't care about this
    behaves exactly as it did before this fix."""

    def __init__(self, health: dict | None = None) -> None:
        self._health = health if health is not None else {}
        self.get_calls: list[str] = []

    def get(self, url, *, timeout) -> _FakeHealthResp:
        self.get_calls.append(url)
        return _FakeHealthResp(self._health)


class TestDiscoverLanes:
    def test_no_driver_returns_no_lanes(self):
        cfg = _FakeConfig(machines=[], acceptance=_FakeAcceptanceConfig(drivers={}))
        assert discover_lanes(cfg, "vimcode", http_client=_FakeHealthClient()) == []

    def test_route_with_capable_machine_becomes_a_lane(self):
        machine = _FakeMachine(name="pc1", repos=["vimcode"], capabilities=["windows"])
        entry = _FakeDriverCfg(routes=[_FakeDriverCfg(kind="win-native", capability="windows")])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        lanes = discover_lanes(
            cfg, "vimcode", reference_backend="win-native", http_client=_FakeHealthClient(),
        )
        assert len(lanes) == 1
        assert lanes[0].platform == "win-native"
        assert lanes[0].machine == "pc1"
        assert lanes[0].reference is True

    def test_route_with_no_capable_machine_is_omitted(self):
        machine = _FakeMachine(name="pc1", repos=["vimcode"], capabilities=[])
        entry = _FakeDriverCfg(routes=[_FakeDriverCfg(kind="win-native", capability="windows")])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        assert discover_lanes(cfg, "vimcode", http_client=_FakeHealthClient()) == []

    def test_non_lane_kind_is_ignored(self):
        machine = _FakeMachine(name="pc1", repos=["vimcode"], capabilities=[])
        entry = _FakeDriverCfg(routes=[_FakeDriverCfg(kind="cli-pytest", capability="")])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        assert discover_lanes(cfg, "vimcode", http_client=_FakeHealthClient()) == []

    def test_top_level_driver_without_routes(self):
        machine = _FakeMachine(name="mac1", repos=["vimcode"], capabilities=["macos"])
        entry = _FakeDriverCfg(kind="mac-native", capability="macos", routes=[])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        lanes = discover_lanes(cfg, "vimcode", http_client=_FakeHealthClient())
        assert [l.platform for l in lanes] == ["mac-native"]

    def test_pick_lane_machine_requires_repo_membership(self):
        machine = _FakeMachine(name="pc1", repos=["other"], capabilities=["windows"])
        assert _pick_lane_machine(
            _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig({})),
            "vimcode", "windows", http_client=_FakeHealthClient(),
        ) is None

    def test_declared_capability_contradicted_by_health_probe_is_skipped(self):
        """#3566: a `macos` machine whose Accessibility-trust grant is
        revoked must not be picked just because `coordinator.yml` still
        claims the capability — `/health`'s own probe wins."""
        machine = _FakeMachine(name="macmini", repos=["vimcode"], capabilities=["macos"])
        health = {
            "tool_versions": {
                "macos-accessibility-trust": {
                    "tool": "macos-accessibility-trust", "capability": "macos",
                    "found": False, "version": None, "min_version": None,
                    "meets_floor": None,
                    "what_breaks": "AXIsProcessTrusted() is False — grant it",
                },
            },
        }
        assert _pick_lane_machine(
            _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig({})),
            "vimcode", "macos", http_client=_FakeHealthClient(health),
        ) is None

    def test_declared_capability_contradicted_by_health_probe_falls_through_to_next_machine(self):
        """A second, genuinely-healthy machine is still picked rather than
        the whole lane being dropped — this skips ONE bad candidate, it
        doesn't refuse routing outright when another capable machine exists."""
        denied = _FakeMachine(
            name="macmini", repos=["vimcode"], capabilities=["macos"], host="macmini.local",
        )
        healthy = _FakeMachine(
            name="mac2", repos=["vimcode"], capabilities=["macos"], host="mac2.local",
        )
        health = {
            "tool_versions": {
                "macos-accessibility-trust": {
                    "tool": "macos-accessibility-trust", "capability": "macos",
                    "found": False, "version": None, "min_version": None,
                    "meets_floor": None, "what_breaks": "denied",
                },
            },
        }

        class _PerMachineClient:
            def get(self, url, *, timeout):
                if "macmini" in url:
                    return _FakeHealthResp(health)
                return _FakeHealthResp({})

        assert _pick_lane_machine(
            _FakeConfig(machines=[denied, healthy], acceptance=_FakeAcceptanceConfig({})),
            "vimcode", "macos", http_client=_PerMachineClient(),
        ) == "mac2"

    def test_health_probe_failing_open_still_picks_the_machine(self):
        """No `tool_versions` in `/health` (predates the probe, or a
        connectivity hiccup) must fail OPEN — the same contract
        `coord.smoke._capability_probe_reasons` already documents — so this
        fix doesn't regress routing for the common case of a machine that
        simply hasn't reported a probe for this capability yet."""
        machine = _FakeMachine(name="macmini", repos=["vimcode"], capabilities=["macos"])
        assert _pick_lane_machine(
            _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig({})),
            "vimcode", "macos", http_client=_FakeHealthClient({}),
        ) == "macmini"


class TestDiscoverLanesPlatforms:
    """#3581: a route's `platforms` field yields one lane per OS, each
    resolved to its own capable host, instead of one lane on whichever
    capable machine sorts first."""

    def test_two_platforms_with_capable_hosts_yields_two_lanes_on_the_right_os(self):
        precision = _FakeMachine(name="precision", repos=["vimcode"], capabilities=["rust", "linux"])
        macmini = _FakeMachine(name="macmini", repos=["vimcode"], capabilities=["rust", "macos"])
        entry = _FakeDriverCfg(
            routes=[
                _FakeDriverCfg(kind="tui-pty", capability="rust", platforms=["linux", "macos"]),
            ]
        )
        cfg = _FakeConfig(
            machines=[precision, macmini], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}),
        )
        lanes = discover_lanes(cfg, "vimcode", http_client=_FakeHealthClient())
        assert {l.platform: l.machine for l in lanes} == {
            "tui-pty:linux": "precision",
            "tui-pty:macos": "macmini",
        }
        # driver_kind stays the bare kind (journey/catalogue matching keys
        # off this, not the OS-qualified label) for every lane.
        assert {l.driver_kind for l in lanes} == {"tui-pty"}

    def test_platform_with_no_capable_host_is_reported_unavailable_not_dropped(self):
        precision = _FakeMachine(name="precision", repos=["vimcode"], capabilities=["rust", "linux"])
        entry = _FakeDriverCfg(
            routes=[
                _FakeDriverCfg(kind="tui-pty", capability="rust", platforms=["linux", "macos"]),
            ]
        )
        cfg = _FakeConfig(
            machines=[precision], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}),
        )
        unavailable: list[UnavailableLane] = []
        lanes = discover_lanes(
            cfg, "vimcode", http_client=_FakeHealthClient(), unavailable_out=unavailable,
        )
        assert [l.platform for l in lanes] == ["tui-pty:linux"]
        assert len(unavailable) == 1
        assert unavailable[0] == UnavailableLane(
            driver_kind="tui-pty", os_name="macos", capability="rust",
        )
        assert unavailable[0].platform == "tui-pty:macos"

    def test_unavailable_out_defaults_to_none_and_is_optional(self):
        """A caller that doesn't care about unavailable platforms (every
        pre-#3581 call site) passes nothing and gets exactly the resolved
        lanes, no error."""
        precision = _FakeMachine(name="precision", repos=["vimcode"], capabilities=["rust", "linux"])
        entry = _FakeDriverCfg(
            routes=[
                _FakeDriverCfg(kind="tui-pty", capability="rust", platforms=["linux", "macos"]),
            ]
        )
        cfg = _FakeConfig(
            machines=[precision], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}),
        )
        lanes = discover_lanes(cfg, "vimcode", http_client=_FakeHealthClient())
        assert [l.platform for l in lanes] == ["tui-pty:linux"]

    def test_route_without_platforms_field_is_unchanged(self):
        """A route that never declares `platforms` behaves exactly as
        before #3581 — one lane, `platform == kind`."""
        machine = _FakeMachine(name="pc1", repos=["vimcode"], capabilities=["windows"])
        entry = _FakeDriverCfg(routes=[_FakeDriverCfg(kind="win-native", capability="windows")])
        cfg = _FakeConfig(machines=[machine], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}))
        unavailable: list[UnavailableLane] = []
        lanes = discover_lanes(
            cfg, "vimcode", reference_backend="win-native", http_client=_FakeHealthClient(),
            unavailable_out=unavailable,
        )
        assert len(lanes) == 1
        assert lanes[0].platform == "win-native"
        assert lanes[0].machine == "pc1"
        assert lanes[0].reference is True
        assert unavailable == []

    def test_reference_backend_matches_the_platform_qualified_label(self):
        precision = _FakeMachine(name="precision", repos=["vimcode"], capabilities=["rust", "linux"])
        macmini = _FakeMachine(name="macmini", repos=["vimcode"], capabilities=["rust", "macos"])
        entry = _FakeDriverCfg(
            routes=[
                _FakeDriverCfg(kind="tui-pty", capability="rust", platforms=["linux", "macos"]),
            ]
        )
        cfg = _FakeConfig(
            machines=[precision, macmini], acceptance=_FakeAcceptanceConfig(drivers={"vimcode": entry}),
        )
        lanes = discover_lanes(
            cfg, "vimcode", reference_backend="tui-pty:macos", http_client=_FakeHealthClient(),
        )
        by_platform = {l.platform: l.reference for l in lanes}
        assert by_platform == {"tui-pty:linux": False, "tui-pty:macos": True}

    def test_pick_lane_machine_requires_both_capability_and_os_name(self):
        """A machine declaring the base capability but not the OS name is
        not picked for that platform."""
        rust_only = _FakeMachine(name="pc1", repos=["vimcode"], capabilities=["rust"])
        assert _pick_lane_machine(
            _FakeConfig(machines=[rust_only], acceptance=_FakeAcceptanceConfig({})),
            "vimcode", "rust", os_name="macos", http_client=_FakeHealthClient(),
        ) is None


class TestBuildExplorationBriefing:
    def test_includes_reference_backend_and_checklist(self):
        lane = BugbashLane(platform="win-native", driver_kind="win-native", machine="pc1", capability="windows")
        out = build_exploration_briefing(lane, reference_backend="mac-native", checklist=("panels", "menus"))
        assert "mac-native" in out
        assert "panels" in out
        assert "menus" in out
        assert "bugbash-findings" in out

    def test_includes_the_drive_only_through_the_driver_hard_rule(self):
        """#3566 ask #4: the briefing must tell a `bugbash-explore` worker
        to drive the app ONLY through the lane's own driver and name the
        specific unsafe workarounds the first live incident used."""
        lane = BugbashLane(platform="mac-native", driver_kind="mac-native", machine="macmini", capability="macos")
        out = build_exploration_briefing(lane, reference_backend="win-native")
        assert "HARD RULE" in out
        for forbidden in ("osascript", "System Events", "do script", "System Settings"):
            assert forbidden in out

    def test_each_lane_kind_names_its_own_runnable_command_never_anothers(self, tmp_path, monkeypatch):
        """#3590 acceptance: for each `LANE_DRIVER_KINDS` kind, the briefing
        names a concrete, runnable command (`coord app-drive open <kind>
        ...`), and no lane's briefing names a DIFFERENT lane's driver — the
        exact gap that left every real bugbash run unrunnable (workers
        searching their tool list for a bare Python module path, or being
        told `mac_native_driver` while running under `tui-pty`).

        "Runnable" is checked for real (review fix round 1): every usage
        line this briefing prints is fed through Click's own `CliRunner`
        against the actual `coord app-drive` CLI group, asserting it is
        NOT a usage error — a substring check alone previously passed for
        a command Click rejected outright (`coord app-drive tui-pty open
        ...` — kind/verb reversed), which is exactly how the unrunnable
        bug shipped undetected the first time. `$COORD_DIR` is sandboxed to
        *tmp_path* since `open`'s own real session bookkeeping (and
        whatever short-lived daemon subprocess it spawns before failing
        against the placeholder `--cwd`) must never touch the real
        `~/.coord`."""
        from click.testing import CliRunner

        from coord.bugbash import (
            LANE_DRIVER_KINDS,
            app_drive_open_usage,
            app_drive_run_spec_usage,
            driver_command_for_lane,
        )
        from coord.commands.app_drive import app_drive_group

        monkeypatch.setenv("COORD_DIR", str(tmp_path))
        # `run-spec`'s SPEC_FILE argument is a `click.Path(exists=True)` —
        # the literal `<spec-file>` placeholder text can never satisfy
        # that (a Click-level UsageError, exit 2), which would be a false
        # positive for the very defect this test exists to catch. Swap it
        # for a real (if deliberately empty/invalid) file so the ONLY
        # thing being checked for that line is argument ORDER/SHAPE —
        # whether its own contents parse as a valid spec is a separate
        # concern :mod:`coord.tui_pty_driver` etc. already test.
        real_spec_file = tmp_path / "placeholder-spec.yaml"
        real_spec_file.write_text("version: 1\nsteps: []\n")

        for kind in LANE_DRIVER_KINDS:
            lane = BugbashLane(platform=kind, driver_kind=kind, machine="m1", capability="")
            out = build_exploration_briefing(lane, reference_backend="win-native")
            command = driver_command_for_lane(lane)
            assert command == app_drive_open_usage(kind)
            assert command in out, f"{kind} briefing does not name its own command: {out!r}"

            open_line = app_drive_open_usage(kind)
            run_spec_line = app_drive_run_spec_usage(kind)
            assert open_line in out, f"{kind} briefing missing its own open usage line: {out!r}"
            assert run_spec_line in out, f"{kind} briefing missing its own run-spec usage line: {out!r}"
            for usage_line in (open_line, run_spec_line):
                argv = shlex.split(usage_line)
                assert argv[0] == "coord" and argv[1] == "app-drive"
                rest = argv[2:]
                rest = [str(real_spec_file) if tok == "<spec-file>" else tok for tok in rest]
                result = CliRunner().invoke(app_drive_group, rest)
                assert result.exit_code != 2, (
                    f"{kind} briefing's usage example is not Click-parseable "
                    f"(exit {result.exit_code}): {usage_line!r}\n{result.output}"
                )

            for other_kind in LANE_DRIVER_KINDS:
                if other_kind == kind:
                    continue
                assert app_drive_open_usage(other_kind) not in out, (
                    f"{kind} briefing wrongly names {other_kind}'s open command"
                )
                assert app_drive_run_spec_usage(other_kind) not in out, (
                    f"{kind} briefing wrongly names {other_kind}'s run-spec command"
                )
                # Also guard the OLD (buggy) module-path naming this issue
                # reports, so a regression back to it is caught even if
                # someone restores the module names alongside the command.
                other_module = {
                    "tui-pty": "coord.tui_pty_driver", "win-native": "coord.win_native_driver",
                    "mac-native": "coord.mac_native_driver", "gtk-native": "coord.gtk_native_driver",
                }[other_kind]
                assert other_module not in out, (
                    f"{kind} briefing wrongly names {other_kind}'s driver module {other_module!r}"
                )

    def test_app_drive_kinds_stay_in_sync_with_lane_driver_kinds(self):
        """#3590 review (non-blocking, made a real assertion): `coord
        .app_drive.APP_DRIVE_KINDS` is documented as kept in sync BY HAND
        with `LANE_DRIVER_KINDS` — assert it, so a kind added to one but
        not the other fails loudly instead of silently drifting (#2085
        "one question, one answer")."""
        from coord.app_drive import APP_DRIVE_KINDS
        from coord.bugbash import LANE_DRIVER_KINDS

        assert set(APP_DRIVE_KINDS) == set(LANE_DRIVER_KINDS)

    def test_includes_the_unavailable_reporting_contract(self):
        lane = BugbashLane(platform="mac-native", driver_kind="mac-native", machine="macmini", capability="macos")
        out = build_exploration_briefing(lane, reference_backend="win-native")
        assert UNAVAILABLE_FENCE in out
        assert "stop" in out.lower()

    # ── #3580: repo-supplied catalogue ────────────────────────────────

    def test_catalogue_present_lists_lane_filtered_journeys_in_priority_order(self):
        """#3580 acceptance: catalogue present -> briefing lists the
        lane-filtered journeys in priority order, with expected/reference
        text."""
        catalogue_text = """
version: 1
journeys:
  - id: p3-journey
    area: chrome
    mode: any
    lanes: [tui-pty]
    reference: spec
    reference_detail: "some spec detail"
    steps: "do the low priority thing"
    expected: "low priority expected outcome"
    priority: 3
  - id: p1-journey
    area: vim-mode
    mode: vim
    lanes: [tui-pty, win-native]
    reference: nvim
    reference_detail: "real Neovim for the same buffer"
    steps: "press dd on line 2"
    expected: "line 2 is deleted"
    priority: 1
  - id: other-lane-journey
    area: chrome
    mode: any
    lanes: [mac-native]
    reference: spec
    reference_detail: "n/a"
    steps: "n/a"
    expected: "n/a"
    priority: 1
"""
        lane = BugbashLane(platform="tui-pty", driver_kind="tui-pty", machine="pc1", capability="")
        out = build_exploration_briefing(
            lane, reference_backend="win-native", catalogue_text=catalogue_text,
        )
        # Lane-filtered: `other-lane-journey` (mac-native only) must not appear.
        assert "other-lane-journey" not in out
        # Priority order: p1-journey (priority 1) before p3-journey (priority 3).
        assert out.index("p1-journey") < out.index("p3-journey")
        assert "line 2 is deleted" in out
        assert "real Neovim for the same buffer" in out
        assert "low priority expected outcome" in out
        # #3580 requirement 3: the nvim differential-oracle instruction.
        assert "nvim --headless" in out
        assert "no nvim" in out
        # #3580 requirement 4: mode awareness is stated per journey.
        assert "Vim mode" in out
        assert COVERAGE_FENCE in out

    def test_catalogue_absent_falls_back_to_checklist_with_warning(self):
        lane = BugbashLane(platform="win-native", driver_kind="win-native", machine="pc1", capability="")
        out = build_exploration_briefing(
            lane, reference_backend="mac-native", checklist=("panels", "menus"),
            catalogue_text=None,
        )
        assert "panels" in out
        assert "menus" in out
        # No catalogue_text given at all -> no warning needed, this is just
        # the ordinary no-catalogue path.
        assert "NOTE:" not in out

    def test_catalogue_invalid_falls_back_to_checklist_with_visible_warning(self):
        """#3580 acceptance: catalogue absent or invalid -> fallback
        checklist plus a warning. Must never fail silently or crash."""
        lane = BugbashLane(platform="win-native", driver_kind="win-native", machine="pc1", capability="")
        out = build_exploration_briefing(
            lane, reference_backend="mac-native", checklist=("panels", "menus"),
            catalogue_text="not: [valid, yaml: at: all",
        )
        assert "panels" in out
        assert "menus" in out
        assert "NOTE:" in out

    def test_catalogue_valid_but_no_journey_for_this_lane_falls_back_with_warning(self):
        catalogue_text = """
version: 1
journeys:
  - id: mac-only
    lanes: [mac-native]
    reference: spec
    expected: "something"
    steps: "n/a"
    priority: 1
"""
        lane = BugbashLane(platform="win-native", driver_kind="win-native", machine="pc1", capability="")
        out = build_exploration_briefing(
            lane, reference_backend="mac-native", checklist=("panels",),
            catalogue_text=catalogue_text,
        )
        assert "panels" in out
        assert "NOTE:" in out
        assert "win-native" in out

    def test_vscode_mode_journey_says_which_mode_to_run_in(self):
        catalogue_text = """
version: 1
journeys:
  - id: vscode-ctrl-d
    lanes: [tui-pty]
    mode: vscode
    reference: vscode
    reference_detail: "VS Code default keybinding"
    steps: "press Ctrl+D twice"
    expected: "two selections"
    priority: 1
"""
        lane = BugbashLane(platform="tui-pty", driver_kind="tui-pty", machine="pc1", capability="")
        out = build_exploration_briefing(
            lane, reference_backend="win-native", catalogue_text=catalogue_text,
        )
        assert "Alt-M" in out
        assert "editor_mode" in out


class TestParseCatalogue:
    def test_valid_catalogue_parses_all_fields(self):
        catalogue_text = """
version: 1
journeys:
  - id: j1
    area: vim-mode
    mode: vim
    lanes: [tui-pty, win-native]
    reference: nvim
    reference_detail: "detail"
    steps: "steps text"
    expected: "expected text"
    priority: 2
"""
        result = parse_catalogue(catalogue_text)
        assert result.warning == ""
        assert result.source == CATALOGUE_PATH
        assert len(result.journeys) == 1
        j = result.journeys[0]
        assert j.id == "j1"
        assert j.area == "vim-mode"
        assert j.mode == "vim"
        assert j.lanes == ("tui-pty", "win-native")
        assert j.reference == "nvim"
        assert j.reference_detail == "detail"
        assert j.steps == "steps text"
        assert j.expected == "expected text"
        assert j.priority == 2

    def test_missing_text_warns_and_returns_no_journeys(self):
        result = parse_catalogue(None)
        assert result.journeys == ()
        assert result.warning != ""
        assert result.source == ""

    def test_blank_text_warns_and_returns_no_journeys(self):
        result = parse_catalogue("   \n  ")
        assert result.journeys == ()
        assert result.warning != ""

    def test_malformed_yaml_never_raises(self):
        result = parse_catalogue("not: [valid, yaml: at: all")
        assert result.journeys == ()
        assert result.warning != ""

    def test_wrong_top_level_shape_warns(self):
        result = parse_catalogue("- just\n- a\n- list\n")
        assert result.journeys == ()
        assert result.warning != ""

    def test_unsupported_version_warns(self):
        result = parse_catalogue("version: 2\njourneys: []\n")
        assert result.journeys == ()
        assert result.warning != ""

    def test_no_journeys_list_warns(self):
        result = parse_catalogue("version: 1\n")
        assert result.journeys == ()
        assert result.warning != ""

    def test_entry_missing_required_field_is_dropped_but_others_survive(self):
        catalogue_text = """
version: 1
journeys:
  - id: broken
    lanes: [tui-pty]
    reference: spec
    steps: "n/a"
    # missing `expected`
  - id: fine
    lanes: [tui-pty]
    reference: spec
    expected: "ok"
    steps: "n/a"
    priority: 1
"""
        result = parse_catalogue(catalogue_text)
        assert len(result.journeys) == 1
        assert result.journeys[0].id == "fine"
        assert result.warning != ""
        assert "broken" in result.warning

    def test_all_entries_invalid_warns_with_no_journeys(self):
        catalogue_text = """
version: 1
journeys:
  - id: broken
    lanes: [tui-pty]
"""
        result = parse_catalogue(catalogue_text)
        assert result.journeys == ()
        assert result.warning != ""

    def test_duplicate_ids_drops_the_second(self):
        catalogue_text = """
version: 1
journeys:
  - id: dup
    lanes: [tui-pty]
    reference: spec
    expected: "first"
    steps: "n/a"
    priority: 1
  - id: dup
    lanes: [tui-pty]
    reference: spec
    expected: "second"
    steps: "n/a"
    priority: 1
"""
        result = parse_catalogue(catalogue_text)
        assert len(result.journeys) == 1
        assert result.journeys[0].expected == "first"
        assert result.warning != ""


class TestJourneysForLane:
    def test_filters_and_orders_by_priority_then_id(self):
        journeys = (
            Journey(id="z", lanes=("tui-pty",), reference="spec", expected="e", priority=1),
            Journey(id="a", lanes=("tui-pty",), reference="spec", expected="e", priority=1),
            Journey(id="m", lanes=("mac-native",), reference="spec", expected="e", priority=1),
            Journey(id="b", lanes=("tui-pty",), reference="spec", expected="e", priority=2),
        )
        result = journeys_for_lane(journeys, "tui-pty")
        assert [j.id for j in result] == ["a", "z", "b"]


class TestParseCoverageBlock:
    def test_parses_valid_coverage_array(self):
        text = (
            'done.\n```bugbash-coverage\n'
            '[{"journey_id": "j1", "status": "passed"}, '
            '{"journey_id": "j2", "status": "found"}, '
            '{"journey_id": "j3", "status": "skipped", "reason": "no nvim"}]\n'
            '```'
        )
        outcomes = parse_coverage_block(text)
        assert len(outcomes) == 3
        assert outcomes[0] == JourneyOutcome(journey_id="j1", status="passed", reason="")
        assert outcomes[2] == JourneyOutcome(journey_id="j3", status="skipped", reason="no nvim")

    def test_no_fence_returns_empty_tuple(self):
        assert parse_coverage_block("done, no coverage reported") == ()

    def test_invalid_json_never_raises_and_returns_empty(self):
        text = '```bugbash-coverage\nnot valid json\n```'
        assert parse_coverage_block(text) == ()

    def test_entries_missing_required_fields_are_skipped(self):
        text = (
            '```bugbash-coverage\n'
            '[{"journey_id": "j1"}, {"status": "passed"}, '
            '{"journey_id": "j2", "status": "bogus-status"}, '
            '{"journey_id": "j3", "status": "passed"}]\n'
            '```'
        )
        outcomes = parse_coverage_block(text)
        assert len(outcomes) == 1
        assert outcomes[0].journey_id == "j3"


class TestCoverageSummary:
    def test_from_outcomes_counts_each_bucket(self):
        outcomes = (
            JourneyOutcome(journey_id="j1", status="passed"),
            JourneyOutcome(journey_id="j2", status="passed"),
            JourneyOutcome(journey_id="j3", status="found"),
            JourneyOutcome(journey_id="j4", status="skipped", reason="no nvim"),
        )
        summary = CoverageSummary.from_outcomes(outcomes)
        assert summary.attempted == 4
        assert summary.passed == 2
        assert summary.found == 1
        assert summary.skipped == 1
        assert summary.skip_reasons == ("no nvim",)


class TestApplyOutcomeToRoundCoverage:
    def test_journey_outcomes_populate_lane_coverage(self):
        report = RoundReport(round_num=1)
        lane = _lane(platform="win-native")
        outcome = ExploreOutcome(
            journey_outcomes=(
                JourneyOutcome(journey_id="j1", status="passed"),
                JourneyOutcome(journey_id="j2", status="skipped", reason="no nvim"),
            ),
        )
        _apply_outcome_to_round(report, lane, outcome)
        assert report.lane_coverage["win-native"].attempted == 2
        assert report.lane_coverage["win-native"].passed == 1
        assert report.lane_coverage["win-native"].skipped == 1

    def test_no_journey_outcomes_leaves_lane_coverage_empty(self):
        report = RoundReport(round_num=1)
        lane = _lane(platform="win-native")
        outcome = ExploreOutcome(findings=())
        _apply_outcome_to_round(report, lane, outcome)
        assert report.lane_coverage == {}


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

    def test_suspected_repo_routes_filing_to_a_different_repo(self):
        # #3546: three findings whose fault was in coord's own win-native
        # driver / WSL bridge were filed into vimcode anyway, where no
        # worker could fix them. `suspected_repo` must route BOTH
        # `coord issue create` and `coord drive-queue add` to the repo at
        # fault, not unconditionally to the app repo the lane ran against.
        finding = _finding(
            title="win-native launch() leaks the shell's PID",
            repo="vimcode", suspected_repo="claude-coordinator",
        )
        dedupe = dedupe_finding(finding, [], [])
        runner = FakeRunner(next_issue_number=3542)
        result = file_finding(finding, dedupe, _lane(), runner, dry_run=False)

        assert result.filed is True
        assert result.issue_number == 3542
        assert runner.calls[0][:3] == ["issue", "create", "claude-coordinator"]
        assert runner.calls[1] == [
            "drive-queue", "add", "claude-coordinator", "3542", "--machine", "pc1",
        ]

    def test_suspected_repo_defaults_to_the_app_repo(self):
        # No suspected_repo override (the common case) still files into the
        # app repo the lane actually ran against.
        finding = _finding(repo="vimcode", suspected_repo="vimcode")
        dedupe = dedupe_finding(finding, [], [])
        runner = FakeRunner(next_issue_number=1)
        file_finding(finding, dedupe, _lane(), runner, dry_run=False)
        assert runner.calls[0][:3] == ["issue", "create", "vimcode"]

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

    def test_journey_id_round_trips_into_filed_issue_body(self):
        """#3580 requirement 2/acceptance: a finding carrying `journey_id`
        round-trips into the filed issue body, so the fixer knows which
        catalogue journey (and therefore which reference oracle) the
        expected behaviour came from."""
        finding = _finding(journey_id="vim-dd-deletes-line")
        dedupe = dedupe_finding(finding, [], [])
        runner = FakeRunner()
        file_finding(finding, dedupe, _lane(), runner, dry_run=False)
        create_call = runner.calls[0]
        evidence_idx = create_call.index("--evidence") + 1
        assert "vim-dd-deletes-line" in create_call[evidence_idx]
        assert CATALOGUE_PATH in create_call[evidence_idx]

    def test_no_journey_id_omits_journey_line(self):
        finding = _finding()
        assert finding.journey_id == ""
        dedupe = dedupe_finding(finding, [], [])
        runner = FakeRunner()
        file_finding(finding, dedupe, _lane(), runner, dry_run=False)
        create_call = runner.calls[0]
        evidence_idx = create_call.index("--evidence") + 1
        assert "Journey:" not in create_call[evidence_idx]

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

    def test_all_lanes_failing_reports_lane_failure_not_zero_findings(self):
        # #2096 fix: every lane's ExploreOutcome comes back ok=False (e.g.
        # the whole fleet is unreachable) — this must NOT be reported as
        # "zero_findings" (a genuine clean pass), since nothing was actually
        # verified to have run.
        config = _config(max_rounds=5)

        def failing_explorer(lane, round_num):
            return ExploreOutcome(ok=False, notes="dispatch failed: connection refused")

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=failing_explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "lane_failure"
        assert report.rounds[0].lane_failures == {"win-native": "dispatch failed: connection refused"}
        assert report.rounds[0].all_explored_lanes_failed is True
        assert report.any_lane_failures is True
        assert runner.calls == []

    def test_one_of_two_lanes_failing_is_not_all_failed(self):
        # A partial failure (one lane down, the other verified) must still
        # report a real "zero_findings" clean pass if the surviving lane
        # genuinely found nothing — but the failure is still recorded for
        # visibility.
        ok_lane = _lane(platform="mac-native", machine="mac1")
        bad_lane = _lane(platform="win-native", machine="pc1")
        config = _config(lanes=[bad_lane, ok_lane], max_rounds=5)

        def explorer(lane, round_num):
            if lane.platform == "win-native":
                return ExploreOutcome(ok=False, notes="timed out after 1800s waiting on asg-1")
            return ExploreOutcome(ok=True, findings=(), cost=1.0, notes="status=completed")

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "zero_findings"
        assert report.rounds[0].all_explored_lanes_failed is False
        assert report.rounds[0].lane_failures == {"win-native": "timed out after 1800s waiting on asg-1"}
        assert report.any_lane_failures is True

    def test_skipped_lane_is_not_counted_as_a_failure(self):
        # A lane skipped for blowing its per-lane cost cap never got asked
        # anything this round — it must not show up in lane_failures, and a
        # round where every remaining (non-skipped) lane succeeded is a
        # genuine clean pass.
        expensive_lane = _lane(platform="win-native", machine="pc1")
        cheap_lane = _lane(platform="mac-native", machine="mac1")
        config = _config(lanes=[expensive_lane, cheap_lane], max_rounds=3, cost_cap_per_lane=2.0)

        def explorer(lane, round_num):
            cost = 5.0 if lane.platform == "win-native" else 0.5
            return ExploreOutcome(findings=(), cost=cost, ok=True)

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        # Round 1: both lanes explored, zero findings -> terminates cleanly.
        assert report.termination_reason == "zero_findings"
        assert report.rounds[0].lane_failures == {}
        assert report.rounds[0].skipped_lanes == []

    def test_all_lanes_unavailable_reports_lanes_unavailable_not_zero_findings(self):
        # #3510: every lane's ExploreOutcome comes back unavailable=True
        # (e.g. dell64's session is locked) — this must NOT be reported as
        # "zero_findings" (a genuine clean pass, since nothing was actually
        # exercised against the app) nor as "lane_failure" (the explorer DID
        # get a verified answer, just "the host is locked").
        config = _config(max_rounds=5)

        def unavailable_explorer(lane, round_num):
            return ExploreOutcome(unavailable=True, notes="LogonUI.exe running — desktop is locked")

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=unavailable_explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "lanes_unavailable"
        assert report.rounds[0].unavailable_lanes == {
            "win-native": "LogonUI.exe running — desktop is locked"
        }
        assert report.rounds[0].lane_failures == {}
        assert report.rounds[0].all_explored_lanes_unavailable_or_failed is True
        assert report.any_lane_unavailable is True
        assert runner.calls == []

    def test_unavailable_lane_files_no_findings_even_if_explorer_hands_some_back(self):
        # Defensive: an unavailable lane's findings must never be filed this
        # round, even if the (production) explorer defensively hands back a
        # non-empty findings tuple alongside unavailable=True.
        config = _config(max_rounds=1)
        bogus_finding = _finding(title="should never be filed")

        def unavailable_explorer(lane, round_num):
            return ExploreOutcome(
                unavailable=True, notes="no $DISPLAY", findings=(bogus_finding,),
            )

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=unavailable_explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.rounds[0].findings == []
        assert report.rounds[0].filings == []
        assert report.total_filed == 0
        assert runner.calls == []

    def test_one_unavailable_one_ok_lane_is_not_all_unavailable(self):
        # A partial unavailability (one lane locked, the other verified)
        # must still report a real "zero_findings" clean pass if the
        # surviving lane genuinely found nothing — but the unavailable lane
        # is still recorded for visibility.
        locked_lane = _lane(platform="win-native", machine="pc1")
        ok_lane = _lane(platform="mac-native", machine="mac1")
        config = _config(lanes=[locked_lane, ok_lane], max_rounds=5)

        def explorer(lane, round_num):
            if lane.platform == "win-native":
                return ExploreOutcome(unavailable=True, notes="locked")
            return ExploreOutcome(ok=True, findings=(), cost=1.0, notes="status=completed")

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "zero_findings"
        assert report.rounds[0].all_explored_lanes_unavailable_or_failed is False
        assert report.rounds[0].unavailable_lanes == {"win-native": "locked"}
        assert report.any_lane_unavailable is True

    def test_protocol_error_reports_protocol_error_not_zero_findings(self):
        # #3517: a lane that completed (ok=True) but whose report could not
        # be trusted (a malformed/missing findings block) must NOT let the
        # round read as a genuine "zero_findings" clean pass — that silent
        # collapse is exactly how a real finding got dropped and let a bug
        # pass the #3488 release gate.
        config = _config(max_rounds=5)

        def bad_protocol_explorer(lane, round_num):
            return ExploreOutcome(
                ok=True, findings=(), cost=1.84,
                protocol_error="no fence found and no explicit clean statement",
            )

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=bad_protocol_explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "protocol_error"
        assert report.rounds[0].protocol_error_lanes == {
            "win-native": "no fence found and no explicit clean statement"
        }
        assert report.rounds[0].lane_failures == {}
        assert report.any_protocol_errors is True
        assert runner.calls == []

    def test_one_protocol_error_one_ok_lane_still_blocks_zero_findings(self):
        # Unlike lane_failure/unavailable (which require ALL explored lanes
        # to agree), a protocol error on even ONE lane must block the
        # "zero_findings" read for the whole round (#3517) — a lane's bad
        # report can't be outvoted by a sibling lane's clean one.
        bad_lane = _lane(platform="win-native", machine="pc1")
        ok_lane = _lane(platform="mac-native", machine="mac1")
        config = _config(lanes=[bad_lane, ok_lane], max_rounds=5)

        def explorer(lane, round_num):
            if lane.platform == "win-native":
                return ExploreOutcome(ok=True, findings=(), cost=1.0, protocol_error="bad block")
            return ExploreOutcome(ok=True, findings=(), cost=1.0, notes="status=completed")

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "protocol_error"
        assert report.rounds[0].protocol_error_lanes == {"win-native": "bad block"}

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

    def test_cross_round_dedupe_recognizes_own_earlier_filing_even_with_stale_fetch(self):
        # #3546: the real vimcode run filed "caption buttons unclickable" as
        # vimcode#1656 in round 1, then AGAIN as #1660 in the SAME round and
        # #1669/#1675 in later rounds — dedupe never saw its own earlier
        # filings. Simulate the worst case: the open-issues fetch is
        # permanently stale (never reflects anything this run itself just
        # filed, as a lagging `gh issue list` would) — the SAME finding
        # reported again in round 2 must still be recognised as a duplicate
        # of round 1's real issue number, not filed a second time.
        config = _config(max_rounds=3)
        finding = _finding(title="Caption buttons unclickable")
        explorer = _make_explorer([[finding], [finding]])
        runner = FakeRunner(next_issue_number=1656)
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [],  # stale: never updates
            closed_issues_fetcher=lambda r: [],
        )
        assert report.total_filed == 1
        assert report.rounds[0].filings[0].filed is True
        assert report.rounds[0].filings[0].issue_number == 1656
        assert report.rounds[1].filings[0].verdict is DedupeVerdict.DUPLICATE
        assert report.rounds[1].filings[0].filed is False
        assert report.rounds[1].filings[0].issue_number == 1656
        create_calls = [c for c in runner.calls if c[:2] == ["issue", "create"]]
        assert len(create_calls) == 1

    def test_two_lanes_reporting_same_bug_one_round_files_once(self):
        # #3546: two lane instances sharing the SAME platform (the real run
        # had two "win-native" lanes from a coordinator.yml route + top-level
        # driver both naming that kind) each independently reported the
        # identical bug in round 1 — dedupe must collapse them to one filing,
        # not two, even though neither finding existed as an open issue
        # before the round started.
        lane_a = _lane(platform="win-native", machine="pc1")
        lane_b = _lane(platform="win-native", machine="pc2")
        config = _config(lanes=[lane_a, lane_b], max_rounds=1)

        def explorer(lane, round_num):
            return ExploreOutcome(
                findings=(_finding(title="Caption buttons unclickable", platform="win-native"),)
            )

        runner = FakeRunner(next_issue_number=1656)
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        create_calls = [c for c in runner.calls if c[:2] == ["issue", "create"]]
        assert len(create_calls) == 1
        filings = report.rounds[0].filings
        assert sum(1 for f in filings if f.filed) == 1
        duplicate_filing = next(f for f in filings if not f.filed)
        assert duplicate_filing.verdict is DedupeVerdict.DUPLICATE
        # The duplicate resolves to the sibling's REAL issue number — not a
        # permanent "duplicate of #None" placeholder.
        assert duplicate_filing.issue_number == 1656

    def test_all_lanes_skipped_reports_lanes_unavailable_not_zero_findings(self):
        # #3546: round 4 of the real run skipped every configured lane (both
        # had already blown their per-lane cost cap) and the CLI still
        # printed `terminated='zero_findings'` — a release gate reading that
        # would conclude "last bugbash clean" about a round that tested
        # nothing at all.
        lane = _lane(platform="win-native", machine="pc1")
        config = _config(lanes=[lane], max_rounds=3, cost_cap_per_lane=1.0)

        def explorer(lane, round_num):
            # Round 1 finds something (so the loop doesn't already stop
            # there) and spends enough to blow the per-lane cap for round 2.
            if round_num == 1:
                return ExploreOutcome(findings=(_finding(title="Bug A"),), cost=5.0)
            return ExploreOutcome(findings=(), cost=5.0)

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.rounds[0].skipped_lanes == []
        assert report.rounds[1].skipped_lanes == ["win-native"]
        assert report.rounds[1].all_lanes_skipped is True
        assert "cumulative cost" in report.rounds[1].skip_reasons["win-native"]
        assert report.termination_reason == "lanes_unavailable"
        assert report.termination_reason != "zero_findings"

    def test_some_lanes_skipped_some_explored_is_not_all_skipped(self):
        # A partial skip (one lane over its cap, the other still running)
        # must still report a genuine "zero_findings" clean pass if the
        # surviving lane found nothing — distinct from the all-skipped case
        # above.
        expensive_lane = _lane(platform="win-native", machine="pc1")
        cheap_lane = _lane(platform="mac-native", machine="mac1")
        config = _config(lanes=[expensive_lane, cheap_lane], max_rounds=3, cost_cap_per_lane=2.0)

        def explorer(lane, round_num):
            cost = 5.0 if lane.platform == "win-native" else 0.5
            return ExploreOutcome(findings=(), cost=cost, ok=True)

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.termination_reason == "zero_findings"
        assert report.rounds[0].all_lanes_skipped is False


class TestRunBugbashConcurrency:
    """#3602: a round's lanes run concurrently across HOSTS, but lanes
    sharing a host still serialize — and the total cost cap still trips
    mid-round, blocking any lane that hasn't started yet."""

    def test_lanes_on_different_hosts_overlap_same_host_lanes_never_do(self):
        # 3 hosts x 2 lanes each = 6 lanes. Run strictly sequentially (the
        # old behaviour) this would take >= 6 * SLEEP; concurrent-across-
        # hosts should take roughly 2 * SLEEP (each host's own two-lane
        # chain, overlapping with the other two hosts' chains).
        SLEEP = 0.15
        lanes = [
            _lane(platform=f"{host}-{i}", machine=host)
            for host in ("hostA", "hostB", "hostC")
            for i in range(2)
        ]
        config = _config(lanes=lanes, max_rounds=1)
        intervals: list[tuple[str, float, float]] = []
        lock = threading.Lock()

        def explorer(lane, round_num):
            start = time.monotonic()
            time.sleep(SLEEP)
            end = time.monotonic()
            with lock:
                intervals.append((lane.machine, start, end))
            return ExploreOutcome(findings=())

        runner = FakeRunner()
        wall_start = time.monotonic()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        elapsed = time.monotonic() - wall_start

        assert len(report.rounds) == 1
        assert len(intervals) == 6
        # Generous margin for scheduler noise, but well under half of the
        # fully-sequential 6 * SLEEP — proves hosts ran in parallel. Kept
        # as a loose secondary signal; the overlap assertions below are
        # the real (environment-independent) proof.
        assert elapsed < SLEEP * 5, (
            f"expected lanes on different hosts to overlap; took {elapsed:.2f}s "
            f"for 6 lanes of {SLEEP}s each"
        )

        by_host: dict[str, list[tuple[float, float]]] = {}
        for host, start, end in intervals:
            by_host.setdefault(host, []).append((start, end))

        # Two lanes on the SAME host must never overlap.
        for host, spans in by_host.items():
            spans.sort()
            assert len(spans) == 2
            (s1, e1), (s2, e2) = spans
            assert e1 <= s2, f"{host}'s two lanes overlapped: {spans}"

        # Different hosts' own two-lane windows DID overlap in wall-clock
        # time — the actual proof of concurrency, measured on the
        # recorded intervals themselves rather than on a wall-clock
        # ceiling that could flake under scheduler noise (#3602 review
        # round 1).
        host_spans = {
            host: (min(s for s, _ in spans), max(e for _, e in spans))
            for host, spans in by_host.items()
        }

        def overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
            return a[0] < b[1] and b[0] < a[1]

        hosts = list(host_spans)
        any_cross_host_overlap = any(
            overlaps(host_spans[hosts[i]], host_spans[hosts[j]])
            for i in range(len(hosts))
            for j in range(i + 1, len(hosts))
        )
        assert any_cross_host_overlap, (
            f"expected at least one pair of different hosts to overlap; "
            f"got spans {host_spans}"
        )

    def test_total_cost_cap_trips_mid_round_blocks_new_lanes(self):
        # 4 lanes on the SAME host (so exploration order is deterministic,
        # not a function of thread scheduling): cost 1.0 each, cap_total
        # 2.0 — the cap is tripped the instant lane 1's cost lands, so
        # lanes 2 and 3 (not yet started) must never be asked at all.
        # Each explored lane reports its own (non-duplicate) finding, so
        # the round's new_count is nonzero and the cost-cap check (not the
        # zero-findings one) decides the termination reason.
        lanes = [_lane(platform=f"lane{i}", machine="onehost") for i in range(4)]
        config = _config(lanes=lanes, max_rounds=1, cost_cap_total=2.0)
        calls: list[str] = []

        def explorer(lane, round_num):
            calls.append(lane.platform)
            finding = _finding(title=f"Bug {lane.platform}", platform=lane.platform)
            return ExploreOutcome(findings=(finding,), cost=1.0)

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        # Only the first two lanes (combined cost 2.0) ever ran the
        # explorer — the third and fourth never started once the shared
        # total hit the cap.
        assert calls == ["lane0", "lane1"]
        assert report.rounds[0].skipped_lanes == ["lane2", "lane3"]
        for platform in ("lane2", "lane3"):
            assert "total cost" in report.rounds[0].skip_reasons[platform]
        assert report.total_cost == 2.0
        assert report.termination_reason == "cost_cap"

    def test_cap_trip_on_one_host_blocks_unstarted_lane_on_another_host(self):
        # #3602 review round 1: `test_total_cost_cap_trips_mid_round_
        # blocks_new_lanes` above puts every lane on ONE host, which is
        # exactly the old single-threaded path — it proves the cap still
        # works sequentially, not that a lane on host B is actually
        # blocked by a cap tripped by a lane on host A. This uses a
        # `threading.Event` as a deterministic barrier so hostB's first
        # lane can only return AFTER hostA's first lane has already
        # written the tripped total under the shared lock — exercising
        # the real cross-host race this PR is built around, rather than
        # relying on scheduling luck.
        # Two barriers pin down the exact interleaving: b1 must already
        # be IN FLIGHT (past its own cap check, inside the explorer call)
        # before a1 is allowed to finish and write the tripped total —
        # otherwise b1 itself could race a1's cap check and get skipped
        # too, which would test nothing about a cross-host race.
        b1_started = threading.Event()
        a1_done = threading.Event()
        config = _config(
            lanes=[
                _lane(platform="a1", machine="hostA"),
                _lane(platform="a2", machine="hostA"),
                _lane(platform="b1", machine="hostB"),
                _lane(platform="b2", machine="hostB"),
            ],
            max_rounds=1, cost_cap_total=1.0,
        )
        calls: list[str] = []
        calls_lock = threading.Lock()

        def explorer(lane, round_num):
            with calls_lock:
                calls.append(lane.platform)
            if lane.platform == "a1":
                # Don't trip the cap until hostB's first lane has
                # already passed its own (pre-trip) cap check and is
                # genuinely in flight.
                assert b1_started.wait(timeout=5), "b1 never started"
                outcome = ExploreOutcome(
                    findings=(_finding(title="Bug A1", platform="a1"),), cost=1.0,
                )
                a1_done.set()
                return outcome
            if lane.platform == "b1":
                b1_started.set()
                # Must not return (and so must not let hostB's thread
                # move on to check b2's cap) until hostA's lane has
                # already pushed the total cap past its limit under the
                # lock.
                assert a1_done.wait(timeout=5), "a1 never signalled completion"
                return ExploreOutcome(
                    findings=(_finding(title="Bug B1", platform="b1"),), cost=0.0,
                )
            raise AssertionError(f"{lane.platform} should never have been asked")

        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert sorted(calls) == ["a1", "b1"]
        # a2 (hostA, chained after a1) and b2 (hostB, chained after b1 —
        # which only returns once a1 already tripped the cap) must both
        # have been skipped without ever being asked.
        assert sorted(report.rounds[0].skipped_lanes) == ["a2", "b2"]
        for platform in ("a2", "b2"):
            assert "total cost" in report.rounds[0].skip_reasons[platform]
        assert report.total_cost == 1.0
        assert report.termination_reason == "cost_cap"

    def test_duplicate_finding_winner_is_configured_order_not_thread_completion_order(self):
        # #3602 review round 1: two lanes on DIFFERENT hosts report the
        # identical bug. `lane_a` is listed FIRST in `config.lanes` but
        # takes noticeably longer, so `lane_b`'s thread actually finishes
        # first. Before the fix, outcomes were applied to `report` in
        # THREAD-COMPLETION order, so `lane_b` (suspected_repo="repo-b")
        # would win the within-round dedupe and get filed — meaning the
        # SAME bug could land in a different repo from run to run,
        # depending on scheduling. The fix replays outcomes in
        # `config.lanes`'s own order, so `lane_a` (suspected_repo=
        # "repo-a") must win every time, regardless of which thread
        # actually finished first.
        lane_a = _lane(platform="win-native", machine="hostA")
        lane_b = _lane(platform="win-native", machine="hostB")
        config = _config(lanes=[lane_a, lane_b], max_rounds=1)

        def explorer(lane, round_num):
            if lane.machine == "hostA":
                time.sleep(0.2)
                repo = "repo-a"
            else:
                repo = "repo-b"
            return ExploreOutcome(
                findings=(
                    _finding(title="Shared bug", platform="win-native", repo=repo),
                ),
            )

        runner = FakeRunner(next_issue_number=4200)
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        create_calls = [c for c in runner.calls if c[:2] == ["issue", "create"]]
        assert len(create_calls) == 1
        assert create_calls[0][2] == "repo-a"
        filings = report.rounds[0].filings
        assert sum(1 for f in filings if f.filed) == 1
        filed = next(f for f in filings if f.filed)
        assert filed.issue_number == 4200

    def test_max_rounds_zero_returns_empty_report_without_crashing(self):
        # #3602 review round 1: `total_cost` used to be bound only INSIDE
        # the round loop (via the now-removed `total_cost_box` cell read
        # at the end of each iteration) — with `max_rounds <= 0` the loop
        # body never ran at all, so the final `return
        # BugbashReport(..., total_cost=total_cost)` raised
        # `UnboundLocalError`. Reachable straight from the CLI: `--max-
        # rounds` has no lower bound.
        config = _config(max_rounds=0)
        runner = FakeRunner()

        def explorer(lane, round_num):
            raise AssertionError("no lane should ever be asked with max_rounds=0")

        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.rounds == []
        assert report.total_cost == 0.0
        assert report.termination_reason == "round_cap"


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


# ── production explorer (coord/commands/bugbash.py) ─────────────────────
#
# _dispatch_and_await_lane is the PRODUCTION Explorer seam: dispatch a
# headless worker, wait for it via the shared `poll_until_terminal` poller
# (#2743), then fetch+parse its log. Every branch below faked at the seam
# boundary (`dispatch_with_retry`, `poll_until_terminal`, `httpx.get` for
# the log fetch) — no real network/fleet access.


@dataclass
class _FakeRealMachine:
    name: str
    host: str
    capabilities: list = field(default_factory=list)
    repos: list = field(default_factory=list)


@dataclass
class _FakeConcurrency:
    max_retries: int = 3
    backoff_base: float = 1.0


@dataclass
class _FakeModels:
    default: str = "sonnet"


@dataclass
class _FakeRealConfig:
    machines: list
    concurrency: _FakeConcurrency = field(default_factory=_FakeConcurrency)
    models: _FakeModels = field(default_factory=_FakeModels)


def _prod_lane(machine="pc1", platform="win-native"):
    from coord.bugbash import BugbashLane
    return BugbashLane(platform=platform, driver_kind=platform, machine=machine, capability="windows")


class _FakePollOutcome:
    def __init__(self, status, exit_code=None, error=None):
        self.status = status
        self.exit_code = exit_code
        self.error = error


class TestDispatchAndAwaitLane:
    def test_unknown_machine_is_ok_false(self):
        from coord.commands.bugbash import _dispatch_and_await_lane

        cfg = _FakeRealConfig(machines=[])
        outcome = _dispatch_and_await_lane(
            _prod_lane(machine="ghost"), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.ok is False
        assert "ghost" in outcome.notes
        assert outcome.findings == ()

    def test_dispatch_failure_is_ok_false(self, monkeypatch):
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])

        def boom(*a, **k):
            raise RuntimeError("no agent reachable")

        monkeypatch.setattr("coord.dispatch.dispatch_with_retry", boom)
        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.ok is False
        assert "dispatch failed" in outcome.notes

    def _dispatch_ok(self, monkeypatch):
        monkeypatch.setattr(
            "coord.dispatch.dispatch_with_retry", lambda *a, **k: {"id": "asg-1"},
        )

    def test_poll_not_found_is_ok_false(self, monkeypatch):
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("not_found"),
        )
        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.ok is False
        assert "not found" in outcome.notes

    class _FakeCancelResult:
        def __init__(self, ok, status=None, error=None):
            self.ok = ok
            self.status = status
            self.error = error

    def test_poll_timeout_with_no_new_output_cancels_the_explorer(self, monkeypatch):
        """#3569: a genuinely stalled lane (no new transcript output across
        a full --lane-timeout window) must never be left running
        unattended — the controller tries to cancel it, and says so in
        `notes` (#2096: this is a post-cancel observation, not just proof
        the request was sent)."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("timeout"),
        )
        # Same transcript length on every peek -> never "progressed".
        monkeypatch.setattr(cmd_bugbash, "_peek_log_text", lambda machine, aid: "same log text")
        monkeypatch.setattr(
            "coord.network.cancel_assignment",
            lambda *a, **k: self._FakeCancelResult(ok=True, status="cancelled"),
        )

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
            timeout=1.0,
        )
        assert outcome.ok is False
        assert "timed out" in outcome.notes
        assert "no new output" in outcome.notes
        assert "cancelled the explorer" in outcome.notes

    def test_poll_timeout_cancel_failure_reports_may_still_be_running(self, monkeypatch):
        """#3569 ask #4: when the cancel attempt itself fails (agent
        unreachable, etc.), the explorer is NOT confirmed stopped — the
        notes must say it may still be running and point at the recovery
        command, never silently read as "handled"."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("timeout"),
        )
        monkeypatch.setattr(cmd_bugbash, "_peek_log_text", lambda machine, aid: "same log text")
        monkeypatch.setattr(
            "coord.network.cancel_assignment",
            lambda *a, **k: self._FakeCancelResult(ok=False, status=None, error="connection refused"),
        )

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
            timeout=1.0,
        )
        assert outcome.ok is False
        assert "may still be running" in outcome.notes
        assert "coord bugbash harvest" in outcome.notes

    def test_poll_timeout_cancel_races_a_just_finished_explorer_and_harvests_inline(self, monkeypatch):
        """#3569's concrete instance: the explorer actually finished
        between the controller's last poll and its cancel call. Rather
        than discarding that result (the original bug — $6 of valid
        findings lost), the cancel's own real post-cancel status is used to
        harvest it right there.

        `cancel_assignment()`'s `status` mirrors `AgentAssignment.status`
        straight from the agent's idempotent `/cancel` response
        (`AgentServer.cancel`: already-terminal assignments are returned
        unchanged) — one of `done`/`failed`/`advisory`/`refused_policy`/
        `refused_premise` here, never the unrelated `PollOutcome` value
        `"completed"` (that vocabulary belongs to `poll_until_terminal`,
        not to `/cancel`)."""
        from coord.agent import DONE
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("timeout"),
        )
        monkeypatch.setattr(cmd_bugbash, "_peek_log_text", lambda machine, aid: "same log text")
        monkeypatch.setattr(
            "coord.network.cancel_assignment",
            lambda *a, **k: self._FakeCancelResult(ok=False, status=DONE),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "done.\\n```bugbash-findings\\n'
            '[{\\"title\\": \\"Crash on install\\", \\"expected\\": \\"e\\", '
            '\\"actual\\": \\"a\\", \\"repro\\": \\"r\\", \\"evidence\\": \\"ev\\"}]\\n'
            '```"}]}}\n'
            '{"type": "result", "total_cost_usd": 6.08, "num_turns": 168}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
            timeout=1.0,
        )
        assert outcome.ok is True
        assert len(outcome.findings) == 1
        assert outcome.findings[0].title == "Crash on install"
        assert outcome.cost == 6.08
        assert "harvested inline" in outcome.notes

    def test_poll_timeout_keeps_waiting_while_explorer_still_progresses_under_cost_cap(self, monkeypatch):
        """#3569's "better" option: a lane still producing new transcript
        output and still under its own --cost-cap-per-lane is NOT a stall —
        the controller keeps polling past `--lane-timeout` rather than
        cancelling a still-productive explorer, and files its findings once
        it genuinely finishes."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)

        poll_calls = {"n": 0}

        def fake_poll(*a, **k):
            poll_calls["n"] += 1
            if poll_calls["n"] < 3:
                return _FakePollOutcome("timeout")
            return _FakePollOutcome("completed", exit_code=0)

        monkeypatch.setattr("coord.commands._common.poll_until_terminal", fake_poll)

        peek_calls = {"n": 0}

        def fake_peek(machine, aid):
            peek_calls["n"] += 1
            return "x" * peek_calls["n"]  # strictly growing -> always "progressed"

        monkeypatch.setattr(cmd_bugbash, "_peek_log_text", fake_peek)

        cancel_called = {"called": False}
        monkeypatch.setattr(
            "coord.network.cancel_assignment",
            lambda *a, **k: cancel_called.update(called=True) or self._FakeCancelResult(ok=True, status="cancelled"),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "done.\\n```bugbash-findings\\n[]\\n```"}]}}\n'
            '{"type": "result", "total_cost_usd": 2.5}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
            timeout=1.0, cost_cap=100.0,
        )
        assert outcome.ok is True
        assert outcome.findings == ()
        assert outcome.cost == 2.5
        # Never cancelled -- it finished on its own while still progressing.
        assert cancel_called["called"] is False

    def test_poll_timeout_cost_cap_exceeded_while_progressing_still_cancels(self, monkeypatch):
        """Even a lane that's still producing new output must be cancelled
        once it crosses its own --cost-cap-per-lane — the cost cap is the
        real budget control, and "still talking" is not a license to spend
        past it."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("timeout"),
        )

        peek_calls = {"n": 0}

        def fake_peek(machine, aid):
            peek_calls["n"] += 1
            # Growing transcript (always "progressed") but a cost readout
            # already past the cap on every check.
            return (
                '{"type": "result", "total_cost_usd": 999.0}\n' + ("x" * peek_calls["n"])
            )

        monkeypatch.setattr(cmd_bugbash, "_peek_log_text", fake_peek)
        monkeypatch.setattr(
            "coord.network.cancel_assignment",
            lambda *a, **k: self._FakeCancelResult(ok=True, status="cancelled"),
        )

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
            timeout=1.0, cost_cap=20.0,
        )
        assert outcome.ok is False
        assert "per-lane cap" in outcome.notes
        assert "cancelled the explorer" in outcome.notes

    def test_poll_timeout_transient_peek_failure_is_not_treated_as_a_stall(self, monkeypatch):
        """#3569 fix-round-1: `_peek_log_text` returning `None` (a transient
        HTTP failure talking to the agent — a Tailscale blip, not a real
        stall) must read as "couldn't tell if it progressed", never as "it
        definitely didn't" — so it must NOT cancel a perfectly healthy,
        still-productive explorer. The window that saw the `None` is
        retried rather than counted toward the stall decision; once a real
        peek succeeds and shows growth, the explorer is left running."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)

        poll_calls = {"n": 0}

        def fake_poll(*a, **k):
            poll_calls["n"] += 1
            if poll_calls["n"] < 3:
                return _FakePollOutcome("timeout")
            return _FakePollOutcome("completed", exit_code=0)

        monkeypatch.setattr("coord.commands._common.poll_until_terminal", fake_poll)

        peek_calls = {"n": 0}

        def fake_peek(machine, aid):
            peek_calls["n"] += 1
            if peek_calls["n"] == 2:
                # One transient fetch failure mid-stall-loop.
                return None
            return "x" * peek_calls["n"]  # otherwise strictly growing

        monkeypatch.setattr(cmd_bugbash, "_peek_log_text", fake_peek)

        cancel_called = {"called": False}
        monkeypatch.setattr(
            "coord.network.cancel_assignment",
            lambda *a, **k: cancel_called.update(called=True) or self._FakeCancelResult(ok=True, status="cancelled"),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "done.\\n```bugbash-findings\\n[]\\n```"}]}}\n'
            '{"type": "result", "total_cost_usd": 2.5}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
            timeout=1.0, cost_cap=100.0,
        )
        assert outcome.ok is True
        assert outcome.cost == 2.5
        # The transient None peek must never trigger a cancel.
        assert cancel_called["called"] is False

    def test_nonzero_exit_code_is_ok_false(self, monkeypatch):
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=1, error="crashed"),
        )
        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.ok is False
        assert "FAILED (exit 1)" in outcome.notes
        assert "crashed" in outcome.notes

    def test_log_fetch_failure_is_ok_false(self, monkeypatch):
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        import httpx as httpx_mod

        def boom_get(*a, **k):
            raise httpx_mod.ConnectError("refused")

        monkeypatch.setattr(httpx_mod, "get", boom_get)
        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.ok is False
        assert "log fetch failed" in outcome.notes

    def test_successful_round_trip_parses_findings_and_is_ok_true(self, monkeypatch):
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "done.\\n```bugbash-findings\\n'
            '[{\\"title\\": \\"Crash on install\\", \\"expected\\": \\"e\\", '
            '\\"actual\\": \\"a\\", \\"repro\\": \\"r\\", \\"evidence\\": \\"ev\\"}]\\n'
            '```"}]}}\n'
            '{"type": "result", "total_cost_usd": 1.84, "num_turns": 75}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.ok is True
        # #3517: cost must be the worker's REAL `total_cost_usd` from its
        # log, not a flat per-round placeholder — the bug report's exact
        # mismatch was a $1.84 lane recorded as `total_cost=1.00`.
        assert outcome.cost == 1.84
        assert len(outcome.findings) == 1
        assert outcome.findings[0].title == "Crash on install"
        assert outcome.protocol_error == ""

    def test_coverage_block_is_parsed_into_journey_outcomes(self, monkeypatch):
        """#3580 requirement 5: the production explorer parses the lane
        worker's ```` ```bugbash-coverage ```` block into
        `ExploreOutcome.journey_outcomes`, independent of the findings
        fence/protocol-error decision."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "done.\\n```bugbash-findings\\n[]\\n```\\n'
            '```bugbash-coverage\\n'
            '[{\\"journey_id\\": \\"j1\\", \\"status\\": \\"passed\\"}, '
            '{\\"journey_id\\": \\"j2\\", \\"status\\": \\"skipped\\", '
            '\\"reason\\": \\"no nvim\\"}]\\n'
            '```"}]}}\n'
            '{"type": "result", "total_cost_usd": 2.0}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.ok is True
        assert outcome.findings == ()
        assert len(outcome.journey_outcomes) == 2
        assert outcome.journey_outcomes[0].journey_id == "j1"
        assert outcome.journey_outcomes[0].status == "passed"
        assert outcome.journey_outcomes[1].status == "skipped"
        assert outcome.journey_outcomes[1].reason == "no nvim"

    def test_cost_with_no_result_event_defaults_to_zero_not_a_flat_placeholder(self, monkeypatch):
        """A log with no terminal `result` event (e.g. truncated) must never
        fall back to a made-up flat cost — `0.0` is the honest "we don't
        know" default, same discipline `WorkerSummary` already applies
        everywhere else cost is read (#3517)."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "zero findings this round."}]}}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.cost == 0.0

    def test_no_fence_and_no_clean_statement_is_a_protocol_error_not_zero_findings(self, monkeypatch):
        """#3517's actual regression scenario end to end: a lane worker's
        final message carries no ```` ```bugbash-findings ```` fence and no
        explicit clean statement — `_dispatch_and_await_lane` must hand back
        a non-empty `protocol_error`, never `findings=(), ok=True` (which
        `run_bugbash` would read as a genuine clean pass)."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "I ran out of budget partway through."}]}}\n'
            '{"type": "result", "total_cost_usd": 1.0}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(), 1, repo_name="vimcode", config=cfg, reference_backend="mac-native",
        )
        assert outcome.ok is True
        assert outcome.findings == ()
        assert outcome.protocol_error != ""
        assert outcome.cost == 1.0

    def test_worker_unavailable_report_sets_unavailable_not_zero_findings(self, monkeypatch):
        """#3566 acceptance: an explorer reporting a permission failure
        yields `ExploreOutcome.unavailable=True`, and — end to end through
        `run_bugbash` — `unavailable_lanes=['mac-native']`, never
        `zero_findings`."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "Accessibility is not granted for this identity — '
            "stopping per the hard rule rather than improvising a "
            'workaround.\\n```bugbash-unavailable\\n'
            'AXIsProcessTrusted() is False\\n```"}]}}\n'
            '{"type": "result", "total_cost_usd": 0.42}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(machine="pc1", platform="mac-native"), 1,
            repo_name="vimcode", config=cfg, reference_backend="win-native",
        )
        assert outcome.unavailable is True
        assert outcome.ok is True  # default — unavailable is the real signal
        assert "AXIsProcessTrusted" in outcome.notes
        assert outcome.findings == ()
        assert outcome.cost == 0.42

    def test_unavailable_propagates_through_run_bugbash_as_unavailable_lane(self, monkeypatch):
        """End-to-end through the engine: a lane whose explorer reports
        unavailable must show up in `RoundReport.unavailable_lanes`, and
        the round must terminate `lanes_unavailable`, not `zero_findings`
        (#3510's own engine-level contract, now reachable from the
        production explorer)."""
        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "stopping.\\n```bugbash-unavailable\\n'
            'the screen is locked\\n```"}]}}\n'
            '{"type": "result", "total_cost_usd": 0.1}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        lane = _prod_lane(machine="pc1", platform="mac-native")

        def explorer(explore_lane, round_num):
            return cmd_bugbash._dispatch_and_await_lane(
                explore_lane, round_num, repo_name="vimcode", config=cfg,
                reference_backend="win-native",
            )

        config = BugbashConfig(
            repo="vimcode", lanes=[lane], reference_backend="win-native",
            max_rounds=1, confirm_rounds=0,
        )
        runner = FakeRunner()
        report = run_bugbash(
            config, explorer=explorer, runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.rounds[-1].unavailable_lanes.get("mac-native") is not None
        assert report.termination_reason == "lanes_unavailable"

    #: #3590's two real evidence transcripts' exact final-message text,
    #: verbatim from the issue — each ends with a well-formed
    #: ` ```bugbash-unavailable ``` ` block whose reason does NOT happen to
    #: match any of `_UNAVAILABLE_SIGNATURES`'s fallback substrings (unlike
    #: this test module's other fixtures above, which all incidentally use
    #: a signature phrase like "AXIsProcessTrusted" or "screen is locked" —
    #: so none of them actually exercised the fence-matching path against a
    #: REAL transcript).
    _EVIDENCE_FINAL_MESSAGES = {
        "tui-pty:macos (0138d740a9f5)": (
            "The only deferred tools available in this session are Monitor, "
            "TaskStop, WebFetch, and WebSearch — none of which is the "
            "mac native driver this lane requires.\n\n"
            "```bugbash-unavailable\n"
            "coord.mac_native_driver tool is not present/loaded in this "
            "session\n"
            "```\n"
        ),
        "mac-native (080701430ba6)": (
            "there is no coord.mac_native_driver (or any native-driver "
            "equivalent) exposed as a tool here.\n\n"
            "```bugbash-unavailable\n"
            "coord.mac_native_driver tool is not available in this session\n"
            "```\n"
        ),
    }

    @pytest.mark.parametrize(
        "final_message", _EVIDENCE_FINAL_MESSAGES.values(), ids=_EVIDENCE_FINAL_MESSAGES.keys(),
    )
    def test_well_formed_unavailable_fence_in_a_real_ndjson_log_is_not_a_protocol_error(
        self, monkeypatch, final_message,
    ):
        """#3590 regression: both of the issue's real evidence transcripts
        (:data:`_EVIDENCE_FINAL_MESSAGES`) must parse as `unavailable=True`,
        never `protocol_error`. A real `/logs/{id}` response is NDJSON: a
        message's own newlines/quotes are JSON-escaped, not literal bytes —
        built here with `json.dumps` (not a hand-typed `\\n`) so this test
        fails the same way the real run did if the fence regex is ever
        matched against the raw, still-escaped log text again instead of
        the decoded message text."""
        import json

        from coord.commands import bugbash as cmd_bugbash

        cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        self._dispatch_ok(monkeypatch)
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        log_line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": final_message}]},
        }) + "\n" + json.dumps({"type": "result", "total_cost_usd": 0.07})

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        outcome = cmd_bugbash._dispatch_and_await_lane(
            _prod_lane(machine="pc1", platform="mac-native"), 1,
            repo_name="vimcode", config=cfg, reference_backend="win-native",
        )
        assert outcome.unavailable is True
        assert outcome.protocol_error == ""
        assert "mac_native_driver" in outcome.notes
        assert outcome.findings == ()


# ── coord bugbash harvest (#3569: recover a late-finishing explorer) ─────
#
# `harvest_outcome` is the engine-level recovery path `coord bugbash
# harvest` drives after `_dispatch_and_await_lane`'s controller stopped
# waiting on a lane (its cancel attempt failed, or the explorer kept
# running past --lane-timeout on purpose). It must reuse the EXACT same
# bucketing/dedupe/file logic `run_bugbash`'s own round loop uses — these
# tests exercise it directly against the same fakes the rest of this file
# already uses (FakeRunner, _finding, _lane).


class TestHarvestOutcome:
    def test_new_finding_is_filed_and_queued(self):
        outcome = ExploreOutcome(findings=(_finding(title="Crash on install"),), cost=6.08)
        runner = FakeRunner(next_issue_number=500)
        report = harvest_outcome(
            outcome, _lane(platform="win-native", machine="pc1"), repo="vimcode",
            runner=runner, open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.new_count == 1
        assert len(report.filings) == 1
        assert report.filings[0].filed is True
        assert report.filings[0].issue_number == 500
        assert report.lane_cost.get("win-native") == 6.08
        # `coord issue create` AND `coord drive-queue add` both actually ran.
        assert runner.calls[0][:2] == ["issue", "create"]
        assert runner.calls[1][:2] == ["drive-queue", "add"]

    def test_dry_run_files_nothing(self):
        outcome = ExploreOutcome(findings=(_finding(),), cost=1.0)
        runner = FakeRunner()
        report = harvest_outcome(
            outcome, _lane(), repo="vimcode", runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
            dry_run=True,
        )
        assert report.filings[0].filed is False
        assert report.filings[0].preview_title is not None
        assert runner.calls == []

    def test_journey_outcomes_populate_coverage_summary(self):
        """#3580 requirement 5: harvesting a late-arriving explorer still
        reports its per-journey coverage, same as an inline round."""
        outcome = ExploreOutcome(
            findings=(),
            cost=0.5,
            journey_outcomes=(
                JourneyOutcome(journey_id="j1", status="passed"),
                JourneyOutcome(journey_id="j2", status="skipped", reason="no nvim"),
            ),
        )
        runner = FakeRunner()
        report = harvest_outcome(
            outcome, _lane(platform="win-native"), repo="vimcode", runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        cov = report.lane_coverage["win-native"]
        assert cov.attempted == 2
        assert cov.passed == 1
        assert cov.skipped == 1
        assert cov.skip_reasons == ("no nvim",)

    def test_duplicate_against_open_issue_is_not_refiled(self):
        finding = _finding(title="Extension install flow crashes on Windows")
        outcome = ExploreOutcome(findings=(finding,), cost=0.5)
        runner = FakeRunner()
        open_issues = [
            {"number": 42, "title": "[bugbash:win-native] Extension install flow crashes on Windows"},
        ]
        report = harvest_outcome(
            outcome, _lane(), repo="vimcode", runner=runner,
            open_issues_fetcher=lambda r: open_issues, closed_issues_fetcher=lambda r: [],
        )
        assert report.filings[0].filed is False
        assert report.filings[0].issue_number == 42
        assert runner.calls == []

    def test_unavailable_outcome_is_bucketed_not_filed(self):
        outcome = ExploreOutcome(unavailable=True, notes="the screen is locked", cost=0.1)
        runner = FakeRunner()
        report = harvest_outcome(
            outcome, _lane(platform="mac-native"), repo="vimcode", runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.unavailable_lanes.get("mac-native") == "the screen is locked"
        assert report.findings == []
        assert report.filings == []
        assert runner.calls == []

    def test_protocol_error_outcome_is_bucketed_not_filed(self):
        outcome = ExploreOutcome(ok=True, protocol_error="no fence found", cost=1.0)
        runner = FakeRunner()
        report = harvest_outcome(
            outcome, _lane(), repo="vimcode", runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.protocol_error_lanes.get("win-native") == "no fence found"
        assert report.findings == []
        assert runner.calls == []

    def test_lane_failure_outcome_ok_false_is_bucketed(self):
        outcome = ExploreOutcome(ok=False, notes="still running on pc1")
        runner = FakeRunner()
        report = harvest_outcome(
            outcome, _lane(), repo="vimcode", runner=runner,
            open_issues_fetcher=lambda r: [], closed_issues_fetcher=lambda r: [],
        )
        assert report.lane_failures.get("win-native") == "still running on pc1"
        assert report.findings == []
        assert runner.calls == []


class TestPrintRoundCoverage:
    """#3580 requirement 5: a clean round's CLI output must read as "N
    journeys passed", not just a quiet "0 finding(s)" line."""

    def test_coverage_summary_is_printed_per_lane(self, capsys):
        from coord.commands.bugbash import _print_round
        from coord.bugbash import BugbashReport

        report_round = RoundReport(round_num=1)
        report_round.lane_coverage["win-native"] = CoverageSummary(
            attempted=5, passed=3, found=1, skipped=1, skip_reasons=("no nvim",),
        )
        wrapped = BugbashReport(repo="vimcode", rounds=[report_round], termination_reason="zero_findings")
        _print_round(wrapped)
        out = capsys.readouterr().out
        assert "coverage (win-native)" in out
        assert "5 attempted" in out
        assert "3 passed" in out
        assert "1 found" in out
        assert "1 skipped" in out
        assert "no nvim" in out

    def test_unavailable_lane_is_printed_with_its_reason(self, capsys):
        """#3611: a round where a lane never ran at all (its driver's own
        session-availability precheck refused, #3510) must say so by name
        — previously `RoundReport.unavailable_lanes` fed the round's
        termination reason but was never actually rendered here, so e.g.
        every win-native/mac-native lane coming back unavailable read as a
        silent, unexplained empty round."""
        from coord.commands.bugbash import _print_round
        from coord.bugbash import BugbashReport

        report_round = RoundReport(round_num=1)
        report_round.unavailable_lanes["win-native"] = (
            "win-native driver requires a real Windows host"
        )
        report_round.unavailable_lanes["mac-native"] = "the screen is locked"
        wrapped = BugbashReport(
            repo="vimcode", rounds=[report_round], termination_reason="lanes_unavailable",
        )
        _print_round(wrapped)
        out = capsys.readouterr().out
        assert "lane UNAVAILABLE (win-native): win-native driver requires a real Windows host" in out
        assert "lane UNAVAILABLE (mac-native): the screen is locked" in out


# ── coord bugbash CLI (#3569: --lane-timeout, run/harvest subcommands) ───


class TestBugbashCli:
    """CLI-level coverage for #3569: the `--lane-timeout` flag threading
    through to the production explorer seam, and the `run`/`harvest`
    subcommand group wiring (`coord bugbash REPO ...` must keep working
    exactly as before even though `harvest` is now a real second
    subcommand)."""

    def test_bare_repo_invocation_still_resolves_to_the_run_subcommand(self):
        from click.testing import CliRunner
        import coord.commands.bugbash as cmd_bugbash

        result = CliRunner().invoke(cmd_bugbash.bugbash_cmd, ["vimcode", "--help"])
        assert result.exit_code == 0
        assert "Usage: bugbash run" in result.output
        assert "--lane-timeout" in result.output

    def test_harvest_is_a_real_subcommand(self):
        from click.testing import CliRunner
        import coord.commands.bugbash as cmd_bugbash

        result = CliRunner().invoke(cmd_bugbash.bugbash_cmd, ["harvest", "--help"])
        assert result.exit_code == 0
        assert "Usage: bugbash harvest" in result.output

    def test_fetch_catalogue_text_forwards_repo_default_branch(self, monkeypatch):
        """#3580 review: `_fetch_catalogue_text` must thread the repo's own
        configured default branch through to `github_ops.get_repo_file`
        (like every other call site does), not rely on that function's
        `branch="develop"` default — otherwise any repo whose default
        branch isn't literally "develop" (e.g. "main") 404s and silently
        falls back to the checklist even with a real catalogue present."""
        import coord.commands.bugbash as cmd_bugbash

        captured = {}

        def fake_get_repo_file(slug, path, branch="develop"):
            captured["slug"] = slug
            captured["path"] = path
            captured["branch"] = branch
            return "version: 1\njourneys: []\n"

        monkeypatch.setattr(cmd_bugbash.github_ops, "get_repo_file", fake_get_repo_file)

        result = cmd_bugbash._fetch_catalogue_text("acme/quadraui", "main")

        assert result == "version: 1\njourneys: []\n"
        assert captured["slug"] == "acme/quadraui"
        assert captured["path"] == cmd_bugbash.CATALOGUE_PATH
        assert captured["branch"] == "main"

    def test_fetch_catalogue_text_missing_on_default_branch_returns_none(self, monkeypatch):
        """A genuine 404 (no catalogue on the repo's real default branch)
        must still degrade to `None` (the fallback path), not raise."""
        import coord.commands.bugbash as cmd_bugbash

        def fake_get_repo_file(slug, path, branch="develop"):
            raise RuntimeError(f"not found on {branch}")

        monkeypatch.setattr(cmd_bugbash.github_ops, "get_repo_file", fake_get_repo_file)

        assert cmd_bugbash._fetch_catalogue_text("acme/quadraui", "main") is None

    def test_cli_forwards_repo_default_branch_to_fetch_catalogue_text(self, monkeypatch):
        """#3580 review: the `bugbash run` CLI must pass `repo_cfg
        .default_branch` — not a hardcoded/omitted value — to
        `_fetch_catalogue_text`, so a repo configured with `default_branch:
        main` (the common case; see coordinator.example.yml) is actually
        looked up on `main`, not `develop`."""
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner
        from coord.bugbash import BugbashReport

        class _FakeRepoCfg:
            github = "acme/quadraui"
            default_branch = "main"

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: _FakeRepoCfg()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        monkeypatch.setattr(
            cmd_bugbash, "discover_lanes",
            lambda cfg, repo, reference_backend="": [_prod_lane(machine="pc1", platform="win-native")],
        )
        captured = {}

        def fake_fetch(slug, branch):
            captured["slug"] = slug
            captured["branch"] = branch
            return None

        monkeypatch.setattr(cmd_bugbash, "_fetch_catalogue_text", fake_fetch)
        monkeypatch.setattr(
            cmd_bugbash, "run_bugbash",
            lambda bb_config, **kw: BugbashReport(
                repo=bb_config.repo, rounds=[], termination_reason="round_cap", total_cost=0.0,
            ),
        )

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            ["quadraui", "--reference", "win-native", "--dry-run", "-y"],
        )
        assert result.exit_code == 0, result.output
        assert captured["slug"] == "acme/quadraui"
        assert captured["branch"] == "main"

    def test_dry_run_names_catalogue_in_use_and_journey_count_per_lane(self, monkeypatch):
        """#3580 acceptance: `coord bugbash <repo> --dry-run` output names
        the catalogue in use (or the fallback) and the journey count per
        lane."""
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner
        from coord.bugbash import BugbashReport

        class _FakeRepoCfg:
            github = "acme/vimcode"
            default_branch = "develop"

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: _FakeRepoCfg()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        monkeypatch.setattr(
            cmd_bugbash, "discover_lanes",
            lambda cfg, repo, reference_backend="": [
                _prod_lane(machine="pc1", platform="win-native"),
                _prod_lane(machine="pc1", platform="tui-pty"),
            ],
        )
        catalogue_text = (
            "version: 1\n"
            "journeys:\n"
            "  - id: j1\n"
            "    lanes: [win-native]\n"
            "    reference: spec\n"
            "    expected: \"e\"\n"
            "    steps: \"s\"\n"
            "    priority: 1\n"
            "  - id: j2\n"
            "    lanes: [tui-pty]\n"
            "    reference: spec\n"
            "    expected: \"e\"\n"
            "    steps: \"s\"\n"
            "    priority: 2\n"
        )
        monkeypatch.setattr(cmd_bugbash, "_fetch_catalogue_text", lambda slug, branch: catalogue_text)
        monkeypatch.setattr(
            cmd_bugbash, "run_bugbash",
            lambda bb_config, **kw: BugbashReport(
                repo=bb_config.repo, rounds=[], termination_reason="round_cap", total_cost=0.0,
            ),
        )

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            ["vimcode", "--reference", "win-native", "--dry-run", "-y"],
        )
        assert result.exit_code == 0, result.output
        assert "catalogue:" in result.output
        assert CATALOGUE_PATH in result.output
        assert "win-native=1" in result.output
        assert "tui-pty=1" in result.output

    def test_dry_run_prints_the_per_lane_driver_command(self, monkeypatch):
        """#3590 acceptance: `coord bugbash REPO --dry-run` prints the exact
        `coord app-drive <kind>` command each lane's worker will be handed
        — the SAME `driver_command_for_lane` the briefing's own HARD RULE
        calls (#2096 "one question, one answer"), so this can never drift
        from what a worker is actually told to run."""
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner
        from coord.bugbash import BugbashReport, driver_command_for_lane

        class _FakeRepoCfg:
            github = "acme/vimcode"
            default_branch = "develop"

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: _FakeRepoCfg()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        lanes = [
            _prod_lane(machine="pc1", platform="win-native"),
            _prod_lane(machine="pc1", platform="tui-pty"),
        ]
        monkeypatch.setattr(
            cmd_bugbash, "discover_lanes", lambda cfg, repo, reference_backend="": lanes,
        )
        monkeypatch.setattr(cmd_bugbash, "_fetch_catalogue_text", lambda slug, branch: None)
        monkeypatch.setattr(
            cmd_bugbash, "run_bugbash",
            lambda bb_config, **kw: BugbashReport(
                repo=bb_config.repo, rounds=[], termination_reason="round_cap", total_cost=0.0,
            ),
        )

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            ["vimcode", "--reference", "win-native", "--dry-run", "-y"],
        )
        assert result.exit_code == 0, result.output
        for lane in lanes:
            assert driver_command_for_lane(lane) in result.output

    def test_dry_run_names_the_fallback_when_no_catalogue(self, monkeypatch):
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner
        from coord.bugbash import BugbashReport

        class _FakeRepoCfg:
            github = "acme/vimcode"
            default_branch = "develop"

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: _FakeRepoCfg()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        monkeypatch.setattr(
            cmd_bugbash, "discover_lanes",
            lambda cfg, repo, reference_backend="": [_prod_lane(machine="pc1", platform="win-native")],
        )
        monkeypatch.setattr(cmd_bugbash, "_fetch_catalogue_text", lambda slug, branch: None)
        monkeypatch.setattr(
            cmd_bugbash, "run_bugbash",
            lambda bb_config, **kw: BugbashReport(
                repo=bb_config.repo, rounds=[], termination_reason="round_cap", total_cost=0.0,
            ),
        )

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            ["vimcode", "--reference", "win-native", "--dry-run", "-y"],
        )
        assert result.exit_code == 0, result.output
        assert "catalogue:" in result.output
        assert "falling back to the built-in exploration checklist" in result.output

    def test_lane_timeout_and_cost_cap_reach_dispatch_and_await_lane(self, monkeypatch):
        """#3569 acceptance: `--lane-timeout` (plus `--cost-cap-per-lane`,
        the real budget control the stall loop is bounded by) must reach
        `_dispatch_and_await_lane`, not just be parsed and dropped."""
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner
        from coord.bugbash import BugbashReport

        class _FakeRepoCfg:
            github = "acme/vimcode"
            default_branch = "develop"

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: _FakeRepoCfg()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        monkeypatch.setattr(
            cmd_bugbash, "discover_lanes",
            lambda cfg, repo, reference_backend="": [_prod_lane(machine="pc1", platform="win-native")],
        )
        # #3580: this test is about --lane-timeout/--cost-cap-per-lane
        # reaching the dispatch seam, not the catalogue fetch — stub it out
        # rather than hitting a real `gh` call.
        monkeypatch.setattr(cmd_bugbash, "_fetch_catalogue_text", lambda slug, branch: None)

        captured = {}

        def fake_run_bugbash(bb_config, *, explorer, runner, open_issues_fetcher, closed_issues_fetcher, confirm):
            captured["bb_config"] = bb_config
            captured["explorer"] = explorer
            return BugbashReport(repo=bb_config.repo, rounds=[], termination_reason="round_cap", total_cost=0.0)

        monkeypatch.setattr(cmd_bugbash, "run_bugbash", fake_run_bugbash)

        dispatch_kwargs = {}

        def fake_dispatch(lane, round_num, **kwargs):
            dispatch_kwargs.update(kwargs)
            return ExploreOutcome(ok=True)

        monkeypatch.setattr(cmd_bugbash, "_dispatch_and_await_lane", fake_dispatch)

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            [
                "vimcode", "--reference", "win-native",
                "--lane-timeout", "777", "--cost-cap-per-lane", "33",
                "--dry-run", "-y",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "explorer" in captured

        # Drive the captured `explorer` closure to confirm it forwards the
        # CLI's --lane-timeout/--cost-cap-per-lane straight through to the
        # production seam.
        captured["explorer"](_prod_lane(machine="pc1", platform="win-native"), 1)
        assert dispatch_kwargs.get("timeout") == 777.0
        assert dispatch_kwargs.get("cost_cap") == 33.0

    def test_catalogue_text_reaches_dispatch_and_await_lane(self, monkeypatch):
        """#3580: the catalogue fetched once per run must reach
        `_dispatch_and_await_lane` (and therefore `build_exploration_briefing`)
        for every lane/round, not just be fetched and printed."""
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner
        from coord.bugbash import BugbashReport

        class _FakeRepoCfg:
            github = "acme/vimcode"
            default_branch = "develop"

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: _FakeRepoCfg()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        monkeypatch.setattr(
            cmd_bugbash, "discover_lanes",
            lambda cfg, repo, reference_backend="": [_prod_lane(machine="pc1", platform="win-native")],
        )
        catalogue_text = "version: 1\njourneys: [{id: j1, lanes: [win-native], reference: spec, expected: e, steps: s}]\n"
        monkeypatch.setattr(cmd_bugbash, "_fetch_catalogue_text", lambda slug, branch: catalogue_text)

        captured = {}

        def fake_run_bugbash(bb_config, *, explorer, runner, open_issues_fetcher, closed_issues_fetcher, confirm):
            captured["explorer"] = explorer
            return BugbashReport(repo=bb_config.repo, rounds=[], termination_reason="round_cap", total_cost=0.0)

        monkeypatch.setattr(cmd_bugbash, "run_bugbash", fake_run_bugbash)

        dispatch_kwargs = {}

        def fake_dispatch(lane, round_num, **kwargs):
            dispatch_kwargs.update(kwargs)
            return ExploreOutcome(ok=True)

        monkeypatch.setattr(cmd_bugbash, "_dispatch_and_await_lane", fake_dispatch)

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            ["vimcode", "--reference", "win-native", "--dry-run", "-y"],
        )
        assert result.exit_code == 0, result.output
        captured["explorer"](_prod_lane(machine="pc1", platform="win-native"), 1)
        assert dispatch_kwargs.get("catalogue_text") == catalogue_text

    def test_explorer_progress_lines_are_lane_prefixed(self, monkeypatch, capsys):
        """#3602 review round 1: the new lane-prefixed progress lines
        (``[platform@machine] round N: dispatching...`` / ``...: done in
        ...``) are user-visible CLI output, printed from the real
        `explorer` closure `bugbash_run_cmd` builds — not from
        `run_bugbash` (which every other CLI test fakes). Capture the
        closure and drive it directly against a faked
        `_dispatch_and_await_lane`, so the actual prefixing/formatting
        code runs and is asserted on, rather than assumed from reading
        the source."""
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner
        from coord.bugbash import BugbashReport

        class _FakeRepoCfg:
            github = "acme/vimcode"
            default_branch = "develop"

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: _FakeRepoCfg()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        monkeypatch.setattr(
            cmd_bugbash, "discover_lanes",
            lambda cfg, repo, reference_backend="": [_prod_lane(machine="pc1", platform="win-native")],
        )
        monkeypatch.setattr(cmd_bugbash, "_fetch_catalogue_text", lambda slug, branch: None)

        captured = {}

        def fake_run_bugbash(bb_config, *, explorer, runner, open_issues_fetcher, closed_issues_fetcher, confirm):
            captured["explorer"] = explorer
            return BugbashReport(repo=bb_config.repo, rounds=[], termination_reason="round_cap", total_cost=0.0)

        monkeypatch.setattr(cmd_bugbash, "run_bugbash", fake_run_bugbash)
        monkeypatch.setattr(
            cmd_bugbash, "_dispatch_and_await_lane",
            lambda lane, round_num, **kwargs: ExploreOutcome(ok=True, findings=(), cost=1.5),
        )

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            ["vimcode", "--reference", "win-native", "--dry-run", "-y"],
        )
        assert result.exit_code == 0, result.output
        assert "explorer" in captured

        # CliRunner's own stdout capture is already torn down by the time
        # `invoke` returns, so this is a clean slate for the manual
        # closure call below.
        capsys.readouterr()
        captured["explorer"](_prod_lane(machine="pc1", platform="win-native"), 3)
        out = capsys.readouterr().out
        assert "[win-native@pc1] round 3: dispatching..." in out
        assert "[win-native@pc1] round 3: done in" in out
        assert "cost=1.50" in out

    def test_harvest_command_files_a_recovered_finding(self, monkeypatch):
        """End-to-end `coord bugbash harvest` against faked seams: an
        assignment that's already completed on its machine gets its
        findings parsed and filed through the normal dedupe/file/queue
        path — the #3569 recovery path for a lane that finished after the
        controller stopped waiting on it."""
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner

        class _FakeRepoCfg:
            github = "acme/vimcode"
            default_branch = "develop"

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: _FakeRepoCfg()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        monkeypatch.setattr(cmd_bugbash, "discover_lanes", lambda cfg, repo, reference_backend="": [])

        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("completed", exit_code=0),
        )

        log_line = (
            '{"type": "assistant", "message": {"content": [{"type": "text", '
            '"text": "done.\\n```bugbash-findings\\n'
            '[{\\"title\\": \\"Crash on install\\", \\"expected\\": \\"e\\", '
            '\\"actual\\": \\"a\\", \\"repro\\": \\"r\\", \\"evidence\\": \\"ev\\"}]\\n'
            '```"}]}}\n'
            '{"type": "result", "total_cost_usd": 6.08}'
        )

        class _Resp:
            status_code = 200
            text = log_line

            def raise_for_status(self):
                pass

        import httpx as httpx_mod
        monkeypatch.setattr(httpx_mod, "get", lambda *a, **k: _Resp())

        fake_runner_calls = []

        def fake_runner(args):
            args = list(args)
            fake_runner_calls.append(args)
            if args[:2] == ["issue", "create"]:
                return "#900 (vimcode) created\n"
            return "queued\n"

        monkeypatch.setattr(cmd_bugbash, "subprocess_coord_runner", fake_runner)
        monkeypatch.setattr(cmd_bugbash.github_ops, "get_open_issues", lambda slug: [])
        monkeypatch.setattr(cmd_bugbash, "_fetch_recently_closed_issues", lambda slug: [])

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            [
                "harvest", "asg-late-1",
                "--repo", "vimcode", "--machine", "pc1", "--lane", "win-native",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "filed+queued: #900" in result.output
        assert fake_runner_calls[0][:2] == ["issue", "create"]
        assert fake_runner_calls[1][:2] == ["drive-queue", "add"]

    def test_harvest_command_reports_still_running_without_filing(self, monkeypatch):
        """A harvest attempt against an assignment that hasn't actually
        finished yet must say so and exit nonzero — never silently read as
        "harvested, found nothing"."""
        import coord.commands.bugbash as cmd_bugbash
        from click.testing import CliRunner

        fake_cfg = _FakeRealConfig(machines=[_FakeRealMachine(name="pc1", host="pc1.local")])
        fake_cfg.repo = lambda name: object()
        monkeypatch.setattr(cmd_bugbash, "_load_config", lambda path: fake_cfg)
        monkeypatch.setattr(cmd_bugbash, "discover_lanes", lambda cfg, repo, reference_backend="": [])
        monkeypatch.setattr(
            "coord.commands._common.poll_until_terminal",
            lambda *a, **k: _FakePollOutcome("timeout"),
        )

        result = CliRunner().invoke(
            cmd_bugbash.bugbash_cmd,
            [
                "harvest", "asg-still-running",
                "--repo", "vimcode", "--machine", "pc1", "--lane", "win-native",
            ],
        )
        assert result.exit_code == 1
        assert "still running" in result.output
