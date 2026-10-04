"""Tests for coord/tui_pty_driver.py — the ``tui-pty`` acceptance driver's
real engine (#3483): the smoke-spec YAML parser, raw-terminal-byte
encoding, VT screen assertions (via `pyte`), and the step executor
(:class:`~coord.tui_pty_driver.SmokeRunner`) driven against a scripted fake
child process rather than a real pty/ConPTY — per the issue's own
acceptance bar ("unit tests cover the spec parser, the step executor
against a fake child process, and the VT screen assertions"). A real
pty/ConPTY run against a real app is out of reach for this repo's test
suite (no target binary checked in here) and is exercised at the operator
level instead — see this issue's PR description.

:class:`TestUnixPtyChildReal` is the one exception: a small, fast, real
Unix-pty-backed test (skipped off POSIX) that proves
:class:`~coord.tui_pty_driver.UnixPtyChild` itself genuinely drives a real
child process through a real pseudo-terminal, not just against the fake.

:class:`TestUnixPtyChildSurvivesAbnormalParentDeath` is a second, deliberate
exception (#3583): it SIGKILLs the *managing* process (a throwaway
``python -c`` subprocess that constructs a real ``UnixPtyChild`` and never
calls ``close()``) and asserts the real grandchild is reaped anyway — the
exact failure mode the companion bugbash finding reported (51 orphaned
``vcd`` processes reparented to ``systemd --user``). Against the pre-#3583
code (``preexec_fn=os.setsid`` with no parent-death signal) this test fails:
the grandchild survives its manager's SIGKILL indefinitely.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from coord.tui_pty_driver import (
    SmokeRunner,
    SmokeSpec,
    SmokeStep,
    TuiPtyRuntimeError,
    TuiPtySpecError,
    UnixPtyChild,
    VtScreen,
    encode_click,
    encode_key,
    parse_smoke_spec,
    run_smoke_spec,
)

# ── parse_smoke_spec ────────────────────────────────────────────────────────


class TestParseSmokeSpec:
    VALID_YAML = """
name: vcd smoke
cols: 100
rows: 30
steps:
  - type: launch
  - type: click
    row: 2
    col: 5
    button: right
  - type: wait_idle
    ms: 100
    timeout_ms: 2000
  - type: expect_screen
    id: explorer-opens
    text: "EXPLORER"
  - type: expect_silent
    id: idle-flicker-1634
    seconds: 2.0
  - type: expect_within
    id: right-click-menu-1635
    ms: 500
    text: "Cut"
