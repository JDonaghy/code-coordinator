"""Tests for the reap-thread helpers added in #228.

Covers `_log_has_result` and the `_wait_for_proc_or_result` polling loop
that handles claude-cli sessions where `proc.wait()` hangs after the
worker has emitted its final result event.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Tuple

import pytest

from coord.agent import (
    HOST_SLEEP_EXIT,
    NO_FIRST_OUTPUT_EXIT,
    RUNTIME_CEILING_EXIT,
    _log_has_output,
    _log_has_result,
    _maybe_bash_wrap,
    _wait_for_proc_or_result,
    _PTY_RESULT_LINE_MARKER,
    _RESULT_LINE_MARKER,
    format_host_sleep_reason,
    format_runtime_ceiling_reason,
    is_host_sleep_reason,
    is_runtime_ceiling_reason,
)


# ── _log_has_result ──────────────────────────────────────────────────────────

def test_log_has_result_finds_marker(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text(
        '{"type":"assistant","message":{}}\n'
        '{"type":"result","subtype":"success","is_error":false}\n'
    )
    assert _log_has_result(str(log))


def test_log_has_result_false_when_marker_absent(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text(
        '{"type":"assistant","message":{}}\n'
        '{"type":"user","message":{}}\n'
    )
    assert not _log_has_result(str(log))


def test_log_has_result_false_for_missing_file(tmp_path: Path) -> None:
    assert not _log_has_result(str(tmp_path / "nonexistent"))


def test_log_has_result_skips_binary_noise_but_finds_real_result(tmp_path: Path) -> None:
    """Binary/garbage lines are skipped without crashing; a genuine top-level
    result event on its own line is still detected.

    (Replaces the old whole-file substring scan: binary noise that merely
    *contains* the marker bytes is no longer a completion — only a parseable
    top-level ``{"type":"result"}`` line is.)
    """
    log = tmp_path / "log"
    log.write_bytes(
        b'\xff\xfe binary noise containing "type":"result" but not valid json\n'
        b'{"type":"result","subtype":"success","is_error":false}\n'
    )
    assert _log_has_result(str(log))


def test_log_has_result_ignores_marker_inside_tool_result(tmp_path: Path) -> None:
    """Regression (#324/#325, 2026-06-06): a worker that *reads* a file
    containing the literal ``"type":"result"`` echoes it back inside a
    ``tool_result`` payload. That line's top-level ``type`` is ``"user"``, not
    ``"result"``, so it must NOT count as the worker's own completion — else
    the reap thread force-kills the worker mid-task and records the no-op as
    ``done`` (the green-box bug).
    """
    log = tmp_path / "log"
    tool_result_line = json.dumps({
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "content": 'coord/agent.py: _RESULT_LINE_MARKER = \'"type":"result"\'',
        }]},
    })
    log.write_text(
        '{"type":"assistant","message":{}}\n'
        + tool_result_line + "\n"
    )
    assert not _log_has_result(str(log))


def test_log_has_result_ignores_pty_marker_inside_tool_result(tmp_path: Path) -> None:
    """Same regression class for the PTY sentinel: a worker reading
    coord/providers/claude_pty.py echoes the marker string inside a
    ``tool_result``; only a *standalone* sentinel line counts, not the string
    embedded in a JSON payload.
    """
    log = tmp_path / "log"
    tool_result_line = json.dumps({
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "content": 'PTY_RESULT_MARKER = "# pty: worker exited"',
        }]},
    })
    log.write_text(tool_result_line + "\n")
    assert not _log_has_result(str(log))


def test_log_has_result_finds_pty_marker(tmp_path: Path) -> None:
    """The PTY sentinel (stamped after the PTY worker exits) is detected.

    Interactive ``claude`` never emits ``"type":"result"`` stream-json, so
    the pump thread writes ``_PTY_RESULT_LINE_MARKER`` to the log instead.
    ``_log_has_result`` must recognise it so the reap-thread grace-period
    optimisation (force-kill after logical completion) fires for PTY workers
    the same as for ``claude -p`` workers.
    """
    log = tmp_path / "log"
    log.write_bytes(b"\n" + _PTY_RESULT_LINE_MARKER + b"\n")
    assert _log_has_result(str(log))


def test_log_has_result_pty_marker_absent_returns_false(tmp_path: Path) -> None:
    """A log with only TTY bytes but no PTY sentinel is not yet complete."""
    log = tmp_path / "log"
    log.write_bytes(b"\x1b[1mclaude>\x1b[0m hello\n")
    assert not _log_has_result(str(log))


def test_pty_marker_bytes_sync_with_provider_string() -> None:
    """The byte sentinel in coord.agent must stay in sync with the provider.

    There are two independent copies of the PTY result sentinel — one as a
    bytes literal in ``coord.agent`` (so ``_log_has_result`` can do a binary
    scan) and one as a string in ``coord.providers.claude_pty`` (the source
    of truth that the PTY pump thread reads via
    ``ClaudePtyProvider.result_marker()``).  If they ever drift, the pump
    thread will stamp a marker the reap thread no longer recognises, and
    PTY workers will silently wait the full ``_REAP_MAX_WAIT`` (~2h)
    before being reaped.  This test guards against that silent regression.
    """
    from coord.providers.claude_pty import PTY_RESULT_MARKER

    assert PTY_RESULT_MARKER.encode("utf-8") == _PTY_RESULT_LINE_MARKER


# ── _wait_for_proc_or_result ─────────────────────────────────────────────────

# #2729 (review iteration 1): only tests whose kill escalation reaches the
# real `signal.SIGKILL` constant need this skip -- `coord/agent.py`'s own
# module-level `signal` import has no `SIGKILL` attribute on win32 at all
# ("Availability: Unix", per the stdlib docs), so the real (non-test)
# function raises `AttributeError` there regardless of the injected
# `killpg` callable. `signal.SIGTERM`, unlike `SIGKILL`, IS defined on
# win32 -- so a test whose escalation stops at a single SIGTERM (the fake
# proc "honors" that kill, so the retry `proc.wait()` never times out and
# the SIGKILL-escalation branch a few lines further down is never reached)
# does not need this skip and is left unmarked below to keep real Windows
# coverage on that path. This is the process-group reaper CP-7 (#1163) owns
# the Job Objects port for, per #2729/#2680's explicit bucket-b boundary:
# skip rather than shim. Tests where the watchdog/ceiling never actually
# fires (``calls == []``) never reach those lines either and need no skip.
_WIN32_REAPER_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_wait_for_proc_or_result's kill escalation reaches the real "
    "signal.SIGKILL (coord/agent.py) even with killpg() injected -- "
    "process-group reaping is POSIX-only until Job Objects (Windows-native "
    "attended sessions) land, owned by #1163, not this issue",
)


class _FakeProc:
    """Minimal Popen stand-in: exits after N wait() calls or never exits.

    Also accepts kill signals via the recorded `killpg` callable so tests can
    assert what was sent without invoking the OS.
    """

    def __init__(
        self,
        *,
        exit_after_calls: int | None = None,
        exit_code: int = 0,
        exit_after_kill: bool = True,
    ) -> None:
        self.pid = 99999
        self._wait_calls = 0
        self._exit_after_calls = exit_after_calls
        self._exit_code = exit_code
        self._exit_after_kill = exit_after_kill
        self._was_killed = False

    def wait(self, timeout: float | None = None) -> int:
        self._wait_calls += 1
        # If we've been killed and the proc honors kill, return on next wait.
        if self._was_killed and self._exit_after_kill:
            return self._exit_code
        # Otherwise return when call count is reached, else TimeoutExpired.
        if self._exit_after_calls is not None and self._wait_calls >= self._exit_after_calls:
            return self._exit_code
        raise subprocess.TimeoutExpired("fake", timeout)

    def mark_killed(self) -> None:
        self._was_killed = True


def _make_killpg_recorder() -> Tuple[Callable[[int, int], None], List[Tuple[int, int]], _FakeProc | None]:
    """Build a killpg callable that records (pid, sig) calls and, if a proc is
    set later, notifies it so its next `wait()` returns."""
    calls: List[Tuple[int, int]] = []
    proc_holder: List[_FakeProc] = []

    def record(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if proc_holder:
            proc_holder[0].mark_killed()

    def set_proc(p: _FakeProc) -> None:
        proc_holder.append(p)

    return record, calls, set_proc  # type: ignore[return-value]


def _fake_clock() -> Callable[[], float]:
    """A controllable clock — call returned ticker to advance time."""
    state = {"t": 0.0}

    def now() -> float:
        return state["t"]

    def advance(delta: float) -> None:
        state["t"] += delta

    now.advance = advance  # type: ignore[attr-defined]
    return now


def _fake_wall_clock() -> Callable[[], float]:
    """A second, independently-advanceable ticker modelling `time.time()`.

    Kept separate from `_fake_clock` (monotonic) so tests can make the two
    diverge — that divergence is the entire signal the #2638 host-sleep
    detector looks for. Starts at an arbitrary epoch-ish value purely so
    assertion output never looks like a bare small int.
    """
    state = {"t": 1_700_000_000.0}

    def now() -> float:
        return state["t"]

    def advance(delta: float) -> None:
        state["t"] += delta

    now.advance = advance  # type: ignore[attr-defined]
    return now


def test_clean_exit_returns_proc_exit_code(tmp_path: Path) -> None:
    """When proc exits normally on the first wait, return its exit code."""
    proc = _FakeProc(exit_after_calls=1, exit_code=42)
    killpg = lambda pid, sig: None  # noqa: E731 — should never fire
    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        str(tmp_path / "log"),
        poll_interval=0.01,
        grace_after_result=0.5,
        max_wait=10.0,
        killpg=killpg,
        log_has_result=lambda _: False,
    )
    assert code == 42


def test_force_kills_when_result_seen_then_proc_hangs(tmp_path: Path) -> None:
    """The fix for #228: worker emits result, proc.wait keeps timing out,
    so we SIGTERM the group and return 0.

    No `_WIN32_REAPER_SKIP` needed: the fake proc honors the single SIGTERM
    (`exit_after_kill=True`), so the retry `proc.wait()` never times out and
    the real `signal.SIGKILL` branch below it is never reached -- only
    `signal.SIGTERM`, which IS defined on win32, gets evaluated (#2729)."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=0.01,
        grace_after_result=0.0,  # immediately escalate after result seen
        max_wait=10.0,
        killpg=record,
        log_has_result=lambda _: True,  # claim result already in log
    )
    assert code == 0
    # SIGTERM must have fired on the process group.
    assert calls and calls[0] == (proc.pid, signal.SIGTERM)
    # Log should mention the escalation so an operator can diagnose.
    text = Path(log_path).read_text()
    assert "SIGTERM" in text


@_WIN32_REAPER_SKIP
def test_escalates_to_sigkill_when_sigterm_ignored(tmp_path: Path) -> None:
    """If SIGTERM doesn't bring the proc down, SIGKILL follows."""
    log_path = str(tmp_path / "log")
    # exit_after_kill=False → even after being killed, wait keeps timing out.
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=False)
    calls: List[Tuple[int, int]] = []
    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=0.01,
        grace_after_result=0.0,
        max_wait=10.0,
        killpg=lambda pid, sig: calls.append((pid, sig)),
        log_has_result=lambda _: True,
    )
    assert code == 0  # logical completion → success
    sigs = [s for _, s in calls]
    assert signal.SIGTERM in sigs
    assert signal.SIGKILL in sigs


