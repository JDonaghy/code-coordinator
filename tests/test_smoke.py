"""Tests for smoke-test orchestration (coord/smoke.py)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from coord.config import Config, SmokeRule, SmokeTestsConfig, load
from coord.models import Assignment, Board, Machine, Repo
from coord.smoke import (
    MUTE_SMOKE_LEG_BUDGET,
    NO_SMOKE_VERDICT_MARKER,
    SMOKE_SYSTEM_PROMPT,
    SmokeCommand,
    build_smoke_briefing,
    dispatch_pending_smoke,
    dispatch_smoke,
    match_rules,
    mute_smoke_legs,
    mute_smoke_tally,
    pick_smoke_machine,
    resolve_rule_command,
    resolve_smoke_command,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(
        name="api", github="acme/api", depends_on=[], default_branch="main",
        test_command="make test",
    )


def _machine(name: str, host: str, *, caps: list[str], path: str = "/work/api") -> Machine:
    return Machine(
        name=name, host=host, capabilities=caps, repos=["api"],
        repo_paths={"api": path},
    )


@pytest.fixture
def gtk_and_server_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python"], path="/srv/api"),
            _machine("desktop-a", "desktop-a.tail", caps=["python", "gtk"], path="/d/api"),
        ],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[
                SmokeRule(files=["src/gtk/"], requires=["gtk"]),
                SmokeRule(files=["src/tui_main/"], requires=["terminal"]),
            ],
        ),
    )


def _completed(
    *, machine: str = "server", branch: str = "issue-1-fix", repo: str = "api",
) -> Assignment:
    return Assignment(
        machine_name=machine,
        repo_name=repo,
        issue_number=287,
        issue_title="GTK key routing fix",
        briefing="Worker briefing",
        assignment_id="abc123",
        status="done",
        branch=branch,
        dispatched_at=0.0,
        finished_at=1.0,
        type="work",
    )


# ── Rule matching ───────────────────────────────────────────────────────────


def test_match_rules_returns_required_capabilities() -> None:
    rules = [
        SmokeRule(files=["src/gtk/"], requires=["gtk"]),
        SmokeRule(files=["src/tui_main/"], requires=["terminal"]),
    ]
    caps = match_rules(["src/gtk/window.c", "src/lib/util.c"], rules)
    assert caps == ["gtk"]


def test_match_rules_unions_caps_across_rules() -> None:
    rules = [
        SmokeRule(files=["src/gtk/"], requires=["gtk"]),
        SmokeRule(files=["src/tui_main/"], requires=["terminal"]),
    ]
    caps = match_rules(["src/gtk/a.c", "src/tui_main/b.c"], rules)
    assert set(caps) == {"gtk", "terminal"}


def test_match_rules_returns_empty_for_no_match() -> None:
    rules = [SmokeRule(files=["src/gtk/"], requires=["gtk"])]
    assert match_rules(["docs/README.md", "src/core/util.c"], rules) == []


def test_match_rules_deduplicates_capabilities() -> None:
    rules = [
        SmokeRule(files=["src/gtk/"], requires=["gtk"]),
        SmokeRule(files=["app/gtk_main.c"], requires=["gtk"]),
    ]
    caps = match_rules(["src/gtk/x.c", "app/gtk_main.c"], rules)
    assert caps == ["gtk"]


def test_match_rules_no_trailing_slash_matches_files_too() -> None:
    """A rule `src/gtk` (no slash) is the loose form — catches gtk_helpers.c."""
    rules = [SmokeRule(files=["src/gtk"], requires=["gtk"])]
    assert match_rules(["src/gtk_helpers.c"], rules) == ["gtk"]


# ── Rule command override (#3056) ───────────────────────────────────────────


@pytest.fixture
def win_repo() -> Repo:
    return Repo(
        name="quadraui", github="acme/quadraui", depends_on=[], default_branch="main",
        test_command="cargo check -p quadraui --features win",
    )


def test_resolve_rule_command_returns_none_when_no_rule_declares_one() -> None:
    """A rule with no `command` behaves exactly as before #3056 — routing
    only, no say over which command runs."""
    rules = [SmokeRule(files=["src/win/"], requires=["windows"])]
    assert resolve_rule_command(["src/win/gpu.rs"], rules) is None


def test_resolve_rule_command_returns_the_matched_rules_command() -> None:
    rules = [
        SmokeRule(files=["src/win/"], requires=["windows"], command="cargo xwin test"),
    ]
    resolved = resolve_rule_command(["src/win/gpu.rs"], rules)
    assert resolved is not None
    assert resolved.command == "cargo xwin test"
    assert resolved.ci_equivalent is False
    assert resolved.from_rule is True
    assert "capability_rules[0]" in resolved.source


def test_resolve_rule_command_ignores_a_non_matching_rule() -> None:
    rules = [
        SmokeRule(files=["src/gtk/"], requires=["gtk"], command="make gtk-smoke"),
    ]
    assert resolve_rule_command(["src/win/gpu.rs"], rules) is None


def test_resolve_rule_command_first_declared_match_wins() -> None:
    """#3056: a diff touching both a gtk/ rule and a win/ rule, each with its
    own `command`, cannot union the two — the FIRST rule in
    `capability_rules` declaration order wins, deterministically (not dict
    ordering)."""
    rules = [
        SmokeRule(files=["src/gtk/"], requires=["gtk"], command="make gtk-smoke"),
        SmokeRule(files=["src/win/"], requires=["windows"], command="cargo xwin test"),
    ]
    touched = ["src/win/gpu.rs", "src/gtk/window.c"]
    resolved = resolve_rule_command(touched, rules)
    assert resolved is not None
    assert resolved.command == "make gtk-smoke"

    # Declaration order controls the outcome, not the order files are listed
    # in the diff or the order rules match within it — swap the declaration
    # order and the winner swaps too.
    rules_reordered = [
        SmokeRule(files=["src/win/"], requires=["windows"], command="cargo xwin test"),
        SmokeRule(files=["src/gtk/"], requires=["gtk"], command="make gtk-smoke"),
    ]
    resolved2 = resolve_rule_command(touched, rules_reordered)
    assert resolved2 is not None
    assert resolved2.command == "cargo xwin test"


def test_resolve_rule_command_skips_a_matching_rule_with_no_command() -> None:
    """A matching rule with no `command` is not a candidate — resolution
    keeps looking rather than stopping on it."""
    rules = [
        SmokeRule(files=["src/win/"], requires=["windows"]),
        SmokeRule(files=["src/win/gpu"], requires=["windows"], command="cargo xwin test"),
    ]
    resolved = resolve_rule_command(["src/win/gpu.rs"], rules)
    assert resolved is not None
    assert resolved.command == "cargo xwin test"


class TestResolveSmokeCommandRulePrecedence:
    """#3056: a matched rule's `command` must outrank every repo-wide
    source — that's the entire point of the feature."""

    def test_rule_command_outranks_ci_command(self, win_repo: Repo) -> None:
        smoke_cfg = SmokeTestsConfig(
            default_command="make smoke",
            capability_rules=[
                SmokeRule(
                    files=["src/win/"], requires=["windows"],
                    command="cargo xwin test",
                ),
            ],
        )
        repo = replace(win_repo, ci_command="cargo test -p quadraui --features win")
        resolved = resolve_smoke_command(
            repo, smoke_cfg, touched_files=["src/win/gpu.rs"]
        )
        assert resolved.command == "cargo xwin test"
        assert resolved.from_rule is True
        assert resolved.ci_equivalent is False

    def test_no_touched_files_falls_back_to_old_precedence(
        self, win_repo: Repo
    ) -> None:
        """Callers with no diff context (fleet-health, onboarding) are
        unaffected — the #3056 rule step is a no-op without `touched_files`."""
        smoke_cfg = SmokeTestsConfig(
            capability_rules=[
                SmokeRule(
                    files=["src/win/"], requires=["windows"],
                    command="cargo xwin test",
                ),
            ],
        )
        repo = replace(win_repo, ci_command="cargo test -p quadraui --features win")
        resolved = resolve_smoke_command(repo, smoke_cfg)
        assert resolved.command == "cargo test -p quadraui --features win"
        assert resolved.from_rule is False

    def test_non_matching_diff_falls_back_to_ci_command(self, win_repo: Repo) -> None:
        smoke_cfg = SmokeTestsConfig(
            capability_rules=[
                SmokeRule(
                    files=["src/win/"], requires=["windows"],
                    command="cargo xwin test",
                ),
            ],
        )
        repo = replace(win_repo, ci_command="cargo test -p quadraui --features win")
        resolved = resolve_smoke_command(
            repo, smoke_cfg, touched_files=["src/gtk/window.c"]
        )
        assert resolved.command == "cargo test -p quadraui --features win"
        assert resolved.from_rule is False

    def test_rule_with_no_command_is_byte_identical_to_pre_3056(
        self, win_repo: Repo
    ) -> None:
        smoke_cfg = SmokeTestsConfig(
            default_command="make smoke",
            capability_rules=[SmokeRule(files=["src/win/"], requires=["windows"])],
        )
        resolved = resolve_smoke_command(
            win_repo, smoke_cfg, touched_files=["src/win/gpu.rs"]
        )
        assert resolved.command == "make smoke"
        assert resolved.source == "smoke_tests.default_command"
        assert resolved.from_rule is False


class TestRuleCommandBriefingNote:
    def test_names_the_rule_as_provenance(self) -> None:
        resolved = resolve_rule_command(
            ["src/win/gpu.rs"],
            [
                SmokeRule(
                    files=["src/win/"], requires=["windows"],
                    command="cargo xwin test",
                ),
            ],
        )
        assert resolved is not None
        note = resolved.briefing_note()
        assert "capability_rules" in note
        assert "capability_rules[0]" in note

    def test_repo_command_note_is_unaffected_by_from_rule_default(self) -> None:
        """Back-compat: a plain repo-sourced SmokeCommand (from_rule defaults
        False) still gets the pre-#3056 briefing text."""
        note = SmokeCommand("npm test", "repos[portal].test_command", False).briefing_note()
        assert "NARROWER than CI" in note
        assert "capability_rules" not in note


# ── Machine selection ───────────────────────────────────────────────────────


def test_pick_smoke_prefers_capable_machine_different_from_worker(
    gtk_and_server_config: Config,
) -> None:
    """The worker machine (``server``) lacks ``gtk``, so the capable machine
    wins — #1402's worker preference never overrides a capability rule."""
    board = Board()
    choice = pick_smoke_machine(
        ["gtk"], "api", "server", board, gtk_and_server_config
    )
    assert choice is not None
    assert choice.machine.name == "desktop-a"
    assert choice.is_worker is False


def test_pick_smoke_returns_none_when_no_machine_has_capability(
    gtk_and_server_config: Config,
) -> None:
    cfg = replace(
        gtk_and_server_config,
        machines=[_machine("server", "server.tail", caps=["python"])],
    )
    choice = pick_smoke_machine(["gtk"], "api", "server", Board(), cfg)
    assert choice is None


def test_pick_smoke_falls_back_to_worker_machine_when_only_capable(
    repo: Repo,
) -> None:
    """Only the worker machine is capable — it is chosen either way."""
    cfg = Config(
        repos=[repo],
        machines=[_machine("desktop-a", "d.tail", caps=["python", "gtk"])],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    choice = pick_smoke_machine(["gtk"], "api", "desktop-a", Board(), cfg)
    assert choice is not None
    assert choice.machine.name == "desktop-a"
    assert choice.is_worker is True

    # Same outcome with the pre-#1402 ordering — capability is the only
    # constraint that can be satisfied here.
    legacy = pick_smoke_machine(
        ["gtk"], "api", "desktop-a", Board(), cfg, prefer_worker=False
    )
    assert legacy is not None
    assert legacy.machine.name == "desktop-a"
    assert legacy.is_worker is True
    assert "same machine" in legacy.rationale


# ── #1402: the Test stage prefers the machine with the warm build cache ─────


def test_pick_smoke_prefers_idle_worker_machine_over_idle_other(repo: Repo) -> None:
    """#1402: with two equally capable idle machines, the worker's own wins —
    its cargo target cache is already warm (~18 s vs ~3 min cold)."""
    cfg = Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python", "gtk"]),
            _machine("desktop-a", "d.tail", caps=["python", "gtk"]),
        ],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    choice = pick_smoke_machine(["gtk"], "api", "server", Board(), cfg)
    assert choice is not None
    assert choice.machine.name == "server"
    assert choice.is_worker is True
    assert "warm" in choice.rationale