"""

    def test_parses_all_step_types_and_top_level_fields(self) -> None:
        spec = parse_smoke_spec(self.VALID_YAML)
        assert spec.name == "vcd smoke"
        assert spec.cols == 100
        assert spec.rows == 30
        assert [s.kind for s in spec.steps] == [
            "launch", "click", "wait_idle", "expect_screen", "expect_silent",
            "expect_within",
        ]

    def test_click_fields_parsed(self) -> None:
        spec = parse_smoke_spec(self.VALID_YAML)
        click = spec.steps[1]
        assert (click.row, click.col, click.button) == (2, 5, "right")

    def test_named_id_used_verbatim(self) -> None:
        spec = parse_smoke_spec(self.VALID_YAML)
        silent = next(s for s in spec.steps if s.kind == "expect_silent")
        assert silent.step_id == "idle-flicker-1634"
        assert silent.seconds == 2.0

    def test_unnamed_step_gets_positional_id(self) -> None:
        spec = parse_smoke_spec(self.VALID_YAML)
        launch = spec.steps[0]
        assert launch.step_id == "000 launch"

    def test_defaults_when_top_level_fields_omitted(self) -> None:
        spec = parse_smoke_spec("steps:\n  - type: launch\n")
        assert spec.name == ""
        assert spec.cols == 80
        assert spec.rows == 24

    def test_explicit_zero_ms_is_honored_not_treated_as_absent(self) -> None:
        # `int(entry.get("ms", 200) or 200)` would silently turn an
        # explicit `ms: 0` into the 200 default — it must be honored as
        # the literal value the spec author wrote.
        spec = parse_smoke_spec(
            "steps:\n  - type: wait_idle\n    ms: 0\n    timeout_ms: 0\n"
        )
        step = spec.steps[0]
        assert step.ms == 0
        assert step.timeout_ms == 0

    def test_explicit_zero_seconds_is_honored_not_treated_as_absent(self) -> None:
        spec = parse_smoke_spec(
            "steps:\n  - type: expect_silent\n    seconds: 0\n"
        )
        assert spec.steps[0].seconds == 0.0

    def test_invalid_yaml_raises_spec_error(self) -> None:
        with pytest.raises(TuiPtySpecError, match="not valid YAML"):
            parse_smoke_spec("steps: [")

    def test_non_mapping_top_level_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="mapping"):
            parse_smoke_spec("- just\n- a\n- list\n")

    def test_missing_steps_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="steps"):
            parse_smoke_spec("name: nothing here\n")

    def test_empty_steps_list_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="steps"):
            parse_smoke_spec("steps: []\n")

    def test_step_not_a_mapping_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="steps\\[0\\]"):
            parse_smoke_spec("steps:\n  - just a string\n")

    def test_unknown_step_type_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="unknown step type"):
            parse_smoke_spec("steps:\n  - type: teleport\n")

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="key"):
            parse_smoke_spec("steps:\n  - type: key\n")

    def test_click_missing_row_col_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="row"):
            parse_smoke_spec("steps:\n  - type: click\n")

    def test_expect_within_missing_text_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="text"):
            parse_smoke_spec("steps:\n  - type: expect_within\n    ms: 500\n")

    def test_unrecognized_button_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="button"):
            parse_smoke_spec(
                "steps:\n  - type: click\n    row: 0\n    col: 0\n    button: super-click\n"
            )

    def test_region_must_be_a_mapping(self) -> None:
        with pytest.raises(TuiPtySpecError, match="region"):
            parse_smoke_spec(
                "steps:\n  - type: expect_screen\n    region: not-a-mapping\n"
            )

    def test_attr_must_be_a_mapping(self) -> None:
        with pytest.raises(TuiPtySpecError, match="attr"):
            parse_smoke_spec(
                "steps:\n  - type: expect_screen\n    attr: not-a-mapping\n"
            )

    def test_expect_screen_with_neither_text_nor_attr_raises(self) -> None:
        # (#2096) A step with nothing to assert against would always pass
        # regardless of what's on screen — its failing verdict must be
        # unreachable at parse time, not just at runtime.
        with pytest.raises(TuiPtySpecError, match="text.*and/or.*attr"):
            parse_smoke_spec("steps:\n  - type: expect_screen\n    id: foo\n")

    def test_expect_screen_with_only_region_and_no_text_or_attr_raises(self) -> None:
        # A `region:` alone narrows *where* a comparison would look but
        # still supplies nothing to compare against — same always-passes
        # trap as omitting region entirely.
        with pytest.raises(TuiPtySpecError, match="text.*and/or.*attr"):
            parse_smoke_spec(
                "steps:\n  - type: expect_screen\n"
                "    region: {row: 0, col: 0, height: 1, width: 5}\n"
            )

    def test_expect_screen_attr_with_only_row_and_col_raises(self) -> None:
        # (#2096) `row:`/`col:` only *address* a cell — the step body skips
        # them when comparing, so an `attr:` holding nothing else is
        # non-empty (passes a plain truthiness check) yet leaves the
        # comparison loop with nothing to iterate: the step would always
        # pass regardless of what's on screen.
        with pytest.raises(TuiPtySpecError, match="no cell attribute"):
            parse_smoke_spec(
                "steps:\n  - type: expect_screen\n    id: foo\n"
                "    attr: {row: 3, col: 5}\n"
            )

    def test_expect_screen_attr_with_only_row_raises(self) -> None:
        # Same hole reached with a single addressing key rather than both.
        with pytest.raises(TuiPtySpecError, match="no cell attribute"):
            parse_smoke_spec(
                "steps:\n  - type: expect_screen\n    attr: {row: 3}\n"
            )

    def test_expect_screen_empty_attr_mapping_raises(self) -> None:
        # `attr: {}` is falsy, so it must still be caught (by the
        # neither-text-nor-attr arm) rather than slipping through.
        with pytest.raises(TuiPtySpecError, match="attr"):
            parse_smoke_spec(
                "steps:\n  - type: expect_screen\n    attr: {}\n"
            )

    def test_expect_screen_attr_without_attribute_key_raises_even_with_text(
        self,
    ) -> None:
        # The `text:` half is falsifiable, but the `attr:` half silently
        # asserts nothing — an author who wrote it meant to check something,
        # so reject rather than quietly dropping half the step.
        with pytest.raises(TuiPtySpecError, match="no cell attribute"):
            parse_smoke_spec(
                "steps:\n  - type: expect_screen\n    text: hello\n"
                "    attr: {row: 0, col: 0}\n"
            )

    def test_expect_screen_attr_with_a_real_attribute_key_parses(self) -> None:
        # The positive control: addressing keys *plus* something to compare
        # is the intended form and must keep parsing.
        spec = parse_smoke_spec(
            "steps:\n  - type: expect_screen\n    id: bold-check\n"
            "    attr: {row: 3, col: 5, bold: true}\n"
        )
        assert spec.steps[0].attr == {"row": 3, "col": 5, "bold": True}


# ── raw terminal-byte encoding ──────────────────────────────────────────────


class TestEncodeKey:
    def test_named_keys(self) -> None:
        assert encode_key("enter") == b"\r"
        assert encode_key("esc") == b"\x1b"
        assert encode_key("tab") == b"\t"
        assert encode_key("up") == b"\x1b[A"
        assert encode_key("f5") == b"\x1b[15~"

    def test_case_insensitive(self) -> None:
        assert encode_key("ENTER") == b"\r"
        assert encode_key("Esc") == b"\x1b"

    def test_ctrl_combo(self) -> None:
        assert encode_key("ctrl+c") == b"\x03"
        assert encode_key("ctrl+a") == b"\x01"

    def test_literal_character(self) -> None:
        assert encode_key("a") == b"a"
        assert encode_key("Q") == b"Q"

    def test_unrecognized_key_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="unrecognized key"):
            encode_key("moonwalk")


class TestEncodeClick:
    def test_left_click_is_press_and_release(self) -> None:
        # button 0 = left; SGR coords are 1-indexed, hence row/col + 1.
        assert encode_click(0, 0, "left") == b"\x1b[<0;1;1M\x1b[<0;1;1m"

    def test_right_click_uses_button_code_2(self) -> None:
        data = encode_click(2, 5, "right")
        assert data == b"\x1b[<2;6;3M\x1b[<2;6;3m"

    def test_wheel_is_press_only_no_release(self) -> None:
        # A real scroll wheel reports a single notch, never a "release".
        data = encode_click(1, 1, "wheel-up")
        assert data == b"\x1b[<64;2;2M"
        assert b"m" not in data  # no lowercase release suffix

    def test_unrecognized_button_raises(self) -> None:
        with pytest.raises(TuiPtySpecError, match="unrecognized mouse button"):
            encode_click(0, 0, "super-click")


# ── VtScreen ─────────────────────────────────────────────────────────────────


class TestVtScreen:
    def test_feed_plain_text_and_read_it_back(self) -> None:
        screen = VtScreen(cols=20, rows=5)
        screen.feed(b"HELLO WORLD\r\n")
        assert "HELLO WORLD" in screen.text()

    def test_region_extracts_a_sub_rectangle(self) -> None:
        screen = VtScreen(cols=20, rows=5)
        screen.feed(b"AAAAAAAAAA\r\nBBBBBBBBBB\r\n")
        region_text = screen.text({"row": 1, "col": 0, "width": 4, "height": 1})
        assert region_text == "BBBB"

    def test_sgr_bold_sets_cell_attribute(self) -> None:
        screen = VtScreen(cols=20, rows=5)
        # \x1b[1m turns on bold; \x1b[0m resets it. Write "X" bold at (0,0),
        # then "Y" plain right after — attr must differ per cell, not leak.
        screen.feed(b"\x1b[1mX\x1b[0mY")
        assert screen.cell_attr(0, 0, "bold") is True
        assert screen.cell_attr(0, 1, "bold") is False

    def test_cursor_position_tracks_writes(self) -> None:
        screen = VtScreen(cols=20, rows=5)
        screen.feed(b"HI")
        assert screen.cursor() == (0, 2)

    def test_missing_pyte_raises_actionable_error(self, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pyte":
                raise ModuleNotFoundError("no module named pyte")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(TuiPtyRuntimeError, match="tui-pty.*extra"):
            VtScreen(cols=10, rows=5)


# ── a scripted fake child, for exercising SmokeRunner deterministically ────


class FakePtyChild:
    """A scripted :class:`PtyChild` (#3483): ``script`` is a list of
    ``(delay_seconds_since_launch, bytes)`` tuples, delivered in order once
    their delay has elapsed — real enough timing behavior to exercise
    ``wait_idle``/``expect_silent``/``expect_within`` without a real
    pty/ConPTY. Every ``write()`` call is recorded in ``.writes`` so tests
    can assert on exactly what :class:`SmokeRunner` sent (key/mouse
    encoding, the CPR auto-reply).
    """

    def __init__(self, script: list[tuple[float, bytes]] | None = None, dies_immediately: bool = False) -> None:
        self._script = list(script or [])
        self._start = time.monotonic()
        self._alive = not dies_immediately
        self.writes: list[bytes] = []
        self._closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def read(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            if self._script and time.monotonic() >= self._start + self._script[0][0]:
                _, data = self._script.pop(0)
                return data
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.005)

    def is_alive(self) -> bool:
        return self._alive

    def close(self) -> None:
        self._closed = True
        self._alive = False


def _spec(steps: list[SmokeStep], cols: int = 80, rows: int = 24) -> SmokeSpec:
    return SmokeSpec(name="test", cols=cols, rows=rows, steps=tuple(steps))


def _step(kind: str, index: int, **kwargs) -> SmokeStep:
    return SmokeStep(kind=kind, index=index, **kwargs)


class TestSmokeRunnerLaunch:
    def test_launch_failure_reports_a_failing_entry(self) -> None:
        child = FakePtyChild(dies_immediately=True)
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([_step("launch", 0)]))
        assert results == [{
            "id": "000 launch", "status": "fail",
            "message": (
                "child process exited immediately after launch (command "
                "failed to start?)"
            ),
        }]

    def test_launch_success_reports_a_passing_entry(self) -> None:
        child = FakePtyChild()
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([_step("launch", 0)]))
        assert results == [{"id": "000 launch", "status": "pass", "message": ""}]

    def test_step_before_launch_fails_with_a_clear_message(self) -> None:
        runner = SmokeRunner(lambda cols, rows: FakePtyChild())
        results = runner.run(_spec([_step("key", 0, key="a")]))
        assert results[0]["status"] == "fail"
        assert "no child process" in results[0]["message"]


class TestSmokeRunnerActions:
    def test_key_step_writes_encoded_bytes(self) -> None:
        child = FakePtyChild()
        runner = SmokeRunner(lambda cols, rows: child)
        runner.run(_spec([_step("launch", 0), _step("key", 1, key="enter")]))
        assert b"\r" in child.writes

    def test_click_step_writes_sgr_sequence(self) -> None:
        child = FakePtyChild()
        runner = SmokeRunner(lambda cols, rows: child)
        runner.run(_spec([
            _step("launch", 0),
            _step("click", 1, row=3, col=7, button="right"),
        ]))
        assert encode_click(3, 7, "right") in child.writes


class TestSmokeRunnerWaitIdle:
    def test_succeeds_once_stream_goes_quiet(self) -> None:
        child = FakePtyChild(script=[(0.0, b"draw")])
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("wait_idle", 1, ms=80, timeout_ms=2000),
        ]))
        assert results[1] == {"id": "001 wait_idle", "status": "pass", "message": ""}

    def test_a_gate_that_never_goes_idle_can_fail(self) -> None:
        # Continuous output every 20ms — never a clean 60ms idle window —
        # must time out, not hang or pass. This is the "a gate must be able
        # to fail" check for wait_idle.
        script = [(i * 0.02, b"x") for i in range(1, 20)]
        child = FakePtyChild(script=script)
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("wait_idle", 1, ms=60, timeout_ms=200),
        ]))
        assert results[1]["status"] == "fail"
        assert "never went idle" in results[1]["message"]


class TestSmokeRunnerExpectScreen:
    def test_text_found_passes(self) -> None:
        child = FakePtyChild(script=[(0.0, b"HELLO WORLD")])
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("wait_idle", 1, ms=30, timeout_ms=1000),
            _step("expect_screen", 2, id="greeting", text="HELLO"),
        ]))
        assert results[2] == {"id": "greeting", "status": "pass", "message": ""}

    def test_text_absent_fails_with_screen_contents_in_message(self) -> None:
        child = FakePtyChild(script=[(0.0, b"GOODBYE")])
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("wait_idle", 1, ms=30, timeout_ms=1000),
            _step("expect_screen", 2, id="greeting", text="HELLO"),
        ]))
        assert results[2]["status"] == "fail"
        assert "HELLO" in results[2]["message"]
        assert "GOODBYE" in results[2]["message"]

    def test_attr_mismatch_fails(self) -> None:
        child = FakePtyChild(script=[(0.0, b"plain text, no bold")])
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("wait_idle", 1, ms=30, timeout_ms=1000),
            _step("expect_screen", 2, id="bold-check", attr={"row": 0, "col": 0, "bold": True}),
        ]))
        assert results[2]["status"] == "fail"
        assert "bold" in results[2]["message"]


class TestSmokeRunnerExpectSilent:
    """vimcode#1634's regression shape: a real terminal that's actually
    idle produces no output; a ~1 Hz flicker does. Both directions must be
    provable — a pass that can never fail is not a gate (#2096)."""

    def test_passes_when_stream_is_actually_silent(self) -> None:
        child = FakePtyChild()  # no scripted output at all
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_silent", 1, id="idle-check", seconds=0.15),
        ]))
        assert results[1] == {"id": "idle-check", "status": "pass", "message": ""}

    def test_fails_when_the_stream_flickers_during_the_window(self) -> None:
        # A byte lands mid-window — the observation must be taken AFTER the
        # full window elapses and re-read the counter, not merely "no
        # exception was raised while sleeping" (#2096). The scripted delay
        # is relative to child creation, and `_do_launch` itself blocks for
        # a ~0.2s startup grace period before the `expect_silent` step ever
        # starts — schedule the flicker comfortably after that so it lands
        # inside this step's own window, not the grace period.
        child = FakePtyChild(script=[(0.3, b"repaint")])
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_silent", 1, id="idle-flicker-1634", seconds=0.3),
        ]))
        assert results[1]["status"] == "fail"
        assert "idle-flicker-1634" == results[1]["id"]
        assert "byte(s) of output" in results[1]["message"]