@_WIN32_REAPER_SKIP
def test_max_wait_safety_net_sigkills_and_marks_failed(tmp_path: Path) -> None:
    """If the worker never emits a result, max_wait elapses and we SIGKILL."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=-9)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    clock = _fake_clock()

    # Make poll_interval advance the clock so max_wait can be reached.
    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        clock.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=2.0,  # → 3rd iteration trips the safety net
        killpg=record,
        log_has_result=lambda _: False,  # never emits result
        clock=clock,
    )
    assert code == 137  # SIGKILL convention
    assert (proc.pid, signal.SIGKILL) in calls
    assert "max-wait" in Path(log_path).read_text()


def test_result_detected_mid_wait_only_kills_after_grace(tmp_path: Path) -> None:
    """If the worker emits the result mid-wait but might still exit cleanly,
    we wait the grace period before escalating.

    No `_WIN32_REAPER_SKIP` needed: `exit_after_kill=True` on the fake proc
    means the single SIGTERM is honored and `signal.SIGKILL` is never
    reached -- see the comment on `_WIN32_REAPER_SKIP` above (#2729)."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    clock = _fake_clock()

    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        clock.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    # Trigger log_has_result True only after the 2nd iteration.
    state = {"calls": 0}

    def has_result(_path: str) -> bool:
        state["calls"] += 1
        return state["calls"] >= 2

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=0.5,
        grace_after_result=1.0,  # need 1s elapsed after result seen
        max_wait=10.0,
        killpg=record,
        log_has_result=has_result,
        clock=clock,
    )
    assert code == 0
    # The SIGTERM should not have fired on the first iteration where result
    # wasn't yet detected.
    assert calls, "expected SIGTERM to fire eventually"