def test_pick_smoke_prefer_worker_false_restores_different_machine_first(
    repo: Repo,
) -> None:
    """#1402: the old ordering is still reachable via ``prefer_worker=False``
    (review-style independence), so the flip is opt-out, not one-way."""
    cfg = Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python", "gtk"]),
            _machine("desktop-a", "d.tail", caps=["python", "gtk"]),
        ],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    choice = pick_smoke_machine(
        ["gtk"], "api", "server", Board(), cfg, prefer_worker=False
    )
    assert choice is not None
    assert choice.machine.name == "desktop-a"
    assert choice.is_worker is False


def test_pick_smoke_worker_preference_never_beats_capability_rules(
    repo: Repo,
) -> None:
    """#1402 acceptance: capability rules still bind.  The worker machine is
    idle but has no ``gtk``, so the Test stage goes to the capable machine
    even though that means a cold build."""
    cfg = Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python"]),
            _machine("desktop-a", "d.tail", caps=["python", "gtk"]),
        ],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    choice = pick_smoke_machine(["gtk"], "api", "server", Board(), cfg)
    assert choice is not None
    assert choice.machine.name == "desktop-a"
    assert choice.is_worker is False


def test_pick_smoke_busy_worker_machine_beats_busy_other(repo: Repo) -> None:
    """#1402: when every capable machine is busy the warm one still wins —
    the smoke queues on the worker machine rather than rebuilding cold."""
    cfg = Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python", "gtk"]),
            _machine("desktop-a", "d.tail", caps=["python", "gtk"]),
        ],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    board = Board(active=[
        Assignment(
            machine_name="desktop-a", repo_name="api", issue_number=99,
            issue_title="other", status="running", assignment_id="x",
        ),
        Assignment(
            machine_name="server", repo_name="api", issue_number=98,
            issue_title="mine", status="running", assignment_id="y",
        ),
    ])
    choice = pick_smoke_machine(["gtk"], "api", "server", board, cfg)
    assert choice is not None
    assert choice.machine.name == "server"
    assert choice.is_worker is True
    assert "busy" in choice.rationale


def test_pick_smoke_idle_other_beats_busy_worker_machine(repo: Repo) -> None:
    """#1402: a warm-but-busy worker machine loses to an idle capable one —
    queueing behind another worker costs more than the cold build saves."""
    cfg = Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python", "gtk"]),
            _machine("desktop-a", "d.tail", caps=["python", "gtk"]),
        ],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    board = Board(active=[
        Assignment(
            machine_name="server", repo_name="api", issue_number=98,
            issue_title="mine", status="running", assignment_id="y",
        )
    ])
    choice = pick_smoke_machine(["gtk"], "api", "server", board, cfg)
    assert choice is not None
    assert choice.machine.name == "desktop-a"
    assert choice.is_worker is False


# ── Config parsing ──────────────────────────────────────────────────────────