class TestSmokeRunnerExpectWithin:
    def test_passes_when_text_appears_before_the_deadline(self) -> None:
        child = FakePtyChild(script=[(0.05, b"Cut / Copy / Paste")])
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_within", 1, id="menu-1635", ms=500, text="Cut"),
        ]))
        assert results[1] == {"id": "menu-1635", "status": "pass", "message": ""}

    def test_a_gate_that_never_appears_can_fail(self) -> None:
        child = FakePtyChild()  # never emits the expected text at all
        runner = SmokeRunner(lambda cols, rows: child)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_within", 1, id="menu-1635", ms=100, text="Cut"),
        ]))
        assert results[1]["status"] == "fail"
        assert "menu-1635" == results[1]["id"]
        assert "within 100ms" in results[1]["message"]


class TestSmokeRunnerCprAutoReply:
    """ratatui's `Terminal::new()` blocks on a cursor-position reply to the
    `ESC[6n` it sends at startup — the app never paints without one. The
    reply must come from a thread OTHER than the byte-stream reader (see
    the module docstring — replying synchronously from inside the reader
    deadlocks a real Windows ConPTY session)."""

    def test_cpr_query_gets_a_reply_from_the_responder_thread(self) -> None:
        child = FakePtyChild(script=[(0.0, b"\x1b[6n")])
        runner = SmokeRunner(lambda cols, rows: child)
        runner.run(_spec([
            _step("launch", 0),
            _step("wait_idle", 1, ms=50, timeout_ms=1000),
        ]))
        replies = [w for w in child.writes if w.startswith(b"\x1b[") and w.endswith(b"R")]
        assert len(replies) == 1
        assert replies[0] == b"\x1b[1;1R"