# ── _log_has_output ──────────────────────────────────────────────────────────

def test_log_has_output_false_for_header_only(tmp_path: Path) -> None:
    """A log containing only the spawn header (# comments) has no output yet."""
    log = tmp_path / "log"
    log.write_text(
        "# agent=m repo=r issue=#1 argv=claude -p\n"
        "# all pulls succeeded; starting worker\n"
    )
    assert not _log_has_output(str(log))


def test_log_has_output_true_for_stream_json_line(tmp_path: Path) -> None:
    """A worker stream-json line counts as output."""
    log = tmp_path / "log"
    log.write_text(
        "# agent=m repo=r issue=#1 argv=claude -p\n"
        '{"type":"assistant","message":{}}\n'
    )
    assert _log_has_output(str(log))


def test_log_has_output_false_for_missing_file(tmp_path: Path) -> None:
    assert not _log_has_output(str(tmp_path / "nonexistent"))


def test_log_has_output_false_for_blank_lines(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text("# header\n\n   \n")
    assert not _log_has_output(str(log))


# ── first-output (TTFT) watchdog ─────────────────────────────────────────────

def _clock_advancing_proc(proc: _FakeProc, clock) -> None:
    """Patch proc.wait so each poll advances the fake clock by `timeout`."""
    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        clock.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]