def test_smoke_config_defaults(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.smoke_tests.auto_queue is False
    assert cfg.smoke_tests.capability_rules == []


def test_smoke_config_parses_capability_rules(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tail
    repos: [api]
smoke_tests:
  auto_queue: true
  default_command: "make smoke"
  timeout_seconds: 300
  capability_rules:
    - files: ["src/gtk/"]
      requires: [gtk]
    - files: ["src/tui_main/"]
      requires: [terminal]
"""
    )
    cfg = load(p)
    assert cfg.smoke_tests.auto_queue is True
    assert cfg.smoke_tests.default_command == "make smoke"
    assert cfg.smoke_tests.timeout_seconds == 300
    assert len(cfg.smoke_tests.capability_rules) == 2
    assert cfg.smoke_tests.capability_rules[0].requires == ["gtk"]


def test_smoke_config_rejects_empty_files_or_requires(tmp_path: Path) -> None:
    from coord.config import ConfigError

    p = tmp_path / "coordinator.yml"
    p.write_text(
        """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tail
    repos: [api]
smoke_tests:
  auto_queue: true
  capability_rules:
    - files: []
      requires: [gtk]
"""
    )
    with pytest.raises(ConfigError, match="files must be non-empty"):
        load(p)


# ── Briefing assembly ───────────────────────────────────────────────────────


def test_briefing_includes_branch_command_and_required_caps() -> None:
    briefing = build_smoke_briefing(
        repo_github="acme/api",
        repo_name="api",
        branch="issue-287-fix",
        issue_number=287,
        issue_title="GTK fix",
        smoke_command="make smoke",
        required_caps=["gtk"],
        timeout_seconds=600,
        is_worker=False,
    )
    assert "issue-287-fix" in briefing
    assert "make smoke" in briefing
    assert "gtk" in briefing
    assert "SMOKE: pass" in briefing
    assert "running on the same machine" not in briefing


def test_briefing_warns_when_run_on_worker_machine() -> None:
    briefing = build_smoke_briefing(
        repo_github="acme/api", repo_name="api", branch="b",
        issue_number=1, issue_title="X", smoke_command="cmd",
        required_caps=["gtk"], timeout_seconds=60, is_worker=True,
    )
    assert "running on the same machine" in briefing


# ── #2170: baseline-red is a third dispatch-time outcome, not folded into fail ─


def test_smoke_system_prompt_documents_baseline_red_outcome() -> None:
    """The dispatched headless smoke agent must be told about the third
    outcome (#2170): a smoke command that exits with the runner's reserved
    baseline-red code AND prints its marker is NOT a branch failure, and
    must be reported distinctly (`SMOKE: baseline-red ...`, not `SMOKE:
    fail`) so `coord.notify`'s completion handler can tell the two apart —
    see `TestSmokeCompletionBaselineRedVerdict` in tests/test_notify.py."""
    from coord.revalidate import BASELINE_RED_OUTPUT_MARKER, RUNNER_BASELINE_RED_EXIT

    assert "SMOKE: baseline-red" in SMOKE_SYSTEM_PROMPT
    assert BASELINE_RED_OUTPUT_MARKER in SMOKE_SYSTEM_PROMPT
    assert f"exits {RUNNER_BASELINE_RED_EXIT}" in SMOKE_SYSTEM_PROMPT
    # Still documents the ordinary two outcomes.
    assert "SMOKE: pass" in SMOKE_SYSTEM_PROMPT
    assert "SMOKE: fail" in SMOKE_SYSTEM_PROMPT


# ── #2244: the printed marker is the verdict, not the exit code ─────────────


def test_smoke_system_prompt_says_the_marker_is_the_verdict() -> None:
    """A `claude -p` worker cannot signal through its exit code (an `exit 1`
    inside a tool call ends the tool call, not the session), so the prompt
    must not tell it the coordinator reads that code — the printed `SMOKE:`
    line is the verdict. See tests/test_notify.py::TestSmokeVerdictFailsClosed.
    """
    assert "NOT YOUR EXIT CODE" in SMOKE_SYSTEM_PROMPT
    # No line of the prompt may itself start with the marker, or a transcript
    # that quotes the prompt back would parse as a verdict.
    assert not any(
        line.startswith("SMOKE:") for line in SMOKE_SYSTEM_PROMPT.splitlines()
    )


def test_briefing_tells_the_worker_to_record_the_verdict_itself() -> None:
    """#2244 belt (the #2217 fix applied to the Test stage): the worker gets
    the PARENT work assignment id and the exact `coord test` command, so the
    authoritative board write doesn't depend on a transcript parse."""
    briefing = build_smoke_briefing(
        repo_github="acme/api", repo_name="api", branch="b",
        issue_number=1, issue_title="X", smoke_command="cmd",
        required_caps=[], timeout_seconds=60, is_worker=False,
        parent_assignment_id="884e2fe6eb5b",
    )
    assert "coord test --passed 884e2fe6eb5b" in briefing
    assert "coord test --fail" in briefing
    assert "884e2fe6eb5b" in briefing
    assert not any(line.startswith("SMOKE:") for line in briefing.splitlines())


def test_briefing_omits_the_coord_test_command_without_a_parent_id() -> None:
    briefing = build_smoke_briefing(
        repo_github="acme/api", repo_name="api", branch="b",
        issue_number=1, issue_title="X", smoke_command="cmd",
        required_caps=[], timeout_seconds=60, is_worker=False,
    )
    assert "coord test" not in briefing
    assert "SMOKE: pass" in briefing


# ── #2272: the 600s Bash ceiling, and the mute-leg retry budget ─────────────


def test_smoke_system_prompt_tells_the_worker_to_poll_a_backgrounded_run() -> None:
    """#2272 half 1. The ceiling is the one outcome the prompt never covered.

    It told the worker what to do for exit 0, for the #2170 baseline-red exit,
    and for any other non-zero — but not for "your Bash call did not return a
    result at all", which is exactly what Claude Code's ~600s ceiling
    produces: the call is moved to the background and the agent gets a task
    id. On 2026-08-15 the smoke worker narrated that it had kicked the suite
    off and ended its turn, five times in a row.
    """
    prompt = SMOKE_SYSTEM_PROMPT
    assert "600s" in prompt, "the prompt must name the ceiling it has to survive"
    assert "NEVER END YOUR TURN WITH THE SUITE STILL RUNNING" in prompt
    # The instruction has to be actionable, not just a warning: say to poll,
    # and say what to print if polling genuinely can't produce a status.
    lowered = prompt.lower()
    assert "poll" in lowered
    assert "background" in lowered
    assert "exit status" in lowered
    # The escape hatch: a stated verdict beats silence.
    assert "rather than ending mute" in lowered
    # Same invariant as #2244 — no prompt line may itself parse as a verdict.
    assert not any(line.startswith("SMOKE:") for line in prompt.splitlines())


# ── #2301: Monitor is an await-a-notification tool — it ends a headless leg ─


def test_smoke_system_prompt_does_not_recommend_monitor_for_polling() -> None:
    """#2272's own fix text used to name `Monitor` as a sanctioned polling
    option alongside `BashOutput`/`TaskOutput`. Two real smoke legs took that
    literally on 2026-08-15: reached `Monitor` via ToolSearch, called it to
    "wait" for a backgrounded `cargo test`, and ended their turn — which IS
    the end of a one-shot `claude -p` session (#1394). Both legs died in
    16-24s, before the suite even finished compiling, with no verdict ever
    printed; the backgrounded suite was reaped ~30s later by the coordinator.
    `Monitor` must not appear as a recommended polling tool in this prompt
    any more — `BashOutput`/`TaskOutput` still do the job without ending the
    turn."""
    prompt = SMOKE_SYSTEM_PROMPT
    assert "BashOutput" in prompt
    assert "TaskOutput" in prompt
    # The old phrasing offered Monitor as an alternative poll mechanism
    # ("...with the id, or `Monitor`)") right next to BashOutput/TaskOutput
    # — that specific recommendation must be gone.
    assert "or `Monitor`" not in prompt


def test_smoke_system_prompt_states_monitor_ends_a_headless_session() -> None:
    """Positive statement, not just an absence: the prompt must explain WHY
    Monitor (and tools like it) are wrong here, or the next worker reaches
    for it again under a different name — mirrors the #2158 lesson that
    WORKER_SYSTEM_PROMPT's ONE-SHOT section bakes in for work legs."""
    prompt = SMOKE_SYSTEM_PROMPT
    assert "#2301" in prompt
    assert "Monitor" in prompt
    lowered = prompt.lower()
    assert "await-a-notification" in lowered
    assert "one-shot" in lowered
    # The actual mechanism, not just "don't do this": ending the turn to
    # await a notification ends the whole headless session, permanently.
    assert "ending your turn ends the session itself" in lowered


def test_briefing_tells_the_worker_to_poll_a_backgrounded_run() -> None:
    """The briefing carries the same instruction as the system prompt — it is
    what the worker re-reads while deciding what to do next."""
    briefing = build_smoke_briefing(
        repo_github="acme/api", repo_name="api", branch="b",
        issue_number=1, issue_title="X", smoke_command="pytest -q",
        required_caps=[], timeout_seconds=60, is_worker=False,
        parent_assignment_id="884e2fe6eb5b",
    )
    assert "600s" in briefing
    assert "background" in briefing.lower()
    assert "Do not end your turn there" in briefing
    assert not any(line.startswith("SMOKE:") for line in briefing.splitlines())


@pytest.mark.parametrize(
    "reason, expected",
    [
        (None, 0),
        ("", 0),
        ("dispatched: Test stage running (#1426)", 0),
        ("headless smoke: 5 failed", 0),
        # Bare marker — what a pre-#2272 coordinator wrote. Reads back as ONE
        # leg, never zero: an upgrade mid-loop must not hand the row a fresh
        # budget and let the loop run the whole way again.
        ("no-verdict (#2244): the Test-stage worker abc ended without…", 1),
        ("no-verdict (#2244) x2: …", 2),
        ("no-verdict (#2244) x7 — parked", 7),
        # Case/spacing tolerance, so a hand-edited reason still counts.
        ("NO-VERDICT (#2244) X3", 3),
    ],
)
def test_mute_smoke_legs_reads_the_tally_back(reason, expected) -> None:
    assert mute_smoke_legs(reason) == expected


def test_mute_smoke_tally_round_trips_through_mute_smoke_legs() -> None:
    """The writer and the reader must agree, or the budget silently resets."""
    for count in range(1, 8):
        assert mute_smoke_legs(mute_smoke_tally(count)) == count
    # One leg renders bare — a count is only shown once there's one worth
    # seeing — but still reads back as one.
    assert mute_smoke_tally(1) == NO_SMOKE_VERDICT_MARKER


def test_dispatch_smoke_carries_the_mute_leg_tally_across_the_running_stamp(
    gtk_and_server_config: Config,
) -> None:
    """THE #2272 regression, and the reason #2244's bound never fired.

    `test_reason` is the ONLY field that survives from one Test-stage leg to
    the next, so it is where the "this leg came back mute" evidence lives.
    This stamp used to overwrite it with a flat literal — the counter was
    erased by the very retry it existed to count, so `already_mute_once` read
    False on every lap and the row re-dispatched forever (five legs observed,
    ~$0.12 each, stopped by hand).
    """
    parent = _completed(machine="server")
    parent.test_reason = f"{NO_SMOKE_VERDICT_MARKER}: worker abc ended mute"
    board = Board(completed=[parent])

    result = dispatch_smoke(
        parent, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-run"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )

    assert result is not None
    assert parent.test_state == "running"
    # The tally survived...
    assert mute_smoke_legs(parent.test_reason) == 1, (
        "the mute-leg tally must survive the `running` stamp, or the retry "
        f"budget can never fire — got {parent.test_reason!r}"
    )
    # ...and the row says, on the board, that it is a RETRY. Before this, a
    # row on its fifth mute lap rendered identically to one whose Test stage
    # had just started for the first time — which is why five identical laps
    # looked like progress.
    assert f"retry 2 of {MUTE_SMOKE_LEG_BUDGET}" in parent.test_reason
    assert "no verdict" in parent.test_reason


def test_dispatch_smoke_running_stamp_stays_plain_on_a_first_dispatch(
    gtk_and_server_config: Config,
) -> None:
    """The ordinary path is untouched: no mute history, no retry wording."""
    parent = _completed(machine="server")
    board = Board(completed=[parent])

    result = dispatch_smoke(
        parent, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-run"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )

    assert result is not None
    assert parent.test_state == "running"
    assert parent.test_reason == "dispatched: Test stage running (#1426)"
    assert mute_smoke_legs(parent.test_reason) == 0


def test_dispatch_smoke_refuses_a_row_parked_by_the_mute_budget(
    gtk_and_server_config: Config,
) -> None:
    """Once the budget parks the row, the dispatcher must not pick it back up
    — that refusal is what actually stops the billing."""
    from coord.smoke import TEST_STATE_BLOCKED  # noqa: PLC0415

    parent = _completed(machine="server")
    parent.test_state = TEST_STATE_BLOCKED
    parent.test_reason = f"{mute_smoke_tally(MUTE_SMOKE_LEG_BUDGET)}: parked"
    client = _FakeClient({"id": "smoke-run"})

    result = dispatch_smoke(
        parent, Board(completed=[parent]), gtk_and_server_config,
        http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )

    assert result is None
    assert client.calls == [], "a parked row must not dispatch another leg"


def test_environmental_death_preserves_the_mute_leg_tally(coord_db) -> None:
    """#2272: reconcile's #1605 environmental clear is the OTHER writer that
    replaces `test_reason` wholesale, and it must carry the tally too.

    `test_reason` is the only field that survives between Test-stage legs, so
    any writer that overwrites it hands the row a fresh retry budget. A row
    alternating "leg went mute" / "worker died environmentally" would then
    never reach the bound from either side — the same defect as the `running`
    stamp, one module over.
    """
    from coord.models import Assignment  # noqa: PLC0415
    from coord.reconcile import propagate_smoke_terminal_failure  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        _record_dispatched_assignment_local,
        get_connection,
        record_test_verdict,
    )

    work = Assignment(
        assignment_id="w-env", machine_name="laptop", repo_name="api",
        issue_number=7, issue_title="X", type="work", status="done",
        branch="issue-7",
    )
    _record_dispatched_assignment_local(assignment=work, repo_github="acme/api")
    record_test_verdict(
        assignment_id="w-env",
        test_state="running",
        test_reason=f"{mute_smoke_tally(1)} — dispatched: Test stage running",
    )

    propagate_smoke_terminal_failure(
        parent_assignment_id="w-env",
        failure_reason="api error 529",
    )

    row = get_connection().execute(
        "SELECT test_state, test_reason FROM assignments WHERE assignment_id=?",
        ("w-env",),
    ).fetchone()
    # #1605's semantics are unchanged: cleared for automatic re-dispatch.
    assert row["test_state"] is None
    assert "died environmentally" in row["test_reason"]
    # ...but the mute count survives it.
    assert mute_smoke_legs(row["test_reason"]) == 1, (
        "the environmental clear must preserve the mute-leg tally, or a row "
        f"can ping-pong past the budget forever — got {row['test_reason']!r}"
    )


def test_environmental_death_adds_no_tally_when_there_is_none(coord_db) -> None:
    """The ordinary #1605 case is untouched — an environmental death is NOT a
    mute leg, and must not start the counter."""
    from coord.models import Assignment  # noqa: PLC0415
    from coord.reconcile import propagate_smoke_terminal_failure  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        _record_dispatched_assignment_local,
        get_connection,
    )

    work = Assignment(
        assignment_id="w-env2", machine_name="laptop", repo_name="api",
        issue_number=8, issue_title="X", type="work", status="done",
        branch="issue-8",
    )
    _record_dispatched_assignment_local(assignment=work, repo_github="acme/api")

    propagate_smoke_terminal_failure(
        parent_assignment_id="w-env2",
        failure_reason="api error 529",
    )

    row = get_connection().execute(
        "SELECT test_state, test_reason FROM assignments WHERE assignment_id=?",
        ("w-env2",),
    ).fetchone()
    assert row["test_state"] is None
    assert mute_smoke_legs(row["test_reason"]) == 0


# ── dispatch_smoke (HTTP mocked) ────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._p


class _FakeClient:
    def __init__(self, payload: dict, *, health: dict | None = None) -> None:
        self._p = payload
        self.calls: list[tuple[str, dict]] = []
        # #1570 D: dispatch_smoke's capability-probe check GETs /health
        # before it POSTs /assign. Default: no `tool_versions` key, mirroring
        # an agent that predates #1570 B — the check fails OPEN on that, so
        # existing tests (none of which care about this) are unaffected.
        # `get_calls` is tracked separately so `.calls`-based assertions
        # elsewhere (POST /assign only) don't need touching.
        self._health = health if health is not None else {}
        self.get_calls: list[str] = []

    def post(self, url, *, json, timeout) -> _FakeResp:
        self.calls.append((url, json))
        return _FakeResp(self._p)

    def get(self, url, *, timeout) -> _FakeResp:
        self.get_calls.append(url)
        return _FakeResp(self._health)


def test_dispatch_smoke_skipped_when_auto_queue_off(
    gtk_and_server_config: Config,
) -> None:
    cfg = replace(
        gtk_and_server_config,
        smoke_tests=replace(gtk_and_server_config.smoke_tests, auto_queue=False),
    )
    result = dispatch_smoke(
        _completed(), Board(), cfg,
        http_client=_FakeClient({"id": "x"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is None


def test_dispatch_smoke_dispatches_work_when_no_rule_matches_but_test_command_set(
    gtk_and_server_config: Config,
) -> None:
    """#1426: a capability-rule miss used to mean "skip silently" — the
    blocker that kept the Test stage from ever dispatching for any repo/diff
    not explicitly covered by `capability_rules` (only `tui/`-  and
    `coord/dashboard/webapp/`-style rules were ever routed; everything else,
    including most Python work, silently never got a headless Test-stage
    dispatch at all). It now means "no extra hardware capability required" —
    `type="work"` still dispatches, to any capable-for-repo machine, as long
    as a real command is configured."""
    result = dispatch_smoke(
        _completed(), Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "x"}),
        diff_lookup=lambda repo, branch: ["docs/README.md"],
    )
    assert result is not None
    assert result.type == "smoke"
    # No capability required → prefers the (idle) worker machine (#1402).
    assert result.machine_name == "server"


def test_dispatch_smoke_skipped_when_no_rule_matches_and_no_command_configured(
) -> None:
    """A repo with no `test_command` and no `smoke_tests.default_command`
    has genuinely nothing to run — this must stay a silent skip, not a
    dispatch of a command-less assignment."""
    bare_repo = Repo(
        name="api", github="acme/api", depends_on=[], default_branch="main",
    )
    cfg = Config(
        repos=[bare_repo],
        machines=[_machine("server", "server.tail", caps=["python"])],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    result = dispatch_smoke(
        _completed(), Board(), cfg,
        http_client=_FakeClient({"id": "x"}),
        diff_lookup=lambda repo, branch: ["docs/README.md"],
    )
    assert result is None


def test_dispatch_smoke_skipped_for_mock_author_when_no_rule_matches(
    gtk_and_server_config: Config,
) -> None:
    """#930/#1176 + #1076/#1152: mock-author/test-author (Gate-A contract or
    fixture-only diffs) keep the OLD skip-on-miss behavior even though a real
    `test_command` is configured — a rule miss for these types means
    "genuinely nothing to smoke-test", and `dispatch_pending_reviews`
    back-fills `test_state="skipped"` for them. Dispatching a real suite run
    here would duplicate that and burn a full test run on a diff that never
    touches source."""
    mock_author = replace(_completed(), type="mock-author", assignment_id="ma1")
    result = dispatch_smoke(
        mock_author, Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "x"}),
        diff_lookup=lambda repo, branch: ["tests/acceptance/fixture.screen"],
    )
    assert result is None


def test_dispatch_smoke_skipped_for_failed_or_review(
    gtk_and_server_config: Config,
) -> None:
    failed = replace(_completed(), status="failed")
    review = replace(_completed(), type="review")
    diff = lambda repo, branch: ["src/gtk/x.c"]
    assert dispatch_smoke(
        failed, Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "x"}), diff_lookup=diff,
    ) is None
    assert dispatch_smoke(
        review, Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "x"}), diff_lookup=diff,
    ) is None