class TestSmokeRunnerDeadline:
    def test_overall_timeout_aborts_remaining_steps(self) -> None:
        child = FakePtyChild()
        deadline = time.monotonic() - 1  # already exhausted
        runner = SmokeRunner(lambda cols, rows: child, deadline=deadline)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_screen", 1, id="never-runs", text="anything"),
        ]))
        assert results[1] == {
            "id": "never-runs", "status": "fail",
            "message": "aborted: tui-pty driver-level timeout exceeded",
        }


# ── run_smoke_spec (top-level entry point) ──────────────────────────────────


class TestRunSmokeSpec:
    def test_end_to_end_with_an_injected_fake(self) -> None:
        spec_text = """
steps:
  - type: launch
  - type: wait_idle
    ms: 30
    timeout_ms: 1000
  - type: expect_screen
    id: greeting
    text: HELLO
"""
        child = FakePtyChild(script=[(0.0, b"HELLO")])
        results = run_smoke_spec(
            spec_text, launch_command="unused", cwd=".",
            spawn_child=lambda cols, rows: child,
        )
        assert [r["id"] for r in results] == ["000 launch", "001 wait_idle", "greeting"]
        assert all(r["status"] == "pass" for r in results)

    def test_malformed_spec_raises_before_ever_spawning_a_child(self) -> None:
        spawned = []
        with pytest.raises(TuiPtySpecError):
            run_smoke_spec(
                "steps: []\n", launch_command="unused", cwd=".",
                spawn_child=lambda cols, rows: spawned.append(1) or FakePtyChild(),
            )
        assert spawned == []