@_WIN32_REAPER_SKIP
def test_watchdog_fires_when_no_first_output(tmp_path: Path) -> None:
    """No output within first_output_timeout → killpg once + NO_FIRST_OUTPUT_EXIT."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    clock = _fake_clock()
    _clock_advancing_proc(proc, clock)

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=10_000.0,  # well past the watchdog so the watchdog is what fires
        first_output_timeout=2.0,  # → 3rd iteration trips the watchdog
        killpg=record,
        log_has_result=lambda _: False,
        log_has_output=lambda _: False,  # truly silent worker
        clock=clock,
    )
    assert code == NO_FIRST_OUTPUT_EXIT
    # Exactly one kill (SIGKILL) on the process group.
    assert calls == [(proc.pid, signal.SIGKILL)]
    text = Path(log_path).read_text()
    assert "no first output" in text


def test_watchdog_does_not_fire_when_output_appears(tmp_path: Path) -> None:
    """A worker that emits output before the timeout is never killed by the
    watchdog — even if it keeps running a while (rate-limited-but-emitting)."""
    log_path = str(tmp_path / "log")
    # Worker exits cleanly later, after the watchdog window would have passed.
    proc = _FakeProc(exit_after_calls=5, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    clock = _fake_clock()
    _clock_advancing_proc(proc, clock)

    # Output is visible from the very first poll, satisfying the watchdog
    # permanently; the proc then keeps running past first_output_timeout.
    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=10_000.0,
        first_output_timeout=2.0,
        killpg=record,
        log_has_result=lambda _: False,
        log_has_output=lambda _: True,  # rate-limited worker DOES emit output
        clock=clock,
    )
    assert code == 0  # clean exit
    assert calls == []  # watchdog never killed anything


def test_watchdog_disabled_when_timeout_zero(tmp_path: Path) -> None:
    """first_output_timeout=0 disables the watchdog even with zero output."""
    log_path = str(tmp_path / "log")
    # Worker exits cleanly after a few polls; without the watchdog disabled a
    # silent worker would otherwise be killed.
    proc = _FakeProc(exit_after_calls=3, exit_after_kill=True, exit_code=7)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    clock = _fake_clock()
    _clock_advancing_proc(proc, clock)

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=10_000.0,
        first_output_timeout=0.0,  # disabled
        killpg=record,
        log_has_result=lambda _: False,
        log_has_output=lambda _: False,  # silent, but watchdog is off
        clock=clock,
    )
    assert code == 7  # proc's own exit code — watchdog never intervened
    assert calls == []


def test_ttft_watchdog_unaffected_by_the_new_wall_clock_params(tmp_path: Path) -> None:
    """#2638 regression: the wall-clock ceiling and host-sleep checks now run
    on every poll too, but they must not change the TTFT watchdog's existing
    behaviour — a rate-limited-but-emitting worker is still never killed by
    it (#299), even with both new checks fully armed and driven by clocks
    that never diverge (a normal, awake box)."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=5, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    mono = _fake_clock()
    wall = _fake_wall_clock()
    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        mono.advance(timeout or 0.0)  # type: ignore[attr-defined]
        wall.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=10_000.0,
        first_output_timeout=2.0,
        runtime_ceiling_s=10_000.0,
        sleep_divergence_s=60.0,
        killpg=record,
        log_has_result=lambda _: False,
        log_has_output=lambda _: True,  # emits output from the very first poll
        clock=mono,
        wall_clock=wall,
    )
    assert code == 0  # clean exit — nothing killed it
    assert calls == []