def test_dispatch_smoke_dispatches_for_mock_author_type(
    gtk_and_server_config: Config,
) -> None:
    """#930 fix: a completed ``type="mock-author"`` (Gate A) assignment must
    be eligible for automatic smoke dispatch, not just ``type="work"`` —
    mirrors the same fix applied to review/merge dispatch so the Test stage
    of Work -> Test -> Review -> Merge also fires for Gate A branches."""
    mock_author = replace(_completed(), type="mock-author", assignment_id="ma1")
    result = dispatch_smoke(
        mock_author, Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-ma"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is not None
    assert result.type == "smoke"
    assert result.review_of_assignment_id == "ma1"


def test_dispatch_smoke_sends_to_capable_different_machine(
    gtk_and_server_config: Config,
) -> None:
    board = Board()
    client = _FakeClient({"id": "smoke-1"})
    result = dispatch_smoke(
        _completed(machine="server"), board, gtk_and_server_config,
        http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
        now=42.0,
    )
    assert result is not None
    assert result.type == "smoke"
    assert result.machine_name == "desktop-a"  # has gtk; server doesn't
    assert result.assignment_id == "smoke-1"
    assert result.review_of_assignment_id == "abc123"
    assert result.dispatched_at == 42.0
    assert board.active == [result]

    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert "desktop-a.tail" in url
    assert payload["type"] == "smoke"
    assert payload["system_prompt"] == SMOKE_SYSTEM_PROMPT
    assert payload["review_target"] == "issue-1-fix"
    assert payload["repo_path"] == "/d/api"
    # Briefing should mention the test_command fallback (make test).
    assert "make test" in payload["briefing"]


def test_dispatch_smoke_pins_model_to_models_default(
    gtk_and_server_config: Config,
) -> None:
    """#2168: the Test stage used to dispatch with no `model` at all, so the
    agent fell through to the machine's ambient `claude -p` default (Opus)
    instead of the configured `models.default`. Mirrors the review path
    (coord/review.py, coord/gate_b.py): the resolved alias lands on both the
    wire payload (what the agent actually runs with) and the returned
    `Assignment.model` (what `coord usage` reports)."""
    cfg = replace(
        gtk_and_server_config,
        models=replace(gtk_and_server_config.models, default="haiku"),
    )
    client = _FakeClient({"id": "smoke-1"})
    result = dispatch_smoke(
        _completed(), Board(), cfg,
        http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is not None
    assert result.model == "haiku"

    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert payload["model"] == "haiku"


def test_dispatch_smoke_model_pin_ignores_models_labels(
    gtk_and_server_config: Config,
) -> None:
    """#2168: the Test stage's difficulty is a property of the repo's test
    command, never the issue's tier label — mirrors #1430's review-path
    decision. A `models.labels` entry that would route a `type="work"`
    dispatch to `opus` must NOT leak into the Test stage's model pin; it
    must still resolve to `models.default`. Guards against re-introducing
    the #1798 label leak through this new path."""
    cfg = replace(
        gtk_and_server_config,
        models=replace(
            gtk_and_server_config.models,
            default="sonnet",
            labels={"tier:large": "opus"},
        ),
    )
    client = _FakeClient({"id": "smoke-1"})
    result = dispatch_smoke(
        _completed(), Board(), cfg,
        http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is not None
    assert result.model == "sonnet"

    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert payload["model"] == "sonnet"


def test_dispatch_smoke_refuses_machine_whose_probe_contradicts_its_capability(
    gtk_and_server_config: Config,
) -> None:
    """#1570 D: desktop-a *claims* gtk in coordinator.yml, but its own
    /health probe (#1570 B) says GTK4 isn't actually installed there —
    dispatch_smoke must refuse to route rather than dispatch a worker that
    fails deep into a smoke run for an unrelated-looking reason."""
    board = Board()
    client = _FakeClient(
        {"id": "smoke-1"},
        health={
            "tool_versions": {
                "gtk4": {
                    "found": False, "version": None, "min_version": None,
                    "meets_floor": None, "capability": "gtk", "ok": False,
                },
            }
        },
    )
    result = dispatch_smoke(
        _completed(machine="server"), board, gtk_and_server_config,
        http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is None
    assert board.active == []
    # Refused before ever POSTing /assign.
    assert client.calls == []
    assert client.get_calls == ["http://desktop-a.tail:7433/health"]


def test_dispatch_smoke_proceeds_when_health_has_no_tool_versions(
    gtk_and_server_config: Config,
) -> None:
    """#1570 D fails OPEN on missing telemetry: an agent that predates
    #1570 B (no `tool_versions` in /health) must not block smoke dispatch
    fleet-wide during rollout — only an explicit probe failure refuses."""
    board = Board()
    client = _FakeClient({"id": "smoke-2"}, health={})
    result = dispatch_smoke(
        _completed(machine="server"), board, gtk_and_server_config,
        http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is not None
    assert result.machine_name == "desktop-a"
    assert len(client.calls) == 1


def test_dispatch_smoke_marks_parent_test_state_running(
    gtk_and_server_config: Config,
) -> None:
    """#1395/#1426: dispatching the Test stage as a real assignment must not
    reopen the #1395 gap — the parent work row's `test_state` flips to
    'running' the moment the smoke assignment is dispatched, the same
    marker `coord test --running` set for the old local-subprocess path, so
    the board/TUI reads Test as Active for the run's duration instead of
    idle."""
    parent = _completed(machine="server")
    board = Board(completed=[parent])
    result = dispatch_smoke(
        parent, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-run"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is not None
    assert parent.test_state == "running"


def test_dispatch_smoke_uses_default_command_when_set(
    gtk_and_server_config: Config,
) -> None:
    cfg = replace(
        gtk_and_server_config,
        smoke_tests=replace(
            gtk_and_server_config.smoke_tests,
            default_command="make smoke",
        ),
    )
    client = _FakeClient({"id": "s2"})
    result = dispatch_smoke(
        _completed(), Board(), cfg, http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/x.c"],
    )
    assert result is not None
    assert "make smoke" in client.calls[0][1]["briefing"]


def test_dispatch_smoke_returns_none_on_http_failure(
    gtk_and_server_config: Config,
) -> None:
    import httpx

    class _Bad:
        def post(self, url, *, json, timeout):
            raise httpx.ConnectError("unreachable")

        def get(self, url, *, timeout):
            raise httpx.ConnectError("unreachable")

    board = Board()
    result = dispatch_smoke(
        _completed(), board, gtk_and_server_config,
        http_client=_Bad(),
        diff_lookup=lambda repo, branch: ["src/gtk/x.c"],
    )
    assert result is None
    assert board.active == []


def test_dispatch_smoke_returns_none_when_no_capable_machine(
    repo: Repo,
) -> None:
    cfg = Config(
        repos=[repo],
        machines=[_machine("server", "server.tail", caps=["python"])],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[SmokeRule(files=["src/gtk/"], requires=["gtk"])],
        ),
    )
    result = dispatch_smoke(
        _completed(), Board(), cfg,
        http_client=_FakeClient({"id": "x"}),
        diff_lookup=lambda repo, branch: ["src/gtk/x.c"],
    )
    assert result is None


# ── #1672: candidate-list fallback + one-shot unroutable report ─────────────
#
# #1678 (2026-08-01): `dispatch_smoke` picked ONE capability-matched machine,
# its `/health` probe read UNMET, and the router refused — correctly (#1570 D)
# — and then stopped, even though two other machines declared the same
# capability. The Test stage never started, no smoke row was ever created, and
# the identical refusal re-logged every 30 s to the daemon journal with nothing
# on the board to show for it.


def _gtk_probe(*, found: bool) -> dict:
    """A `/health` body whose gtk4 probe reports `found`."""
    return {
        "tool_versions": {
            "gtk4": {
                "found": found,
                "version": "4.12.0" if found else None,
                "min_version": None,
                "meets_floor": True if found else None,
                "capability": "gtk",
                "ok": found,
            },
        }
    }


class _MultiHostClient:
    """Fake httpx client with PER-HOST `/health` and `/assign` behaviour.

    `_FakeClient` answers every host identically, which cannot express the
    #1672 case at all (first machine unhealthy, second healthy). Keyed by
    hostname so a test can say exactly which machine is broken and how.
    """

    def __init__(
        self,
        *,
        health: dict[str, dict] | None = None,
        assign: dict[str, dict | Exception] | None = None,
        default_assign: dict | None = None,
    ) -> None:
        self._health = health or {}
        self._assign = assign or {}
        self._default_assign = default_assign or {"id": "smoke-ok"}
        self.get_calls: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _host(url: str) -> str:
        return url.split("//", 1)[1].split(":", 1)[0]

    @property
    def probed_hosts(self) -> list[str]:
        return [self._host(u) for u in self.get_calls]

    @property
    def assigned_hosts(self) -> list[str]:
        return [self._host(u) for u, _ in self.calls]

    def get(self, url, *, timeout) -> _FakeResp:
        self.get_calls.append(url)
        return _FakeResp(self._health.get(self._host(url), {}))

    def post(self, url, *, json, timeout) -> _FakeResp:
        self.calls.append((url, json))
        outcome = self._assign.get(self._host(url), self._default_assign)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResp(outcome)


@pytest.fixture
def three_gtk_config(repo: Repo) -> Config:
    """One incapable worker machine + THREE machines declaring `gtk`.

    Mirrors the #1678 fleet shape (three machines declared `rust`; the router
    only ever tried one). `server` deliberately lacks gtk so every test here
    also proves capability matching is never relaxed to make routing succeed.
    """
    return Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python"], path="/srv/api"),
            _machine("desktop-a", "desktop-a.tail", caps=["python", "gtk"], path="/a/api"),
            _machine("desktop-b", "desktop-b.tail", caps=["python", "gtk"], path="/b/api"),
            _machine("desktop-c", "desktop-c.tail", caps=["python", "gtk"], path="/c/api"),
        ],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[SmokeRule(files=["src/gtk/"], requires=["gtk"])],
        ),
    )


_GTK_DIFF = ["src/gtk/window.c"]


@pytest.fixture(autouse=True)
def _clear_soft_report_memo():
    """`coord.smoke._SOFT_REPORTS_SEEN` is process-global (it is what makes a
    transient dead end log once per daemon rather than every 30 s) — reset it
    around every test so one test's report can't silence another's."""
    from coord.smoke import _SOFT_REPORTS_SEEN

    _SOFT_REPORTS_SEEN.clear()
    yield
    _SOFT_REPORTS_SEEN.clear()


# ── rank_smoke_machines ─────────────────────────────────────────────────────


def test_rank_smoke_machines_returns_every_capable_machine_best_first(
    three_gtk_config: Config,
) -> None:
    from coord.smoke import rank_smoke_machines

    ranked = rank_smoke_machines(["gtk"], "api", "server", Board(), three_gtk_config)
    assert [c.machine.name for c in ranked] == ["desktop-a", "desktop-b", "desktop-c"]
    # The incapable worker machine is NOT in the list — capability matching is
    # the one thing a fallback must never relax.
    assert "server" not in [c.machine.name for c in ranked]


def test_rank_smoke_machines_head_is_exactly_pick_smoke_machine(
    three_gtk_config: Config, gtk_and_server_config: Config,
) -> None:
    """The refactor must not move the FIRST choice — only add the tail."""
    from coord.smoke import rank_smoke_machines

    boards = [
        Board(),
        Board(active=[replace(_completed(machine="desktop-a"), status="running")]),
    ]
    for cfg in (three_gtk_config, gtk_and_server_config):
        for board in boards:
            for worker in ("server", "desktop-a", "nowhere"):
                for prefer in (True, False):
                    ranked = rank_smoke_machines(
                        ["gtk"], "api", worker, board, cfg, prefer_worker=prefer,
                    )
                    pick = pick_smoke_machine(
                        ["gtk"], "api", worker, board, cfg, prefer_worker=prefer,
                    )
                    head = ranked[0].machine.name if ranked else None
                    assert head == (pick.machine.name if pick else None)


def test_rank_smoke_machines_puts_idle_before_busy(
    three_gtk_config: Config,
) -> None:
    busy = replace(_completed(machine="desktop-a"), status="running")
    board = Board(active=[busy])
    from coord.smoke import rank_smoke_machines

    ranked = rank_smoke_machines(["gtk"], "api", "server", board, three_gtk_config)
    names = [c.machine.name for c in ranked]
    assert names == ["desktop-b", "desktop-c", "desktop-a"]
    assert len(set(names)) == len(names)  # each candidate appears once


def test_rank_smoke_machines_empty_when_capability_unmatched(
    three_gtk_config: Config,
) -> None:
    from coord.smoke import rank_smoke_machines

    assert rank_smoke_machines(["cuda"], "api", "server", Board(), three_gtk_config) == []


# ── #2636: rank_smoke_machines consults pause state ─────────────────────────
#
# claude-coordinator#2569, 2026-08-22/23: a smoke leg landed on elitebook 82
# minutes into its declared `quiet_hours` window, and the operator suspended
# the machine ten minutes later — exactly what "no new dispatch in this
# window" invites. `rank_smoke_machines` consulted no pause state of any
# kind. The fix routes candidates through `follow_on_paused_set` — the
# #2240 spelling, NOT `paused_set`, so a release cordon (which must not
# block the tail of work already in flight) keeps working.


def test_rank_smoke_machines_skips_a_machine_inside_quiet_hours(
    three_gtk_config: Config,
) -> None:
    from datetime import datetime, time, timezone

    from coord.models import QuietHours
    from coord.smoke import rank_smoke_machines

    quiet_cfg = replace(
        three_gtk_config,
        machines=[
            m if m.name != "desktop-a" else replace(
                m, quiet_hours=QuietHours(start=time(22, 0), end=time(8, 0), tz="UTC"),
            )
            for m in three_gtk_config.machines
        ],
    )
    inside_window = datetime(2026, 8, 23, 5, 22, tzinfo=timezone.utc)  # 22:00-08:00
    outside_window = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

    ranked = rank_smoke_machines(
        ["gtk"], "api", "server", Board(), quiet_cfg, now=inside_window,
    )
    names = [c.machine.name for c in ranked]
    # desktop-a declares gtk and would otherwise rank first (config order) —
    # it must be filtered by quiet hours, not merely reordered.
    assert "desktop-a" not in names
    assert names == ["desktop-b", "desktop-c"]

    # Outside its window the same machine is a normal, rankable candidate.
    ranked_awake = rank_smoke_machines(
        ["gtk"], "api", "server", Board(), quiet_cfg, now=outside_window,
    )
    assert "desktop-a" in [c.machine.name for c in ranked_awake]


def test_rank_smoke_machines_incident_shape_routes_to_the_idle_non_quiet_worker(
    repo: Repo,
) -> None:
    """The exact #2569 incident: the worker's own machine declares no quiet
    hours and is idle; the OTHER capability-matched machine is inside its
    quiet-hours window right now. Smoke must route to the worker — which is
    both the correct machine (#1402: warm build cache) and the only one not
    filtered out."""
    from datetime import datetime, time, timezone

    from coord.models import QuietHours
    from coord.smoke import rank_smoke_machines

    precision = _machine("precision", "precision.tail", caps=["python"], path="/p/api")
    elitebook = replace(
        _machine("elitebook", "elitebook.tail", caps=["python"], path="/e/api"),
        quiet_hours=QuietHours(start=time(22, 0), end=time(8, 0), tz="America/Chicago"),
    )
    cfg = Config(
        repos=[repo],
        machines=[elitebook, precision],
        smoke_tests=SmokeTestsConfig(auto_queue=True, capability_rules=[]),
    )
    # 2026-08-22 23:22 CDT == 2026-08-23 04:22 UTC — 82 minutes into the window,
    # the exact moment smoke leg 4aad0a84b510 was dispatched to elitebook.
    quiet_moment = datetime(2026, 8, 23, 4, 22, tzinfo=timezone.utc)

    ranked = rank_smoke_machines(
        [], "api", "precision", Board(), cfg, now=quiet_moment,
    )
    assert [c.machine.name for c in ranked] == ["precision"]


def test_rank_smoke_machines_skips_an_explicitly_paused_machine(
    three_gtk_config: Config,
) -> None:
    from coord.machine_pause import local_pause
    from coord.smoke import rank_smoke_machines

    local_pause("desktop-a")

    ranked = rank_smoke_machines(["gtk"], "api", "server", Board(), three_gtk_config)
    names = [c.machine.name for c in ranked]
    assert "desktop-a" not in names
    assert names == ["desktop-b", "desktop-c"]


def test_rank_smoke_machines_still_ranks_a_cordoned_machine(
    three_gtk_config: Config,
) -> None:
    """The #2240 regression guard: a release cordon means "no NEW work", but
    a smoke leg is the tail of work already in flight (the Test stage for a
    completed work row), so it must NOT be filtered — the same reasoning
    `pick_reviewer_machine` already applies via `follow_on_paused_set`. If
    this ever starts filtering cordoned machines, it silently reaches for
    `paused_set` instead of `follow_on_paused_set` and reproduces the
    2026-08-14 drain deadlock in the Test stage."""
    from coord.machine_pause import local_set_cordon
    from coord.smoke import rank_smoke_machines

    local_set_cordon("desktop-a", target_version="0.5.77")

    ranked = rank_smoke_machines(["gtk"], "api", "server", Board(), three_gtk_config)
    names = [c.machine.name for c in ranked]
    assert names == ["desktop-a", "desktop-b", "desktop-c"]


def test_rank_smoke_machines_operator_set_quiet_hours_bind_same_as_config(
    three_gtk_config: Config,
) -> None:
    """#2146: a window set via `coord quiet-hours` (no `quiet_hours:` block
    in `coordinator.yml` at all) must bind exactly like a config-declared
    one — the fix must go through `follow_on_paused_set`/
    `_quiet_covered_names`, never re-read `Machine.quiet_hours` directly,
    which would miss operator-set windows entirely."""
    from datetime import datetime, timezone

    from coord.machine_pause import local_set_quiet_hours
    from coord.smoke import rank_smoke_machines

    # None of `three_gtk_config`'s machines declare a `quiet_hours:` block —
    # this window comes ENTIRELY from the operator-set store.
    local_set_quiet_hours("desktop-a", start="22:00", end="08:00", tz="UTC")
    inside_window = datetime(2026, 8, 23, 5, 22, tzinfo=timezone.utc)

    ranked = rank_smoke_machines(
        ["gtk"], "api", "server", Board(), three_gtk_config, now=inside_window,
    )
    names = [c.machine.name for c in ranked]
    assert "desktop-a" not in names
    assert names == ["desktop-b", "desktop-c"]


def test_dispatch_smoke_blocked_reason_names_pause_not_capability(
    three_gtk_config: Config,
) -> None:
    """#2636 acceptance: when every capability-matched machine is paused or
    quiet-covered, the recorded reason must say so — NOT the generic
    capability-shaped message, which would read as "no capable machine"
    while an operator stares at three capable machines (#1678's failure mode
    by a different route)."""
    from coord.machine_pause import local_pause

    for name in ("desktop-a", "desktop-b", "desktop-c"):
        local_pause(name)

    completed = _completed(machine="server")
    board = Board(completed=[completed])
    result = dispatch_smoke(
        completed, board, three_gtk_config,
        http_client=_MultiHostClient(),
        diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is None
    assert completed.test_state == "blocked"
    reason = completed.test_reason or ""
    assert "paused or inside its quiet-hours window" in reason
    for name in ("desktop-a", "desktop-b", "desktop-c"):
        assert name in reason
    # Must NOT read as a capability miss — all three machines DO declare gtk.
    assert "no configured machine declares capability" not in reason


# ── Fallback: try the NEXT capability-matched machine ───────────────────────


def test_dispatch_smoke_falls_back_to_next_machine_when_first_probe_unhealthy(
    three_gtk_config: Config,
) -> None:
    """#1672 acceptance: N machines declare C, the first probes unhealthy —
    route to the next healthy candidate instead of ending the stage."""
    board = Board()
    completed = _completed(machine="server")
    client = _MultiHostClient(health={
        "desktop-a.tail": _gtk_probe(found=False),   # declared gtk, probe says no
        "desktop-b.tail": _gtk_probe(found=True),
        "desktop-c.tail": _gtk_probe(found=True),
    })
    result = dispatch_smoke(
        completed, board, three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is not None
    assert result.machine_name == "desktop-b"
    assert board.active == [result]
    # Probed a then b, stopped as soon as one was healthy — c untouched.
    assert client.probed_hosts == ["desktop-a.tail", "desktop-b.tail"]
    assert client.assigned_hosts == ["desktop-b.tail"]
    assert client.calls[0][1]["repo_path"] == "/b/api"
    assert completed.test_state == "running"


def test_dispatch_smoke_walks_past_several_unhealthy_machines(
    three_gtk_config: Config,
) -> None:
    board = Board()
    client = _MultiHostClient(health={
        "desktop-a.tail": _gtk_probe(found=False),
        "desktop-b.tail": _gtk_probe(found=False),
        "desktop-c.tail": _gtk_probe(found=True),
    })
    result = dispatch_smoke(
        _completed(machine="server"), board, three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is not None
    assert result.machine_name == "desktop-c"
    assert client.probed_hosts == [
        "desktop-a.tail", "desktop-b.tail", "desktop-c.tail",
    ]


def test_dispatch_smoke_falls_back_when_first_machine_has_no_repo_path(
    repo: Repo,
) -> None:
    """A machine that declares the capability but has no `repo_paths` entry is
    the same class of dead end as an unhealthy probe — skip it, don't stop."""
    no_path = Machine(
        name="desktop-a", host="desktop-a.tail",
        capabilities=["python", "gtk"], repos=["api"], repo_paths={},
    )
    cfg = Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python"]),
            no_path,
            _machine("desktop-b", "desktop-b.tail", caps=["python", "gtk"], path="/b/api"),
        ],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[SmokeRule(files=["src/gtk/"], requires=["gtk"])],
        ),
    )
    client = _MultiHostClient(health={
        "desktop-a.tail": _gtk_probe(found=True),
        "desktop-b.tail": _gtk_probe(found=True),
    })
    result = dispatch_smoke(
        _completed(machine="server"), Board(), cfg,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is not None
    assert result.machine_name == "desktop-b"


def test_dispatch_smoke_healthy_first_machine_dispatches_with_no_extra_probing(
    three_gtk_config: Config,
) -> None:
    """The ordinary case is unchanged: one probe, one POST, first candidate."""
    board = Board()
    client = _MultiHostClient(health={
        "desktop-a.tail": _gtk_probe(found=True),
        "desktop-b.tail": _gtk_probe(found=True),
        "desktop-c.tail": _gtk_probe(found=True),
    })
    result = dispatch_smoke(
        _completed(machine="server"), board, three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is not None
    assert result.machine_name == "desktop-a"
    assert client.probed_hosts == ["desktop-a.tail"]
    assert client.assigned_hosts == ["desktop-a.tail"]


def test_dispatch_smoke_fallback_never_routes_to_an_incapable_machine(
    three_gtk_config: Config,
) -> None:
    """The guardrail: a green Test verdict from a machine that cannot run the
    suite is far worse than a refusal. When EVERY gtk machine is unhealthy the
    router must not fall back to `server`, which never declared gtk."""
    board = Board()
    completed = _completed(machine="server")
    client = _MultiHostClient(health={
        "desktop-a.tail": _gtk_probe(found=False),
        "desktop-b.tail": _gtk_probe(found=False),
        "desktop-c.tail": _gtk_probe(found=False),
    })
    result = dispatch_smoke(
        completed, board, three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is None
    assert board.active == []
    assert client.calls == []                      # nothing dispatched anywhere
    assert "server.tail" not in client.probed_hosts
    assert completed.test_state == "blocked"       # not "passed", not "skipped"


# ── Dead end: reported once, on the row ─────────────────────────────────────


def test_dispatch_smoke_records_blocked_reason_when_all_candidates_unhealthy(
    three_gtk_config: Config,
) -> None:
    """#1672 acceptance: with no healthy candidate the reason is persisted
    where the TUI/CLI can render it — not only in the daemon log."""
    completed = _completed(machine="server")
    board = Board(completed=[completed])
    client = _MultiHostClient(health={
        f"desktop-{s}.tail": _gtk_probe(found=False) for s in "abc"
    })
    result = dispatch_smoke(
        completed, board, three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is None
    assert completed.test_state == "blocked"
    reason = completed.test_reason or ""
    # Names the capability and EVERY machine it tried, so the operator can act
    # from the board alone.
    assert "gtk" in reason
    for name in ("desktop-a", "desktop-b", "desktop-c"):
        assert name in reason
    assert "coord diagnose" in reason  # states the recovery command
    assert "#1672" in reason


def test_dispatch_smoke_blocked_reason_is_persisted_to_the_db(
    three_gtk_config: Config, coord_db,
) -> None:
    """The row the CLI (`coord gates`) and the TUI read is the DB row."""
    from coord.state import record_dispatched_assignment

    completed = _completed(machine="server")
    record_dispatched_assignment(assignment=completed, repo_github="acme/api")
    board = Board(completed=[completed])
    client = _MultiHostClient(health={
        f"desktop-{s}.tail": _gtk_probe(found=False) for s in "abc"
    })
    assert dispatch_smoke(
        completed, board, three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    ) is None

    row = coord_db.execute(
        "SELECT test_state, test_reason, smoke_test FROM assignments "
        "WHERE assignment_id=?", (completed.assignment_id,),
    ).fetchone()
    assert row["test_state"] == "blocked"
    assert "desktop-a" in row["test_reason"]
    # The legacy pass/fail mirror stays NULL: nothing is wrong with the
    # branch, so `coord fix` must not become dispatchable off the back of it.
    assert row["smoke_test"] is None


def test_dispatch_smoke_blocked_report_fires_once_not_every_tick(
    three_gtk_config: Config, caplog,
) -> None:
    """#1672 acceptance: a single visible reason, not a silent retry loop.

    The second pass must not re-probe the fleet, must not re-record and must
    not re-log — that 30 s spin (#1678) is as much the bug as picking one
    machine was."""
    completed = _completed(machine="server")
    board = Board(completed=[completed])
    client = _MultiHostClient(health={
        f"desktop-{s}.tail": _gtk_probe(found=False) for s in "abc"
    })
    with caplog.at_level("WARNING", logger="coord.smoke"):
        assert dispatch_smoke(
            completed, board, three_gtk_config,
            http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
        ) is None
        first_round_probes = list(client.probed_hosts)
        assert len(first_round_probes) == 3

        for _ in range(5):
            assert dispatch_smoke(
                completed, board, three_gtk_config,
                http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
            ) is None
    assert client.probed_hosts == first_round_probes  # no re-probing
    assert client.calls == []
    summaries = [
        r for r in caplog.records if "cannot be routed" in r.getMessage()
    ]
    assert len(summaries) == 1
    assert summaries[0].levelname == "ERROR"  # loud, not a routine warning


def test_dispatch_pending_smoke_skips_a_blocked_row(
    three_gtk_config: Config, monkeypatch,
) -> None:
    """The bulk scan is the tick loop — a reported row must drop out of it."""
    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)

    completed = _completed(machine="server")
    board = Board(completed=[completed])
    client = _MultiHostClient(health={
        f"desktop-{s}.tail": _gtk_probe(found=False) for s in "abc"
    })
    import coord.smoke as _smoke

    real = _smoke.dispatch_smoke
    monkeypatch.setattr(
        _smoke, "dispatch_smoke",
        lambda c, b, cfg, **kw: real(
            c, b, cfg, http_client=client,
            diff_lookup=lambda r, br: _GTK_DIFF, **kw,
        ),
    )
    assert dispatch_pending_smoke(board, three_gtk_config) == []
    assert completed.test_state == "blocked"
    probes_after_first_tick = list(client.probed_hosts)

    # Later ticks: the row carries a verdict now, so the scan skips it.
    assert dispatch_pending_smoke(board, three_gtk_config) == []
    assert dispatch_pending_smoke(board, three_gtk_config) == []
    assert client.probed_hosts == probes_after_first_tick


def test_dispatch_smoke_records_blocked_when_zero_capable_machines(
    repo: Repo,
) -> None:
    """Zero capability-matched machines is the same loud single report — the
    pre-#1672 code logged a WARNING and left the row indistinguishable from
    "not started yet"."""
    cfg = Config(
        repos=[repo],
        machines=[_machine("server", "server.tail", caps=["python"])],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[SmokeRule(files=["src/gtk/"], requires=["gtk"])],
        ),
    )
    completed = _completed()
    result = dispatch_smoke(
        completed, Board(), cfg,
        http_client=_MultiHostClient(),
        diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is None
    assert completed.test_state == "blocked"
    assert "gtk" in (completed.test_reason or "")


def test_dispatch_smoke_transient_post_failure_leaves_the_row_redispatchable(
    three_gtk_config: Config,
) -> None:
    """A machine that is merely unreachable must NOT poison the row.

    It comes back; marking it blocked would cost the operator a manual reset
    for something the next tick fixes for free. Distinct from an explicit
    probe contradiction, which stays broken until somebody acts."""
    import httpx

    completed = _completed(machine="server")
    board = Board()
    client = _MultiHostClient(
        health={f"desktop-{s}.tail": _gtk_probe(found=True) for s in "abc"},
        assign={
            f"desktop-{s}.tail": httpx.ConnectError("unreachable") for s in "abc"
        },
    )
    result = dispatch_smoke(
        completed, board, three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is None
    assert board.active == []
    assert completed.test_state is None          # still eligible next tick
    # It genuinely tried every capable machine before giving up.
    assert client.assigned_hosts == [
        "desktop-a.tail", "desktop-b.tail", "desktop-c.tail",
    ]


def test_dispatch_smoke_transient_dead_end_is_logged_once_per_row(
    three_gtk_config: Config, caplog,
) -> None:
    """Re-dispatchable is not a licence to re-log every tick (#1672)."""
    import httpx

    completed = _completed(machine="server")
    client = _MultiHostClient(
        health={f"desktop-{s}.tail": _gtk_probe(found=True) for s in "abc"},
        assign={
            f"desktop-{s}.tail": httpx.ConnectError("unreachable") for s in "abc"
        },
    )
    with caplog.at_level("WARNING", logger="coord.smoke"):
        for _ in range(4):
            assert dispatch_smoke(
                completed, Board(), three_gtk_config,
                http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
            ) is None
    summaries = [
        r for r in caplog.records if "cannot be routed" in r.getMessage()
    ]
    assert len(summaries) == 1
    assert "re-dispatchable" in summaries[0].getMessage()


def test_dispatch_smoke_falls_back_from_an_unreachable_machine_to_a_live_one(
    three_gtk_config: Config,
) -> None:
    import httpx

    board = Board()
    client = _MultiHostClient(
        health={f"desktop-{s}.tail": _gtk_probe(found=True) for s in "abc"},
        assign={"desktop-a.tail": httpx.ConnectError("unreachable")},
    )
    result = dispatch_smoke(
        _completed(machine="server"), board, three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    )
    assert result is not None
    assert result.machine_name == "desktop-b"


def test_dispatch_smoke_mixed_hard_and_transient_failures_do_not_block(
    three_gtk_config: Config,
) -> None:
    """Two machines are durably broken, one is just down — still transient
    overall, because the fleet may route successfully on the next tick."""
    import httpx

    completed = _completed(machine="server")
    client = _MultiHostClient(
        health={
            "desktop-a.tail": _gtk_probe(found=False),
            "desktop-b.tail": _gtk_probe(found=False),
            "desktop-c.tail": _gtk_probe(found=True),
        },
        assign={"desktop-c.tail": httpx.ConnectError("unreachable")},
    )
    assert dispatch_smoke(
        completed, Board(), three_gtk_config,
        http_client=client, diff_lookup=lambda r, b: _GTK_DIFF,
    ) is None
    assert completed.test_state is None


# ── #685: get_issue_test_mode ───────────────────────────────────────────────


def test_get_issue_test_mode_returns_none_when_no_row(coord_db) -> None:
    """Returns None when the issue isn't in the local cache."""
    from coord.state import get_issue_test_mode

    assert get_issue_test_mode("api", 42) is None


def test_get_issue_test_mode_returns_none_when_no_label(coord_db) -> None:
    """Returns None when the issue row has no test-mode label."""
    import json
    from coord.state import get_issue_test_mode

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 42, "Fix bug", "", "open", json.dumps(["coord", "status:ready"]), 1.0),
    )
    coord_db.commit()
    assert get_issue_test_mode("api", 42) is None


def test_get_issue_test_mode_returns_smoke(coord_db) -> None:
    """Returns 'smoke' when the test-mode:smoke label is present."""
    import json
    from coord.state import get_issue_test_mode

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 42, "Fix bug", "", "open", json.dumps(["coord", "test-mode:smoke"]), 1.0),
    )
    coord_db.commit()
    assert get_issue_test_mode("api", 42) == "smoke"


def test_get_issue_test_mode_returns_auto(coord_db) -> None:
    """Returns 'auto' when the test-mode:auto label is present."""
    import json
    from coord.state import get_issue_test_mode

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 42, "Fix bug", "", "open", json.dumps(["coord", "test-mode:auto"]), 1.0),
    )
    coord_db.commit()
    assert get_issue_test_mode("api", 42) == "auto"