# ── a real Unix pty (not the fake) — proves UnixPtyChild itself works ──────


@pytest.mark.skipif(os.name != "posix", reason="UnixPtyChild requires a POSIX platform")
class TestUnixPtyChildReal:
    def test_real_process_output_is_read_back_through_a_real_pty(self, tmp_path) -> None:
        command = f"{sys.executable} -c \"import sys; sys.stdout.write('HELLO FROM REAL PTY\\r\\n'); sys.stdout.flush(); import time; time.sleep(2)\""
        child = UnixPtyChild(command, str(tmp_path), cols=80, rows=24)
        try:
            collected = b""
            deadline = time.monotonic() + 5
            while b"HELLO FROM REAL PTY" not in collected and time.monotonic() < deadline:
                collected += child.read(0.2)
            assert b"HELLO FROM REAL PTY" in collected
            assert child.is_alive()
        finally:
            child.close()
        assert not child.is_alive()


# ── #3583: the pty child must be reaped even if its manager never gets to
# run close() (killed abnormally rather than exiting normally) ────────────


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.mark.skipif(
    os.name != "posix" or sys.platform != "linux",
    reason="the #3583 fix (PR_SET_PDEATHSIG) is Linux-only; UnixPtyChild "
    "requires POSIX but macOS has no prctl()",
)
class TestUnixPtyChildSurvivesAbnormalParentDeath:
    def test_child_is_reaped_when_manager_is_sigkilled(self, tmp_path) -> None:
        marker = tmp_path / "child.pid"
        script = (
            "import sys; sys.path.insert(0, " + repr(os.getcwd()) + ")\n"
            "from coord.tui_pty_driver import UnixPtyChild\n"
            "child = UnixPtyChild('sleep 60', " + repr(str(tmp_path)) + ", 80, 24)\n"
            "open(" + repr(str(marker)) + ", 'w').write(str(child._proc.pid))\n"
            "import time; time.sleep(60)\n"
        )
        manager = subprocess.Popen([sys.executable, "-c", script])
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.exists(), "manager process never reported a child pid"
            child_pid = int(marker.read_text())
            assert _pid_alive(child_pid), "child process never started"

            # Kill the manager WITHOUT ever letting it call close() — the
            # one case no userspace cleanup code can run for.
            manager.kill()
            manager.wait(timeout=5)

            # #2096: don't just check "no exception" — actually re-observe
            # the real OS process after giving the kernel a bounded window
            # to deliver the parent-death signal.
            deadline = time.monotonic() + 5
            while _pid_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not _pid_alive(child_pid), (
                f"pid {child_pid} is still alive {5}s after its managing "
                "process was SIGKILLed — the pty child was not reaped"
            )
        finally:
            if manager.poll() is None:
                manager.kill()
                manager.wait(timeout=5)