# ── wall-clock runtime ceiling + host-sleep detection (#2638) ───────────────

@_WIN32_REAPER_SKIP
def test_runtime_ceiling_kills_a_leg_that_outlives_it(tmp_path: Path) -> None:
    """A leg whose wall-clock runtime exceeds the ceiling is killed and
    returns RUNTIME_CEILING_EXIT, stamped with a distinguishing log line —
    even though nothing else (TTFT, result, max_wait) ever fires. A leg
    under the ceiling is left alone (see
    `test_runtime_ceiling_disabled_behaves_as_pre_2638` for the no-kill
    shape with the ceiling off; here the proc simply never gets the chance
    to run long enough for the *disabled* case, so it is asserted directly
    by the wall-clock elapsed check below instead)."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    mono = _fake_clock()
    wall = _fake_wall_clock()
    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        # A genuinely long-running, AWAKE leg: monotonic and wall-clock
        # advance together, at the same rate.
        mono.advance(timeout or 0.0)  # type: ignore[attr-defined]
        wall.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=10_000.0,
        first_output_timeout=10_000.0,
        runtime_ceiling_s=3.0,  # → 4th poll (elapsed=4.0) trips the ceiling
        sleep_divergence_s=60.0,
        killpg=record,
        log_has_result=lambda _: False,
        log_has_output=lambda _: True,
        clock=mono,
        wall_clock=wall,
    )
    assert code == RUNTIME_CEILING_EXIT
    assert calls == [(proc.pid, signal.SIGKILL)]
    text = Path(log_path).read_text()
    assert "runtime ceiling" in text


@_WIN32_REAPER_SKIP
def test_runtime_ceiling_measured_on_wall_clock_not_monotonic(tmp_path: Path) -> None:
    """#2638: the ceiling MUST be measured on wall-clock elapsed, not
    monotonic elapsed. A leg whose monotonic clock barely advances (the
    signature of a host that suspended and froze `CLOCK_MONOTONIC`) but
    whose wall clock keeps ticking normally still gets killed.

    A test that only advanced a fake monotonic clock would pass against a
    buggy monotonic-only implementation — this is the assertion that
    matters: monotonic elapsed stays under a second the whole test, while
    wall-clock elapsed alone crosses the ceiling.
    """
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    mono = _fake_clock()
    wall = _fake_wall_clock()
    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        # Monotonic virtually frozen (well under the sleep-divergence
        # threshold each poll); wall clock advances a full poll_interval,
        # same as it would on an awake, running box.
        mono.advance(0.001)  # type: ignore[attr-defined]
        wall.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=10_000.0,
        first_output_timeout=10_000.0,
        runtime_ceiling_s=5.0,  # → 6th poll (wall elapsed=6.0) trips it
        sleep_divergence_s=60.0,  # a ~1s/poll gap must NOT read as a sleep
        killpg=record,
        log_has_result=lambda _: False,
        log_has_output=lambda _: True,
        clock=mono,
        wall_clock=wall,
    )
    assert code == RUNTIME_CEILING_EXIT
    assert calls == [(proc.pid, signal.SIGKILL)]
    assert mono() < 1.0  # monotonic elapsed stayed tiny the whole time


@_WIN32_REAPER_SKIP
def test_host_sleep_detected_via_wall_monotonic_divergence(tmp_path: Path) -> None:
    """#2638: a suspend/resume between two polls — monotonic freezes
    (mirrors `CLOCK_MONOTONIC` not advancing across s2idle) while the wall
    clock jumps forward by the whole suspend duration. Detected immediately,
    distinctly from (and well before) the runtime ceiling, which is set
    generously high here and never reached — this incident's own 10.5h
    suspend duration is used verbatim.
    """
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    mono = _fake_clock()
    wall = _fake_wall_clock()
    real_wait = proc.wait
    state = {"calls": 0}

    def wait_advances(timeout: float | None = None) -> int:
        state["calls"] += 1
        mono.advance(timeout or 0.0)  # type: ignore[attr-defined]
        if state["calls"] == 2:
            # The host suspends for ten and a half hours between polls.
            wall.advance(10.5 * 3600.0)  # type: ignore[attr-defined]
        else:
            wall.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=10_000.0,
        first_output_timeout=10_000.0,
        runtime_ceiling_s=6 * 3600.0,  # generous — must NOT be what fires
        sleep_divergence_s=60.0,
        killpg=record,
        log_has_result=lambda _: False,
        log_has_output=lambda _: True,
        clock=mono,
        wall_clock=wall,
    )
    assert code == HOST_SLEEP_EXIT
    assert calls == [(proc.pid, signal.SIGKILL)]
    text = Path(log_path).read_text()
    assert "host sleep detected" in text


def test_host_sleep_check_gated_on_result_seen_at_is_none(tmp_path: Path) -> None:
    """Review finding (non-blocking, #2638 iteration 1): the host-sleep check
    must be gated on `result_seen_at is None`, exactly like the runtime
    ceiling and #2131's spend ceiling right below it — otherwise a
    suspend/resume that straddles the short post-result grace-period
    teardown would kill and mislabel an already-finished leg as a host-sleep
    kill instead of letting it land DONE via the ordinary grace-period path.

    No `_WIN32_REAPER_SKIP` needed: the grace-period teardown that actually
    fires here escalates only to the single SIGTERM (`exit_after_kill=True`
    on the fake proc), never to `signal.SIGKILL` — see the comment on
    `_WIN32_REAPER_SKIP` above (#2729).

    Sequence: the worker's result is seen on the FIRST poll (so
    `result_seen_at` is set before any divergence is measured); the SECOND
    poll then sees a huge wall-vs-monotonic divergence — well past
    `sleep_divergence_s` — but must be ignored because the leg is already
    logically finished. The pre-existing grace-period teardown must be what
    actually returns 0, not the host-sleep watchdog.
    """
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    mono = _fake_clock()
    wall = _fake_wall_clock()
    real_wait = proc.wait
    state = {"calls": 0}

    def wait_advances(timeout: float | None = None) -> int:
        state["calls"] += 1
        mono.advance(timeout or 0.0)  # type: ignore[attr-defined]
        if state["calls"] == 2:
            # The host suspends for ten hours AFTER the worker's result was
            # already observed — e.g. straddling the post-result teardown.
            wall.advance(10.0 * 3600.0)  # type: ignore[attr-defined]
        else:
            wall.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,  # short grace so poll 2 escalates via SIGTERM
        max_wait=10_000.0,
        first_output_timeout=10_000.0,
        runtime_ceiling_s=6 * 3600.0,
        sleep_divergence_s=60.0,
        killpg=record,
        log_has_result=lambda _: True,  # result already seen on the 1st poll
        log_has_output=lambda _: True,
        clock=mono,
        wall_clock=wall,
    )
    # Grace-period teardown owns the outcome (SIGTERM, exit 0) — the
    # host-sleep watchdog must NOT have fired despite the huge divergence.
    assert code == 0
    assert calls
    assert calls[0][1] == signal.SIGTERM
    text = Path(log_path).read_text()
    assert "host sleep detected" not in text


def test_runtime_ceiling_disabled_behaves_as_pre_2638(tmp_path: Path) -> None:
    """runtime_ceiling_s=None disables the wall-clock ceiling entirely — a
    leg with no ceiling configured behaves exactly as it did before #2638:
    the proc simply runs to its own natural exit, untouched by this
    watchdog (only the pre-existing TTFT/max_wait/result-based checks would
    still apply, and none of them fire here either)."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=5, exit_after_kill=True, exit_code=3)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    mono = _fake_clock()
    wall = _fake_wall_clock()
    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        mono.advance(timeout or 0.0)  # type: ignore[attr-defined]
        wall.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=10_000.0,
        first_output_timeout=10_000.0,
        runtime_ceiling_s=None,  # disabled
        sleep_divergence_s=60.0,
        killpg=record,
        log_has_result=lambda _: False,
        log_has_output=lambda _: True,
        clock=mono,
        wall_clock=wall,
    )
    assert code == 3  # proc's own exit code — the ceiling never intervened
    assert calls == []