# ── #685: reconcile smoke-gate respects test-mode:smoke ───────────────────


def test_reconcile_skips_auto_smoke_for_smoke_mode_issue(
    gtk_and_server_config: Config, coord_db
) -> None:
    """reconcile() must NOT auto-dispatch smoke when the issue has test-mode:smoke."""
    import json
    from unittest.mock import patch as _patch
    from coord.reconcile import reconcile
    from coord.state import save_board

    # Seed the issue with test-mode:smoke.
    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 287, "GTK fix", "", "open", json.dumps(["coord", "test-mode:smoke"]), 1.0),
    )
    coord_db.commit()

    completed_work = _completed(machine="server")
    board = Board(active=[completed_work])
    save_board(board)

    def _fake_agent(_host: str, _port: int = 7433, **kw):
        return {
            "completed": [
                {"id": "abc123", "status": "done", "branch": "issue-287-fix"}
            ]
        }

    with _patch("coord.reconcile._query_agent", side_effect=_fake_agent):
        reconcile(board, gtk_and_server_config)

    # A smoke assignment must NOT have been appended to the board.
    smoke_assignments = [a for a in board.active if a.type == "smoke"]
    assert smoke_assignments == [], (
        "Expected no auto-smoke dispatch for test-mode:smoke issue; "
        f"got {smoke_assignments}"
    )


