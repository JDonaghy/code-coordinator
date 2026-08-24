"""Per-leg spend ceiling (#2131).

Nothing anywhere capped what a single worker leg could spend. Measured
2026-08-08 → 08-11 (393 legs, $907.04): the twelve legs above $10 were 3% of
all legs and 19% of the whole bill, the worst single leg $18.74. These tests
guard the policy layer that bounds that tail:

* the ``budget:`` config block (absent == no ceiling == today's behaviour),
* the live cost meter that answers "how much has this leg spent?",
* the reap watchdog that warns, then kills, then records a *distinguishable*
  terminal reason,
* and the refusals that stop that reason being silently re-spent.

The black-box test at the bottom drives the real ``AgentServer`` with a
stubbed stream-json transcript that crosses the threshold, and asserts on the
rendered outcome — not on internals.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coord.agent import (
    FAILED,
    SPEND_CEILING_EXIT,
    AgentServer,
    AssignmentSpec,
    _wait_for_proc_or_result,
)
from coord.config import BudgetConfig, ConfigError, parse_mapping
from coord.spend_ceiling import (
    SPEND_CEILING_REASON_PREFIX,
    LiveCostMeter,
    format_spend_ceiling_reason,
    is_spend_ceiling_reason,
)

# ── config: absent block must never change an existing deployment ───────────

_BASE_CONFIG = {
    "repos": [{"name": "api", "path": "/tmp/api", "github": "o/api"}],
    "machines": [{"name": "m1", "host": "h", "repos": ["api"]}],
}


def test_absent_budget_block_means_no_ceiling() -> None:
    """The upgrade-safety property: no config, no ceiling, no behaviour change."""
    cfg = parse_mapping(dict(_BASE_CONFIG))
    assert cfg.budget.ceiling_for("work") is None
    assert cfg.budget.ceiling_for("smoke") is None
    assert cfg.budget.ceiling_for(None) is None


def test_ceiling_is_configurable_per_type() -> None:
    cfg = parse_mapping(
        {**_BASE_CONFIG, "budget": {
            "per_leg_ceiling_usd": 8.0,
            "type_ceilings": {"smoke": 2.0, "chat": 0},
        }}
    )
    # No override → the global ceiling.
    assert cfg.budget.ceiling_for("work") == 8.0
    # An $8 ceiling is very wrong for a $0.44 median smoke leg.
    assert cfg.budget.ceiling_for("smoke") == 2.0
    # An explicit 0 disables the ceiling for that type alone — it must NOT
    # fall back to the global one.
    assert cfg.budget.ceiling_for("chat") is None


def test_zero_global_ceiling_disables_everything() -> None:
    cfg = parse_mapping({**_BASE_CONFIG, "budget": {"per_leg_ceiling_usd": 0}})
    assert cfg.budget.ceiling_for("work") is None


@pytest.mark.parametrize(
    "block",
    [
        {"per_leg_ceiling_usd": -1},
        {"per_leg_ceiling_usd": "eight"},
        {"per_leg_ceiling_usd": True},
        {"type_ceilings": {"work": -1}},
        {"type_ceilings": {"work": "nope"}},
        {"type_ceilings": [1, 2]},
        "not-a-mapping",
    ],
)
def test_invalid_budget_block_is_a_config_error(block) -> None:
    with pytest.raises(ConfigError):
        parse_mapping({**_BASE_CONFIG, "budget": block})


# ── the reason string is what makes a ceiling kill distinguishable ──────────


def test_reason_round_trips_and_does_not_match_other_failures() -> None:
    reason = format_spend_ceiling_reason(12.4123, 8.0, "work")
    assert reason == f"{SPEND_CEILING_REASON_PREFIX}$12.41 of $8.00 (type=work)"
    assert is_spend_ceiling_reason(reason)
    # Must not collide with the other diagnostics sharing `failure_reason`.
    assert not is_spend_ceiling_reason("usage limit — resets 8:30pm")
    assert not is_spend_ceiling_reason("529 Overloaded")
    assert not is_spend_ceiling_reason(None)
    assert not is_spend_ceiling_reason("")


# ── the live cost meter ─────────────────────────────────────────────────────


def _assistant(msg_id: str, *, model: str = "claude-opus-5", **usage) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"id": msg_id, "model": model, "usage": usage},
    })


def _result(cost: float) -> str:
    return json.dumps({"type": "result", "subtype": "success", "total_cost_usd": cost})


def test_meter_fails_open_on_every_unknowable_case(tmp_path: Path) -> None:
    """`None` means "let the leg run" and is never a stand-in for $0.

    Killing real work over a parse failure is worse than the overspend.
    """
    assert LiveCostMeter(None).read() is None
    assert LiveCostMeter(tmp_path / "does-not-exist.log").read() is None

    # An interactive (claude-pty) leg's log is not stream-json (#1710) — the
    # documented limit: this ceiling does not cap interactive sessions.
    tty = tmp_path / "interactive.log"
    tty.write_text("some tty output\nnot json at all\n", encoding="utf-8")
    assert LiveCostMeter(tty).read() is None

    # Priceable-looking but the model is unrecognized → never guessed at a
    # tier, never priced as $0.
    unknown = tmp_path / "unknown-model.log"
    unknown.write_text(
        _assistant("m1", model="some-other-vendor-model", output_tokens=100_000) + "\n",
        encoding="utf-8",
    )
    assert LiveCostMeter(unknown).read() is None


def test_meter_estimates_from_per_turn_usage_before_any_result_event(
    tmp_path: Path,
) -> None:
    """The whole reason the meter exists.

    `coord.worker_events.update_summary` only sets `total_cost_usd` from a
    `result` event, and a real transcript carries exactly one of those, at the
    very end — so `cost_so_far` is $0.00 for the entire life of a running leg
    and a ceiling keyed on it alone could never fire in time.
    """
    log = tmp_path / "running.log"
    # 1M output tokens at the opus rate ($25/1M) — no result event yet.
    log.write_text(_assistant("m1", output_tokens=1_000_000) + "\n", encoding="utf-8")
    assert LiveCostMeter(log).read() == pytest.approx(25.0)


def test_meter_dedupes_the_repeated_assistant_events_for_one_message(
    tmp_path: Path,
) -> None:
    """One assistant message → several `assistant` events (one per content
    block), each repeating the SAME usage. Counting them all ran ~45% high on
    a real transcript — i.e. in the direction that kills healthy legs."""
    log = tmp_path / "dupes.log"
    log.write_text(
        "\n".join([
            _assistant("msg_a", output_tokens=1_000_000),
            _assistant("msg_a", output_tokens=1_000_000),
            _assistant("msg_a", output_tokens=1_000_000),
        ]) + "\n",
        encoding="utf-8",
    )
    assert LiveCostMeter(log).read() == pytest.approx(25.0)


def test_meter_reads_incrementally_and_survives_a_partial_trailing_line(
    tmp_path: Path,
) -> None:
    """It is polled on the reap loop, so it must not re-parse the whole
    transcript each time — and a line the worker is mid-way through writing
    must be buffered, not dropped."""
    log = tmp_path / "growing.log"
    log.write_text(
        "# argv=claude -p\n" + _assistant("m1", output_tokens=400_000) + "\n",
        encoding="utf-8",
    )
    meter = LiveCostMeter(log)
    assert meter.read() == pytest.approx(10.0)

    # Append a half-written line: the total must not move, and the fragment
    # must not be lost.
    partial = _assistant("m2", output_tokens=400_000)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(partial[: len(partial) // 2])
    assert meter.read() == pytest.approx(10.0)

    with open(log, "a", encoding="utf-8") as fh:
        fh.write(partial[len(partial) // 2:] + "\n")
    assert meter.read() == pytest.approx(20.0)


def test_terminal_result_event_supersedes_the_estimate(tmp_path: Path) -> None:
    log = tmp_path / "finished.log"
    log.write_text(
        _assistant("m1", output_tokens=1_000_000) + "\n" + _result(3.25) + "\n",
        encoding="utf-8",
    )
    assert LiveCostMeter(log).read() == pytest.approx(3.25)


# ── the watchdog: warn, then kill, with a distinct exit code ────────────────


_NATURAL_EXIT = 7  # what a fake worker that finishes on its own returns


class _FakeProc:
    """A worker that exits on its own after ``die_after`` polls.

    Bounded on purpose: every test here drives the real ``while True`` loop,
    so a process that can never exit turns a failing assertion into a hung
    suite.
    """

    def __init__(self, *, die_after: int = 10_000, pid: int = 4321):
        self.pid = pid
        self.killed: list[int] = []
        self._polls = 0
        self._die_after = die_after
        self._dead = False

    def wait(self, timeout=None):
        if self._dead:
            return -9
        self._polls += 1
        if self._polls > self._die_after:
            self._dead = True
            return _NATURAL_EXIT
        raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout or 0)

    def poll(self):
        return -9 if self._dead else None

    def die(self):
        self._dead = True


def _drive(
    log_path: Path,
    *,
    proc: _FakeProc,
    ceiling=None,
    read_cost=None,
    has_result=lambda _p: False,
    grace_after_result: float = 30.0,
) -> tuple[int, list[int]]:
    """Run the real wait loop against *proc*, returning (exit_code, signals)."""
    killed: list[int] = []

    def killpg(pid, sig):
        killed.append(sig)
        proc.die()

    code = _wait_for_proc_or_result(
        proc, str(log_path),
        poll_interval=0.0,
        grace_after_result=grace_after_result,
        first_output_timeout=0,       # TTFT watchdog off: this is about cost
        max_wait=10_000,
        cost_ceiling_usd=ceiling,
        read_cost_usd=read_cost,
        killpg=killpg,
        log_has_result=has_result,
        log_has_output=lambda _p: True,
        clock=lambda: 0.0,
    )
    return code, killed


def _scripted(values):
    """A cost reader yielding *values*, then repeating the last one forever."""
    seq = list(values)

    def read():
        return seq.pop(0) if len(seq) > 1 else (seq[0] if seq else None)

    return read


def test_watchdog_warns_before_it_kills(tmp_path: Path) -> None:
    """A `STATUS:` warning at a fraction of the ceiling gives an observer a
    chance to intervene, and makes the kill legible after the fact."""
    log = tmp_path / "leg.log"
    log.write_text("", encoding="utf-8")
    proc = _FakeProc()
    code, killed = _drive(
        log, proc=proc, ceiling=8.0, read_cost=_scripted([7.0, 7.5, 9.0]),
    )

    assert code == SPEND_CEILING_EXIT
    assert killed  # the process group really was signalled
    text = log.read_text(encoding="utf-8")
    assert "STATUS: spend $7.00 has passed 80% of the $8.00 per-leg ceiling" in text
    assert "SIGKILL — spend ceiling breached ($9.00 of $8.00)" in text
    # The warning fires once, not on every poll.
    assert text.count("STATUS: spend") == 1


def test_watchdog_never_kills_on_an_unreadable_cost(tmp_path: Path) -> None:
    """Fail open: a leg whose cost cannot be parsed keeps running.

    The ceiling here is absurdly low, so only the `None` reading protects it.
    """
    log = tmp_path / "leg.log"
    log.write_text("", encoding="utf-8")
    proc = _FakeProc(die_after=5)
    code, killed = _drive(
        log, proc=proc, ceiling=0.01, read_cost=lambda: None,
    )
    assert code == _NATURAL_EXIT
    assert killed == []
    assert "spend ceiling" not in log.read_text(encoding="utf-8")


def test_watchdog_does_not_kill_a_leg_that_already_emitted_its_result(
    tmp_path: Path,
) -> None:
    """Once the worker is logically done the money is already spent; killing
    then would only mislabel a completed leg as a ceiling kill."""
    log = tmp_path / "leg.log"
    log.write_text("", encoding="utf-8")
    proc = _FakeProc()
    code, _ = _drive(
        log, proc=proc, ceiling=1.0,
        read_cost=lambda: 99.0,
        has_result=lambda _p: True,
        grace_after_result=0.0,
    )
    # The existing post-result grace path owns this leg, and it reports the
    # work as logically complete (0), not as a ceiling kill.
    assert code == 0
    assert "spend ceiling breached" not in log.read_text(encoding="utf-8")


def test_watchdog_is_inert_with_no_ceiling_configured(tmp_path: Path) -> None:
    """The no-config parity requirement, at the loop level: with no ceiling
    the cost reader is never even consulted."""
    log = tmp_path / "leg.log"
    log.write_text("", encoding="utf-8")
    proc = _FakeProc(die_after=3)
    reads = {"n": 0}

    def read_cost():
        reads["n"] += 1
        return 10_000.0

    code, killed = _drive(log, proc=proc, ceiling=None, read_cost=read_cost)
    assert code == _NATURAL_EXIT
    assert killed == []
    assert reads["n"] == 0


# ── black-box: drive the running agent with a stubbed log ───────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo_local_only(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    return repo


# A worker that writes one stream-json turn priced far past any sane ceiling,
# then sleeps forever — exactly the runaway shape the ceiling exists to stop.
# 4M opus output tokens ≈ $100.
_RUNAWAY_TURN = json.dumps({
    "type": "assistant",
    "message": {
        "id": "msg_runaway",
        "model": "claude-opus-5",
        "usage": {"output_tokens": 4_000_000},
    },
})


def test_running_agent_kills_and_marks_a_leg_that_crosses_the_ceiling(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """Acceptance, end to end through the real ``AgentServer``.

    The worker crosses the threshold and would otherwise run forever. It must
    come back FAILED with a reason that says *spend ceiling* — a generic
    failure here is the bug, because that is what lets `coord retry`
    cheerfully spend the money again.
    """
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            f"printf '%s\\n' '{_RUNAWAY_TURN}'; sleep 300",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2131,
        issue_title="runaway leg",
        briefing="b",
        branch="main",
        type="work",
        cost_ceiling_usd=8.0,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=60)

    assert final.status == FAILED
    assert final.exit_code == SPEND_CEILING_EXIT
    assert is_spend_ceiling_reason(final.spend_ceiling_reason), (
        f"a ceiling kill must be distinguishable from a crash — got "
        f"{final.spend_ceiling_reason!r}"
    )
    assert "of $8.00 (type=work)" in final.spend_ceiling_reason
    # The other diagnostics sharing `failure_reason` must stay clear.
    assert final.usage_limit_reason is None
    assert final.api_error_reason is None
    # And the kill is narrated in the worker's own log.
    log_text = Path(final.log_path).read_text(encoding="utf-8")
    assert "spend ceiling breached" in log_text
    # The reason rides the /status wire, which is how the coordinator ever
    # learns about it.
    entry = next(
        e for e in server.list_assignments()["completed"] if e["id"] == a.id
    )
    assert is_spend_ceiling_reason(entry["spend_ceiling_reason"])
    server.shutdown()


def test_running_agent_leaves_a_cheap_leg_completely_alone(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The 97% of legs the ceiling must never touch.

    Same ceiling, a leg whose one turn costs cents: it finishes on its own
    terms, and nothing about the ceiling appears anywhere.
    """
    cheap_turn = json.dumps({
        "type": "assistant",
        "message": {
            "id": "msg_cheap",
            "model": "claude-sonnet-4-6",
            "usage": {"output_tokens": 1000},
        },
    })
    done = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.02})
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            "printf 'work\\n' > out.txt && git add out.txt && "
            "git -c user.email=t@t.com -c user.name=T commit -q -m 'work' && "
            f"printf '%s\\n%s\\n' '{cheap_turn}' '{done}'",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2131,
        issue_title="an ordinary cheap leg",
        briefing="b",
        branch="main",
        type="work",
        cost_ceiling_usd=8.0,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=60)

    assert final.status != FAILED
    assert final.exit_code != SPEND_CEILING_EXIT
    assert final.spend_ceiling_reason is None
    assert "spend ceiling" not in Path(final.log_path).read_text(encoding="utf-8")
    server.shutdown()


def test_no_ceiling_on_the_spec_is_pre_2131_behaviour(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """An agent that receives no ceiling never samples cost at all — the
    no-config parity requirement, end to end. The same runaway turn that gets
    killed above runs untouched here."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c", f"printf '%s\\n' '{_RUNAWAY_TURN}'",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2131,
        issue_title="uncapped",
        briefing="b",
        branch="main",
        type="work",
        # cost_ceiling_usd deliberately unset.
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=60)

    assert final.exit_code != SPEND_CEILING_EXIT
    assert final.spend_ceiling_reason is None
    server.shutdown()


def test_budget_config_dataclass_default_is_no_ceiling() -> None:
    """Belt-and-braces on the dataclass itself, independent of the parser."""
    assert BudgetConfig().ceiling_for("work") is None