def test_runtime_ceiling_reason_round_trips_and_does_not_match_other_failures() -> None:
    reason = format_runtime_ceiling_reason(6.5 * 3600.0, 6.0 * 3600.0)
    assert is_runtime_ceiling_reason(reason)
    assert "runtime ceiling" in reason
    assert not is_runtime_ceiling_reason("spend ceiling — $12.41 of $8.00")
    assert not is_runtime_ceiling_reason("usage limit — resets 8:30pm")
    assert not is_runtime_ceiling_reason(None)
    assert not is_runtime_ceiling_reason("")


def test_host_sleep_reason_round_trips_and_does_not_match_other_failures() -> None:
    reason = format_host_sleep_reason(37_800.0, 5.0)
    assert is_host_sleep_reason(reason)
    assert "host sleep detected" in reason
    assert not is_host_sleep_reason(
        format_runtime_ceiling_reason(6.5 * 3600.0, 6.0 * 3600.0)
    )
    assert not is_host_sleep_reason(None)
    assert not is_host_sleep_reason("")


# ── _maybe_bash_wrap ─────────────────────────────────────────────────────────

def test_maybe_bash_wrap_enabled_wraps_with_exec() -> None:
    argv = ["claude", "-p", "--system-prompt", "be nice", "--allowedTools", "Read,Bash"]
    wrapped = _maybe_bash_wrap(argv, enabled=True)
    assert wrapped[0] == "bash"
    assert wrapped[1] == "-c"
    assert wrapped[2].startswith("exec ")
    # The joined command must round-trip the original argv via shlex.
    import shlex
    assert shlex.split(wrapped[2])[0] == "exec"
    assert shlex.split(wrapped[2])[1:] == argv


def test_maybe_bash_wrap_disabled_returns_bare_argv() -> None:
    argv = ["claude", "-p"]
    assert _maybe_bash_wrap(argv, enabled=False) == argv