def test_reconcile_dispatches_auto_smoke_for_auto_mode_issue(
    gtk_and_server_config: Config, coord_db
) -> None:
    """reconcile() MUST call dispatch_smoke when the issue has test-mode:auto."""
    import json
    from unittest.mock import patch as _patch
    from coord.reconcile import reconcile
    from coord.state import save_board

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 287, "GTK fix", "", "open", json.dumps(["coord", "test-mode:auto"]), 1.0),
    )
    coord_db.commit()

    completed_work = _completed(machine="server")
    board = Board(active=[completed_work])
    save_board(board)

    def _fake_agent(_host: str, _port: int = 7433, **kw):
        return {
            "completed": [
                {"id": "abc123", "status": "done", "branch": "issue-287-fix"}
            ]
        }

    with _patch("coord.reconcile._query_agent", side_effect=_fake_agent), \
         _patch("coord.smoke.dispatch_smoke", return_value=None) as mock_dispatch:
        reconcile(board, gtk_and_server_config)
        assert mock_dispatch.called, (
            "Expected dispatch_smoke to be called for test-mode:auto issue"
        )


def test_reconcile_thin_client_respects_smoke_mode_via_daemon(
    gtk_and_server_config: Config, coord_db, monkeypatch
) -> None:
    """#906 regression: reconcile() runs from the thin-client-reachable `coord
    resume` (not just the daemon tick loop, as an earlier #906 allowlist
    comment incorrectly assumed). On a thin client the local `issues` table
    is an empty stub, so ``get_issue_test_mode`` must read the daemon's issue
    row rather than the local (empty) table and silently falling through to
    auto-dispatching a headless smoke test for an issue explicitly labeled
    ``test-mode:smoke``.

    #1946: that read is ``GET /issue/{repo}/{n}`` + ``test_mode_from_labels``,
    replacing the deprecated ``POST /issue-test-mode`` projection. What must
    not regress is the *outcome* — the daemon is consulted for this exact
    issue, and no smoke assignment is dispatched.
    """
    import coord.client as cc
    from unittest.mock import patch as _patch
    from coord.reconcile import reconcile
    from coord.state import save_board

    # Local `issues` table is EMPTY — mirrors the thin-client reality that
    # triggered the #906 bug (a local read would return None here even though
    # the issue really has test-mode:smoke on the daemon/GitHub).
    assert coord_db.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0

    # Seed the board locally first (as-if state.py's autouse fixture wrote it
    # before board_service was configured) so this setup step itself doesn't
    # trip the thin-client local-board guard.
    completed_work = _completed(machine="server")
    board = Board(active=[completed_work])
    save_board(board)

    class _FakeSvc:
        url = "http://daemon:7435"
        token = "t"

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())

    daemon_calls: list[tuple[str, int]] = []

    def _fake_fetch_issue(svc, repo_name, number, **kw):
        daemon_calls.append((repo_name, number))
        return {"repo_name": repo_name, "number": number,
                "labels": ["coord", "test-mode:smoke"]}

    monkeypatch.setattr(cc, "fetch_issue", _fake_fetch_issue)

    def _fake_agent(_host: str, _port: int = 7433, **kw):
        return {
            "completed": [
                {"id": "abc123", "status": "done", "branch": "issue-287-fix"}
            ]
        }

    with _patch("coord.reconcile._query_agent", side_effect=_fake_agent):
        reconcile(board, gtk_and_server_config)

    # The daemon was consulted for this exact issue.
    assert ("api", 287) in daemon_calls

    # No smoke assignment must have been auto-dispatched.
    smoke_assignments = [a for a in board.active if a.type == "smoke"]
    assert smoke_assignments == [], (
        "Expected no auto-smoke dispatch for test-mode:smoke issue even though "
        "the local `issues` table is empty (daemon has the real label) — "
        f"got {smoke_assignments}"
    )

    # The local issues table was never populated — proves no local fallback read.
    assert coord_db.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0


def test_reconcile_thin_client_falls_back_to_local_on_daemon_error(
    gtk_and_server_config: Config, coord_db, monkeypatch
) -> None:
    """If the daemon read for test-mode fails, get_issue_test_mode fails open to
    the (empty) local DB — matching pre-#906 "no label" behaviour rather than
    raising and breaking the whole reconcile pass."""
    import coord.client as cc
    from unittest.mock import patch as _patch
    from coord.reconcile import reconcile
    from coord.state import save_board

    completed_work = _completed(machine="server")
    board = Board(active=[completed_work])
    save_board(board)

    class _FakeSvc:
        url = "http://daemon:7435"
        token = "t"

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    # #1946: the read is GET /issue/{repo}/{n} now — the fail-open posture is
    # what this test pins, not which route fails.
    monkeypatch.setattr(
        cc,
        "fetch_issue",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("daemon down")),
    )

    def _fake_agent(_host: str, _port: int = 7433, **kw):
        return {
            "completed": [
                {"id": "abc123", "status": "done", "branch": "issue-287-fix"}
            ]
        }

    with _patch("coord.reconcile._query_agent", side_effect=_fake_agent), \
         _patch("coord.smoke.dispatch_smoke", return_value=None) as mock_dispatch:
        reconcile(board, gtk_and_server_config)

    # Falls open to "no label" behaviour → respects auto_queue=True → dispatches.
    assert mock_dispatch.called


# ── #1426: dispatch_pending_smoke — bulk backlog dispatch ───────────────────


def test_dispatch_pending_smoke_off_when_auto_queue_disabled(
    gtk_and_server_config: Config,
) -> None:
    cfg = replace(
        gtk_and_server_config,
        smoke_tests=replace(gtk_and_server_config.smoke_tests, auto_queue=False),
    )
    board = Board(completed=[_completed()])
    assert dispatch_pending_smoke(board, cfg) == []


def test_dispatch_pending_smoke_skips_rows_with_a_test_state(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    """A row that already has ANY test_state (passed/failed/skipped/running)
    must not be re-dispatched — it either has a verdict already, or someone
    (an interactive --smoke-of session, or an in-flight smoke assignment) is
    already handling it."""
    from unittest.mock import patch as _patch

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)

    for state in ("passed", "failed", "skipped", "running"):
        row = replace(_completed(), test_state=state)
        board = Board(completed=[row])
        with _patch("coord.smoke.dispatch_smoke") as mock_dispatch:
            result = dispatch_pending_smoke(board, gtk_and_server_config)
        assert result == []
        assert not mock_dispatch.called, f"must not dispatch for test_state={state!r}"


def test_dispatch_pending_smoke_skips_test_mode_smoke(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    """test-mode:smoke means the TUI offers the interactive smoke agent — the
    bulk headless path must not auto-dispatch for it."""
    from unittest.mock import patch as _patch

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: "smoke")

    board = Board(completed=[_completed()])
    with _patch("coord.smoke.dispatch_smoke") as mock_dispatch:
        result = dispatch_pending_smoke(board, gtk_and_server_config)
    assert result == []
    assert not mock_dispatch.called


def test_dispatch_pending_smoke_ignores_non_work_like_and_non_done_rows(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    from unittest.mock import patch as _patch

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)

    review_row = replace(_completed(), type="review", assignment_id="r1")
    active_row = replace(_completed(), status="running", assignment_id="w2")
    board = Board(completed=[review_row, active_row])
    with _patch("coord.smoke.dispatch_smoke") as mock_dispatch:
        result = dispatch_pending_smoke(board, gtk_and_server_config)
    assert result == []
    assert not mock_dispatch.called


def test_dispatch_pending_smoke_calls_dispatch_smoke_for_eligible_rows(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    from unittest.mock import patch as _patch

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)

    eligible = _completed()
    board = Board(completed=[eligible])
    sentinel = object()
    with _patch("coord.smoke.dispatch_smoke", return_value=sentinel) as mock_dispatch:
        result = dispatch_pending_smoke(board, gtk_and_server_config)
    assert mock_dispatch.called
    call_args = mock_dispatch.call_args
    assert call_args[0][0] is eligible
    assert result == [sentinel]


# ── #3099: an ADVISORY row with confirmed commits is the #1357 false
# positive and must be promoted to 'done' so Test actually dispatches,
# instead of being skipped forever (the livelock #3099 reports: `coord
# drive --accept-advisory` accepts the row locally, but nothing ever told
# `dispatch_pending_smoke` — the two halves of #1357 disagreed). ────────────


def test_dispatch_pending_smoke_promotes_advisory_row_with_confirmed_commits(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    from unittest.mock import patch as _patch

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)
    monkeypatch.setattr(
        "coord.github_ops.branch_commits_ahead_for_assignment", lambda a, c: 3
    )
    monkeypatch.setattr(
        "coord.state.promote_advisory_with_commits", lambda aid: True
    )

    row = replace(_completed(), status="advisory", review_state="advisory")
    board = Board(completed=[row])
    sentinel = object()
    with _patch("coord.smoke.dispatch_smoke", return_value=sentinel) as mock_dispatch:
        result = dispatch_pending_smoke(board, gtk_and_server_config)

    assert mock_dispatch.called, "promoted row must fall through to the normal 'done' dispatch"
    assert result == [sentinel]
    # The in-memory row is promoted too — a later dispatcher scanning the
    # SAME board object (e.g. dispatch_pending_reviews on a later tick) must
    # see 'done', not the stale 'advisory' this call started with.
    assert row.status == "done"
    assert row.review_state is None


def test_dispatch_pending_smoke_leaves_zero_commit_advisory_row_alone(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    """`ahead == 0` is a genuinely empty branch — #2416's bounded `coord
    retry` loop owns that recovery, not an auto-promotion here."""
    from unittest.mock import patch as _patch

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)
    monkeypatch.setattr(
        "coord.github_ops.branch_commits_ahead_for_assignment", lambda a, c: 0
    )

    row = replace(_completed(), status="advisory")
    board = Board(completed=[row])
    with _patch("coord.smoke.dispatch_smoke") as mock_dispatch:
        result = dispatch_pending_smoke(board, gtk_and_server_config)

    assert result == []
    assert not mock_dispatch.called
    assert row.status == "advisory"


def test_dispatch_pending_smoke_leaves_unconfirmable_advisory_row_alone(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    """`ahead is None` (repo missing from config, a transient `gh api
    compare` failure) must never be read as "safe to promote" — #2426's
    same fail-closed polarity as the driver's own git-based check."""
    from unittest.mock import patch as _patch

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)
    monkeypatch.setattr(
        "coord.github_ops.branch_commits_ahead_for_assignment", lambda a, c: None
    )

    row = replace(_completed(), status="advisory")
    board = Board(completed=[row])
    with _patch("coord.smoke.dispatch_smoke") as mock_dispatch:
        result = dispatch_pending_smoke(board, gtk_and_server_config)

    assert result == []
    assert not mock_dispatch.called
    assert row.status == "advisory"


def test_dispatch_pending_smoke_does_not_dispatch_when_the_db_promotion_races(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    """`promote_advisory_with_commits` returning False means some other
    writer already resolved the row a different way between this scan
    starting and the UPDATE running (e.g. an operator's `coord retry`) — the
    in-memory row must be left exactly as read, not guessed at."""
    from unittest.mock import patch as _patch

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)
    monkeypatch.setattr(
        "coord.github_ops.branch_commits_ahead_for_assignment", lambda a, c: 5
    )
    monkeypatch.setattr(
        "coord.state.promote_advisory_with_commits", lambda aid: False
    )

    row = replace(_completed(), status="advisory")
    board = Board(completed=[row])
    with _patch("coord.smoke.dispatch_smoke") as mock_dispatch:
        result = dispatch_pending_smoke(board, gtk_and_server_config)

    assert result == []
    assert not mock_dispatch.called
    assert row.status == "advisory"


# ── #1819: one branch, one Test run ─────────────────────────────────────────
#
# After a fix round an issue has TWO `work` rows on the SAME branch
# (`--fix-of` reuses the branch by design). `dispatch_smoke`'s dedupe was
# keyed to a single work row, so both rows dispatched their own Test worker —
# two machines running the identical suite on the identical branch, racing to
# write a verdict (observed live on #1797, 2026-08-04). Worse, the re-dispatch
# stamped `test_state="running"` over a verdict that had already satisfied the
# merge gate, so a verdict landing → merge enqueuing → re-dispatch clobbering
# the gate field became a self-sustaining loop.


def _fix_round_pair(branch: str = "issue-1-fix") -> tuple[Assignment, Assignment]:
    """The real shape: round 1 (review=request-changes) + the fix round that
    superseded it, both `done`, both on the SAME branch."""
    round1 = replace(
        _completed(branch=branch),
        assignment_id="b8bff1ea023d",
        dispatched_at=100.0,
        review_verdict="request-changes",
    )
    round2 = replace(
        _completed(branch=branch),
        assignment_id="406bcf394032",
        dispatched_at=200.0,
        review_verdict="approve",
    )
    return round1, round2


def test_dispatch_smoke_dedupes_on_the_branch_not_the_work_row(
    gtk_and_server_config: Config,
) -> None:
    """#1819 criterion 1: two `work` rows on one branch produce exactly ONE
    smoke dispatch — counted, not inferred from the absence of an error."""
    round1, round2 = _fix_round_pair()
    board = Board(completed=[round1, round2])
    client = _FakeClient({"id": "smoke-1"})
    diff = lambda repo, branch: ["src/gtk/window.c"]

    dispatched = [
        dispatch_smoke(
            row, board, gtk_and_server_config,
            http_client=client, diff_lookup=diff,
        )
        for row in (round2, round1)
    ]

    assert len([d for d in dispatched if d is not None]) == 1
    assert len([a for a in board.active if a.type == "smoke"]) == 1
    assert len(client.calls) == 1


def test_dispatch_smoke_never_targets_a_superseded_work_row(
    gtk_and_server_config: Config,
) -> None:
    """#1819 criterion 2: the round-1 row is superseded by the fix round on
    the same branch, so it is never a Test dispatch target — not even with an
    empty board.active (i.e. no in-flight smoke for the branch dedupe to
    catch)."""
    round1, round2 = _fix_round_pair()
    board = Board(completed=[round1, round2])

    assert dispatch_smoke(
        round1, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-1"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    ) is None
    assert board.active == []
    # ...and the row is left untouched: no "running" marker on a row nothing
    # is going to run.
    assert round1.test_state is None


def test_dispatch_smoke_superseded_by_a_still_running_fix_round(
    gtk_and_server_config: Config,
) -> None:
    """A fix round that is still RUNNING supersedes too — dispatching against
    the row it is rewriting tests a branch that is about to change."""
    round1, round2 = _fix_round_pair()
    round2 = replace(round2, status="running")
    board = Board(active=[round2], completed=[round1])

    assert dispatch_smoke(
        round1, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-1"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    ) is None


def test_dispatch_smoke_failed_later_row_does_not_supersede(
    gtk_and_server_config: Config,
) -> None:
    """A `failed` fix round produced nothing — the earlier row is still the
    branch's author and stays testable."""
    round1, round2 = _fix_round_pair()
    round2 = replace(round2, status="failed")
    board = Board(completed=[round1, round2])

    assert dispatch_smoke(
        round1, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-1"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    ) is not None


def test_dispatch_smoke_still_dispatches_per_branch_for_two_branches(
    gtk_and_server_config: Config,
) -> None:
    """#1819 criterion 4: the genuine multi-branch case is unaffected — two
    different branches still get their own smokes."""
    a = replace(
        _completed(branch="issue-1-fix"), assignment_id="w1", dispatched_at=100.0
    )
    b = replace(
        _completed(branch="issue-2-other"), assignment_id="w2", dispatched_at=110.0,
        issue_number=288,
    )
    board = Board(completed=[a, b])
    client = _FakeClient({"id": "smoke-x"})
    diff = lambda repo, branch: ["src/gtk/window.c"]

    first = dispatch_smoke(
        a, board, gtk_and_server_config, http_client=client, diff_lookup=diff
    )
    second = dispatch_smoke(
        b, board, gtk_and_server_config, http_client=client, diff_lookup=diff
    )
    assert first is not None and second is not None
    assert len([x for x in board.active if x.type == "smoke"]) == 2


def test_dispatch_pending_smoke_fix_round_shape_dispatches_once(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    """#1819 criterion 5, through the bulk path the daemon actually runs.

    The real shape after the re-test arm clears the stale verdict (`coord
    diagnose --stage test --reset` NULLs `test_state` on every work row for
    the issue): two `work` rows sharing a branch, the earlier with
    `review_verdict=request-changes`. Before #1819 this dispatched twice.
    """
    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)

    round1, round2 = _fix_round_pair()
    board = Board(completed=[round1, round2])
    client = _FakeClient({"id": "smoke-1"})
    # `dispatch_pending_smoke` has no injection seam of its own — it builds
    # the real client, so stub the module's `httpx` handle instead.
    monkeypatch.setattr("coord.smoke.httpx", client)
    monkeypatch.setattr(
        "coord.smoke._fetch_touched_files", lambda repo, branch: ["src/gtk/window.c"]
    )

    dispatched = dispatch_pending_smoke(board, gtk_and_server_config)

    assert len(dispatched) == 1
    assert len([a for a in board.active if a.type == "smoke"]) == 1
    # The run is keyed to the CURRENT work row, not the superseded one.
    assert dispatched[0].review_of_assignment_id == round2.assignment_id


def test_dispatch_pending_smoke_fix_round_shape_with_running_markers(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    """The exact board #1797 showed: both rows `test_state="running"`, the
    earlier `review_verdict=request-changes`. `running` is the #1395 transient
    marker, so the bulk scan must leave both alone rather than pile on a
    third and fourth Test worker.

    round2 keeps `test_state="running"` so it's caught by the pre-existing
    `test_state is not None` filter (verifying that path is untouched), but
    round1 is left with no verdict at all (`test_state=None`) so it actually
    reaches `dispatch_smoke` and must be skipped by the NEW #1819
    `superseding_work_row` check — round2 is the later work-like row on the
    same branch, so round1 is superseded and must not get a Test dispatch
    of its own. Without this, a bare `both running` board never drives that
    code path at all: the pre-existing filter would skip both rows before
    either reached `superseding_work_row`."""
    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)

    round1, round2 = _fix_round_pair()
    round2 = replace(round2, test_state="running")
    board = Board(completed=[round1, round2])

    assert dispatch_pending_smoke(board, gtk_and_server_config) == []


def test_dispatch_smoke_does_not_clobber_a_recorded_verdict(
    gtk_and_server_config: Config, coord_db,
) -> None:
    """#1819 (loop): dispatching a fresh Test run must not, BY ITSELF,
    un-satisfy a gate that was already satisfied.

    `running` is read as "no verdict yet" by every gate (#1395), so stamping
    it over a `passed` row retracts an answer the merge queue was already
    acting on — the clobber that made #1797 spin.
    """
    from coord.state import record_dispatched_assignment, record_test_verdict

    row = replace(_completed(), test_state="passed")
    board = Board(completed=[row])
    record_dispatched_assignment(assignment=row, repo_github="acme/api")
    record_test_verdict(assignment_id=row.assignment_id, test_state="passed")

    result = dispatch_smoke(
        row, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-1"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )

    assert result is not None          # the fresh run DID go out...
    assert row.test_state == "passed"  # ...and the verdict survived it
    persisted = coord_db.execute(
        "SELECT test_state FROM assignments WHERE assignment_id=?",
        (row.assignment_id,),
    ).fetchone()
    assert persisted["test_state"] == "passed"


def test_redispatch_keeps_the_entry_mergeable(
    gtk_and_server_config: Config,
) -> None:
    """#1819 (loop) criterion: verdict recorded → entry mergeable →
    re-dispatch fires → the entry is STILL mergeable.

    Asserting "exactly one dispatch happened" would pass on the old code path
    for a single-work-row issue and still ship the loop; this asserts the
    property the loop actually violated.
    """
    from coord.merge_queue import QueuedMerge, has_smoke_verdict

    round1, round2 = _fix_round_pair()
    # The verdict landed on the fix round, which is what the Test stage ran.
    round2 = replace(round2, test_state="passed", smoke_test="pass")
    board = Board(completed=[round1, round2])

    entry = QueuedMerge(
        assignment_id=round2.assignment_id,
        repo_name="api",
        repo_github="acme/api",
        branch=round2.branch,
        target_branch="main",
        issue_number=round2.issue_number,
        issue_title=round2.issue_title,
        assignment_type="work",
        required_gates=["smoke", "merge"],
    )
    assert has_smoke_verdict(entry, board)

    # A re-dispatch fires (base moved, operator hit the dashboard button, ...).
    smoke = dispatch_smoke(
        round2, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-redispatch"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert smoke is not None

    # The gate the merge queue reads is STILL satisfied — the loop is broken.
    assert has_smoke_verdict(entry, board)
    # ...and a THIRD run can't pile on while that one is in flight.
    assert dispatch_smoke(
        round2, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-third"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    ) is None


def test_dispatch_pending_smoke_says_why_it_skipped_test_mode_smoke(
    gtk_and_server_config: Config, monkeypatch, caplog,
) -> None:
    """#2024: the `test-mode:smoke` skip is correct but was completely SILENT,
    and silence is what dead-ends an unattended `--fix-of` round — review
    dispatch is held until this row carries a passed/skipped verdict
    (`pipeline.test_precedes_review`) and, under this policy, no automatic
    component will ever record one. The log line has to name the row and the
    two commands that clear it (vimcode#635: 25 min, then 160 min, each
    cleared by hand)."""
    import logging

    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: "smoke")
    # Process-lifetime dedupe set — isolate this test from any earlier one.
    monkeypatch.setattr("coord.smoke._TEST_MODE_SKIP_LOGGED", set())

    board = Board(completed=[_completed()])
    with caplog.at_level(logging.INFO, logger="coord.smoke"):
        assert dispatch_pending_smoke(board, gtk_and_server_config) == []

    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "test-mode:smoke" in msg
    assert "abc123" in msg          # the row an operator has to act on
    assert "coord test" in msg      # ...and how
    assert "--smoke-of" in msg
    # The policy itself is untouched: still no dispatch, no verdict invented.
    assert board.active == []
    assert board.completed[0].test_state is None

    # ...and it says it ONCE per process, not once per tick. #1678 is the
    # standing lesson here: this row can legitimately sit unresolved for
    # hours, and a refusal re-logged every 30 s against a state that cannot
    # change is its own kind of noise.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="coord.smoke"):
        assert dispatch_pending_smoke(board, gtk_and_server_config) == []
    assert [r for r in caplog.records if "test-mode:smoke" in r.getMessage()] == []


def _fix_assignment(*, parent_id: str = "work-1", branch: str = "issue-1-fix") -> Assignment:
    """The literal shape `coord.auto_loop._dispatch_fix` produces for the
    common case: `type=fix_type` where `fix_type` is `"work"` unless the
    source row was `"test-author"`, `review_of_assignment_id=work.assignment_id`
    (link back to the row it fixes), `branch=work.branch` (same branch —
    `--fix-of` reuses it by design), `review_iteration=iteration` (the bounce
    counter). Not a hypothetical fixture — every field here is copied from
    `_dispatch_fix`'s actual `Assignment(...)` construction in
    `coord/auto_loop.py`.
    """
    return replace(
        _completed(branch=branch),
        assignment_id="fix-1",
        dispatched_at=200.0,
        review_of_assignment_id=parent_id,
        review_iteration=1,
    )


def test_dispatch_smoke_dispatches_for_a_fix_assignment(
    gtk_and_server_config: Config,
) -> None:
    """#2024 Ask 1, proved directly rather than asserted in a commit message:
    a completed **fix** assignment (see `_fix_assignment` above) gets a
    Test-stage `smoke` assignment dispatched — exactly like a fresh work row
    does. There never was a `type` filter excluding fix rows: `WORK_LIKE_TYPES`
    only discriminates on `type`, and a fix row's `type` IS `"work"`. The dead
    end this issue reports is specific to the `test-mode:smoke` per-issue
    POLICY skip covered above, not a structural gap in the dispatch path
    itself. Asserted on the board, per the issue's own acceptance criterion —
    not inferred from the gate summary.
    """
    parent = replace(_completed(), assignment_id="work-1", dispatched_at=100.0)
    fix = _fix_assignment()
    board = Board(completed=[parent, fix])

    dispatched = dispatch_smoke(
        fix, board, gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-fix-1"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )

    assert dispatched is not None
    assert dispatched.type == "smoke"
    assert dispatched.review_of_assignment_id == "fix-1"
    assert dispatched in board.active


def test_dispatch_pending_smoke_dispatches_for_a_fix_assignment_through_bulk_path(
    gtk_and_server_config: Config, monkeypatch,
) -> None:
    """Same claim as above, through `dispatch_pending_smoke` — the single
    choke point `reconcile()` (human-invoked `coord resume`) and `coord
    notify` (the unattended 5-minute timer) actually route bulk Test-stage
    dispatch through. Proving it only against `dispatch_smoke` would leave
    open the question this issue is actually about: does the UNATTENDED path
    reach a fix row at all. It does, with no per-issue test-mode policy set.
    """
    monkeypatch.setattr("coord.state.get_issue_test_mode", lambda *a, **k: None)
    monkeypatch.setattr(
        "coord.smoke.httpx", _FakeClient({"id": "smoke-fix-1"}),
    )
    monkeypatch.setattr(
        "coord.smoke._fetch_touched_files", lambda repo, branch: ["src/gtk/window.c"]
    )

    parent = replace(_completed(), assignment_id="work-1", dispatched_at=100.0)
    fix = _fix_assignment()
    board = Board(completed=[parent, fix])

    dispatched = dispatch_pending_smoke(board, gtk_and_server_config)

    assert len(dispatched) == 1
    assert dispatched[0].type == "smoke"
    assert dispatched[0].review_of_assignment_id == "fix-1"
    assert dispatched[0] in board.active
