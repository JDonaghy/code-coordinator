"""Tests for coord/win_native_driver.py — the ``win-native`` acceptance
driver's real engine (#3484): the native-spec YAML parser, the spec-to-Win32
translation (key/button naming, hit-test code lookup), and the step executor
(:class:`~coord.win_native_driver.NativeRunner`) driven against a scripted
fake :class:`~coord.win_native_driver.WinCalls` rather than a real Win32/UIA
call — per the issue's own acceptance bar ("unit tests for the
spec->Win32 translation with the OS calls faked"). A real run against a
real exe on real Windows hardware (dell64) is out of reach for this repo's
test suite (this worktree runs on Linux, and no target exe is checked in
here) and is exercised at the operator level instead — the same split
``tests/test_tui_pty_driver.py`` calls out for ConPTY.
"""

from __future__ import annotations

import base64
import ctypes
import os
import time

import pytest

from coord.win_native_driver import (
    NativeRunner,
    NativeSpec,
    NativeStep,
    WinNativeRuntimeError,
    WinNativeSpecError,
    Win32Calls,
    _find_a11y_match,
    _summarize_elements,
    _vkey_for,
    parse_native_spec,
    run_native_spec,
)

# ── parse_native_spec ───────────────────────────────────────────────────────


class TestParseNativeSpec:
    VALID_YAML = """
name: vimcode native smoke
width: 900
height: 700
mode: window
steps:
  - type: launch
  - type: click
    x: 400
    y: 10
    button: right
  - type: wait
    ms: 100
  - type: capture
  - type: expect_menu
    id: menu-1199
    items: ["File", "Edit", "Help"]
  - type: expect_hit
    id: close-hit
    x: 890
    y: 10
    ht: HTCLOSE
  - type: expect_a11y
    role: Button
    name: Close
  - type: expect_a11y_within
    role: MenuItem
    name: Cut
    timeout_ms: 500
  - type: expect_closed
    timeout_ms: 2000
"""

    def test_parses_all_step_types_and_top_level_fields(self) -> None:
        spec = parse_native_spec(self.VALID_YAML)
        assert spec.name == "vimcode native smoke"
        assert (spec.width, spec.height, spec.mode) == (900, 700, "window")
        assert [s.kind for s in spec.steps] == [
            "launch", "click", "wait", "capture", "expect_menu",
            "expect_hit", "expect_a11y", "expect_a11y_within", "expect_closed",
        ]

    def test_click_fields_parsed(self) -> None:
        spec = parse_native_spec(self.VALID_YAML)
        click = spec.steps[1]
        assert (click.x, click.y, click.button) == (400, 10, "right")

    def test_named_id_used_verbatim(self) -> None:
        spec = parse_native_spec(self.VALID_YAML)
        menu = next(s for s in spec.steps if s.kind == "expect_menu")
        assert menu.step_id == "menu-1199"
        assert menu.items == ("File", "Edit", "Help")

    def test_unnamed_step_gets_positional_id(self) -> None:
        spec = parse_native_spec(self.VALID_YAML)
        assert spec.steps[0].step_id == "000 launch"

    def test_defaults_when_top_level_fields_omitted(self) -> None:
        spec = parse_native_spec("steps:\n  - type: launch\n")
        assert spec.name == ""
        assert (spec.width, spec.height) == (1024, 768)
        assert spec.mode == "window"
        assert spec.terminal_app == ""

    def test_explicit_zero_x_is_honored_not_treated_as_absent(self) -> None:
        spec = parse_native_spec(
            "steps:\n  - type: click\n    x: 0\n    y: 0\n"
        )
        step = spec.steps[0]
        assert (step.x, step.y) == (0, 0)

    def test_terminal_mode_requires_terminal_app(self) -> None:
        with pytest.raises(WinNativeSpecError, match="terminal_app"):
            parse_native_spec("mode: terminal\nsteps:\n  - type: launch\n")

    def test_terminal_mode_with_valid_app_parses(self) -> None:
        spec = parse_native_spec(
            "mode: terminal\nterminal_app: windows-terminal\nsteps:\n  - type: launch\n"
        )
        assert spec.mode == "terminal"
        assert spec.terminal_app == "windows-terminal"

    def test_unrecognized_terminal_app_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="terminal_app"):
            parse_native_spec(
                "mode: terminal\nterminal_app: iterm2\nsteps:\n  - type: launch\n"
            )

    def test_unrecognized_mode_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="mode"):
            parse_native_spec("mode: teleport\nsteps:\n  - type: launch\n")

    def test_invalid_yaml_raises_spec_error(self) -> None:
        with pytest.raises(WinNativeSpecError, match="not valid YAML"):
            parse_native_spec("steps: [")

    def test_non_mapping_top_level_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="mapping"):
            parse_native_spec("- just\n- a\n- list\n")

    def test_missing_steps_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="steps"):
            parse_native_spec("name: nothing here\n")

    def test_empty_steps_list_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="steps"):
            parse_native_spec("steps: []\n")

    def test_step_not_a_mapping_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="steps\\[0\\]"):
            parse_native_spec("steps:\n  - just a string\n")

    def test_unknown_step_type_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="unknown step type"):
            parse_native_spec("steps:\n  - type: teleport\n")

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="key"):
            parse_native_spec("steps:\n  - type: key\n")

    def test_click_missing_x_y_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="x"):
            parse_native_spec("steps:\n  - type: click\n")

    def test_unrecognized_button_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="button"):
            parse_native_spec(
                "steps:\n  - type: click\n    x: 0\n    y: 0\n    button: super-click\n"
            )

    def test_unrecognized_hit_test_code_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="hit-test code"):
            parse_native_spec(
                "steps:\n  - type: expect_hit\n    x: 0\n    y: 0\n    ht: HTWHATEVER\n"
            )

    def test_expect_menu_requires_non_empty_items(self) -> None:
        with pytest.raises(WinNativeSpecError, match="items"):
            parse_native_spec("steps:\n  - type: expect_menu\n    items: []\n")

    def test_expect_menu_exact_flag_parsed(self) -> None:
        spec = parse_native_spec(
            "steps:\n  - type: expect_menu\n    items: [File]\n    exact: true\n"
        )
        assert spec.steps[0].exact is True

    # -- terminal-hosted mode's three added steps (2026-09-30) --

    def test_flicker_latency_panel_switch_steps_parse(self) -> None:
        spec = parse_native_spec(
            "mode: terminal\n"
            "terminal_app: conhost\n"
            "steps:\n"
            "  - type: launch\n"
            "  - type: expect_idle_stable\n"
            "    id: idle-flicker-1634\n"
            "    ms: 5000\n"
            "    interval_ms: 100\n"
            "  - type: expect_menu_latency\n"
            "    id: right-click-1635\n"
            "    x: 40\n"
            "    y: 12\n"
            "    max_ms: 300\n"
            "  - type: expect_panel_switch\n"
            "    id: activity-bar-1636\n"
            "    x: 2\n"
            "    y: 5\n"
            "    role: Pane\n"
            "    name: Explorer\n"
        )
        kinds = [s.kind for s in spec.steps]
        assert kinds == [
            "launch", "expect_idle_stable", "expect_menu_latency", "expect_panel_switch",
        ]
        idle, latency, switch = spec.steps[1], spec.steps[2], spec.steps[3]
        assert (idle.ms, idle.interval_ms) == (5000, 100)
        assert (latency.x, latency.y, latency.max_ms) == (40, 12, 300)
        assert (switch.role, switch.name) == ("Pane", "Explorer")

    def test_panel_switch_missing_role_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="role"):
            parse_native_spec(
                "steps:\n  - type: expect_panel_switch\n    x: 0\n    y: 0\n    name: Explorer\n"
            )


# ── _vkey_for ────────────────────────────────────────────────────────────────


class TestVkeyFor:
    def test_named_keys_case_insensitive(self) -> None:
        assert _vkey_for("enter") == (0x0D, False)
        assert _vkey_for("ENTER") == (0x0D, False)
        assert _vkey_for("Esc") == (0x1B, False)

    def test_ctrl_combo(self) -> None:
        assert _vkey_for("ctrl+c") == (ord("C"), False)

    def test_lowercase_literal_needs_no_shift(self) -> None:
        assert _vkey_for("a") == (ord("A"), False)

    def test_uppercase_literal_needs_shift(self) -> None:
        assert _vkey_for("A") == (ord("A"), True)

    def test_unrecognized_key_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="unrecognized key"):
            _vkey_for("moonwalk")


# ── _find_a11y_match / _summarize_elements ──────────────────────────────────


class TestFindA11yMatch:
    ELEMENTS = [
        {"role": "MenuBar", "name": "", "visible": True},
        {"role": "MenuItem", "name": "File", "visible": True},
        {"role": "MenuItem", "name": "Edit", "visible": True},
        {"role": "Button", "name": "Close", "visible": False},
    ]

    def test_matches_role_and_name_substring(self) -> None:
        match = _find_a11y_match(self.ELEMENTS, "MenuItem", "Fil")
        assert match is not None
        assert match["name"] == "File"

    def test_role_match_is_case_insensitive(self) -> None:
        assert _find_a11y_match(self.ELEMENTS, "menuitem", "file") is not None

    def test_empty_name_matches_any(self) -> None:
        assert _find_a11y_match(self.ELEMENTS, "MenuBar", "") is not None

    def test_no_match_returns_none(self) -> None:
        assert _find_a11y_match(self.ELEMENTS, "MenuItem", "Help") is None

    def test_invisible_element_is_skipped(self) -> None:
        # The "Close" button exists but is explicitly marked invisible —
        # quadraui#1199's whole bug class: a menu/control that technically
        # exists in the tree but isn't actually reachable.
        assert _find_a11y_match(self.ELEMENTS, "Button", "Close") is None

    def test_summarize_empty_tree(self) -> None:
        assert _summarize_elements([]) == "(empty tree)"

    def test_summarize_non_empty_tree(self) -> None:
        summary = _summarize_elements(self.ELEMENTS)
        assert "File" in summary and "Edit" in summary


# ── a scripted fake WinCalls, for exercising NativeRunner deterministically ─


class FakeWinCalls:
    """A scripted :class:`~coord.win_native_driver.WinCalls` (#3484).
    Every real-OS call is recorded so tests can assert exactly what
    :class:`NativeRunner` sent, and every behavior (menu items, hit-test
    result, UIA tree, capture bytes, window aliveness) is injected rather
    than actually touching an OS."""

    def __init__(
        self,
        *,
        launch_fails: bool = False,
        window_never_appears: bool = False,
        menu_items: list[str] | None = "UNSET",
        hit_test_result: str = "HTCLIENT",
        uia_script: list[list[dict]] | None = None,
        capture_script: list[bytes] | None = None,
        closes_after_n_polls: int | None = None,
        session_ok: bool = True,
        session_reason: str = "",
    ) -> None:
        self.launch_fails = launch_fails
        self.window_never_appears = window_never_appears
        self._menu_items = None if menu_items == "UNSET" else menu_items
        self._hit_test_result = hit_test_result
        self._uia_script = list(uia_script or [[]])
        self._capture_script = list(capture_script or [b"frame-0"])
        self._closes_after_n_polls = closes_after_n_polls
        self._alive_poll_count = 0
        self._session_ok = session_ok
        self._session_reason = session_reason

        self.launched: list[tuple[str, str]] = []
        self.launched_in_terminal: list[tuple[str, str, str]] = []
        self.moved: list[tuple[int, int, int, int, int]] = []
        self.clicks: list[tuple[int, int, int, str]] = []
        self.keys: list[tuple[int, str]] = []
        self.killed: list[int] = []
        self._next_pid = 1000
        self._hwnd = 5555

    def session_available(self) -> tuple[bool, str]:
        return self._session_ok, self._session_reason

    def launch(self, command: str, cwd: str) -> int:
        if self.launch_fails:
            raise WinNativeRuntimeError("could not start process")
        self.launched.append((command, cwd))
        self._next_pid += 1
        return self._next_pid

    def launch_in_terminal(self, command: str, cwd: str, terminal_app: str) -> int:
        self.launched_in_terminal.append((command, cwd, terminal_app))
        self._next_pid += 1
        return self._next_pid

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        if self.window_never_appears:
            raise WinNativeRuntimeError(f"no window for pid={pid}")
        return self._hwnd

    def move_window(self, hwnd: int, x: int, y: int, width: int, height: int) -> None:
        self.moved.append((hwnd, x, y, width, height))

    def is_window_alive(self, hwnd: int) -> bool:
        if self._closes_after_n_polls is None:
            return True
        self._alive_poll_count += 1
        return self._alive_poll_count <= self._closes_after_n_polls

    def get_menu_items(self, hwnd: int) -> list[str] | None:
        return self._menu_items

    def hit_test(self, hwnd: int, x: int, y: int) -> str:
        return self._hit_test_result

    def send_click(self, hwnd: int, x: int, y: int, button: str) -> None:
        self.clicks.append((hwnd, x, y, button))

    def send_key(self, hwnd: int, key: str) -> None:
        self.keys.append((hwnd, key))

    def uia_elements(self, hwnd: int) -> list[dict]:
        if len(self._uia_script) > 1:
            return self._uia_script.pop(0)
        return self._uia_script[0]

    def capture(self, hwnd: int) -> bytes:
        if len(self._capture_script) > 1:
            return self._capture_script.pop(0)
        return self._capture_script[0]

    def kill(self, pid: int) -> None:
        self.killed.append(pid)


def _step(kind: str, index: int = 0, **kwargs) -> NativeStep:
    return NativeStep(kind=kind, index=index, **kwargs)


def _spec(steps: list[NativeStep], **kwargs) -> NativeSpec:
    defaults = {"name": "test", "width": 800, "height": 600, "mode": "window", "terminal_app": ""}
    defaults.update(kwargs)
    return NativeSpec(steps=tuple(steps), **defaults)


def _runner(calls: FakeWinCalls, **kwargs) -> NativeRunner:
    return NativeRunner(calls, "./app.exe", "/cwd", **kwargs)


class TestNativeRunnerLaunch:
    def test_launch_success_reports_passing_entry_and_moves_window(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")], width=900, height=700))
        assert results == [{"id": "000 launch", "status": "pass", "message": ""}]
        assert calls.launched == [("./app.exe", "/cwd")]
        assert calls.moved  # MoveWindow was called with the spec's size

    def test_launch_process_failure_reports_failing_entry(self) -> None:
        calls = FakeWinCalls(launch_fails=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results[0]["status"] == "fail"
        assert "could not start process" in results[0]["message"]

    def test_launch_window_never_appears_reports_failing_entry(self) -> None:
        calls = FakeWinCalls(window_never_appears=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results[0]["status"] == "fail"
        assert "no window" in results[0]["message"]

    def test_terminal_mode_launches_via_launch_in_terminal(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        results = runner.run(
            _spec([_step("launch")], mode="terminal", terminal_app="windows-terminal")
        )
        assert results[0]["status"] == "pass"
        assert calls.launched_in_terminal == [("./app.exe", "/cwd", "windows-terminal")]
        assert calls.launched == []

    def test_step_before_launch_fails_with_a_clear_message(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        results = runner.run(_spec([_step("key", key="a")]))
        assert results[0]["status"] == "fail"
        assert "no window" in results[0]["message"]


class TestNativeRunnerSessionPrecheck:
    """#3510: a locked or absent interactive Windows session is reported as
    a distinct ``unavailable`` lane verdict, never an ordinary failed step —
    and no step (not even ``launch``) runs when it's locked."""

    def test_locked_session_reports_unavailable_and_runs_no_step(self) -> None:
        calls = FakeWinCalls(
            session_ok=False,
            session_reason="LogonUI.exe is running in session 1 — the desktop is locked",
        )
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_menu", 1, items=["File"]),
        ]))
        assert results == [{
            "id": "session",
            "status": "unavailable",
            "message": "LogonUI.exe is running in session 1 — the desktop is locked",
        }]
        # No step ran at all — not even `launch`.
        assert calls.launched == []

    def test_locked_session_never_a_failed_step(self) -> None:
        calls = FakeWinCalls(session_ok=False, session_reason="locked")
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert all(r["status"] != "fail" for r in results)

    def test_unlocked_session_runs_normally(self) -> None:
        calls = FakeWinCalls(session_ok=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results == [{"id": "000 launch", "status": "pass", "message": ""}]
        assert calls.launched == [("./app.exe", "/cwd")]

    def test_locked_session_never_tears_down_a_pid_since_none_was_launched(self) -> None:
        calls = FakeWinCalls(session_ok=False, session_reason="locked")
        runner = _runner(calls)
        runner.run(_spec([_step("launch")]))
        assert calls.killed == []


class TestNativeRunnerActions:
    def test_key_step_forwards_to_calls(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0), _step("key", 1, key="enter")]))
        assert calls.keys == [(calls._hwnd, "enter")]

    def test_click_step_forwards_coordinates_and_button(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        runner.run(_spec([
            _step("launch", 0), _step("click", 1, x=42, y=7, button="right"),
        ]))
        assert calls.clicks == [(calls._hwnd, 42, 7, "right")]

    def test_click_defaults_to_left_button(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0), _step("click", 1, x=1, y=1)]))
        assert calls.clicks == [(calls._hwnd, 1, 1, "left")]

    def test_wait_step_sleeps_approximately_ms(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        start = time.monotonic()
        runner.run(_spec([_step("launch", 0), _step("wait", 1, ms=50)]))
        assert time.monotonic() - start >= 0.05

    def test_capture_step_attaches_capture_b64_on_pass(self) -> None:
        calls = FakeWinCalls(capture_script=[b"one-frame"])
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch", 0), _step("capture", 1)]))
        capture_entry = results[1]
        assert capture_entry["status"] == "pass"
        assert base64.b64decode(capture_entry["capture_b64"]) == b"one-frame"


class TestNativeRunnerExpectMenu:
    def test_missing_menu_fails(self) -> None:
        calls = FakeWinCalls(menu_items=None)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_menu", 1, items=("File",)),
        ]))
        assert results[1]["status"] == "fail"
        assert "no native menu" in results[1]["message"]

    def test_subset_match_passes(self) -> None:
        calls = FakeWinCalls(menu_items=["File", "Edit", "View", "Help"])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_menu", 1, items=("File", "Help")),
        ]))
        assert results[1]["status"] == "pass"

    def test_missing_item_fails(self) -> None:
        calls = FakeWinCalls(menu_items=["File", "Edit"])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_menu", 1, items=("File", "Help")),
        ]))
        assert results[1]["status"] == "fail"
        assert "Help" in results[1]["message"]

    def test_exact_mismatch_fails_even_if_superset(self) -> None:
        calls = FakeWinCalls(menu_items=["File", "Edit", "Help"])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_menu", 1, items=("File", "Edit"), exact=True),
        ]))
        assert results[1]["status"] == "fail"


class TestNativeRunnerExpectHit:
    def test_matching_hit_test_passes(self) -> None:
        calls = FakeWinCalls(hit_test_result="HTCLOSE")
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_hit", 1, x=10, y=10, ht="HTCLOSE"),
        ]))
        assert results[1]["status"] == "pass"

    def test_mismatching_hit_test_fails(self) -> None:
        calls = FakeWinCalls(hit_test_result="HTCLIENT")
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_hit", 1, x=10, y=10, ht="HTCLOSE"),
        ]))
        assert results[1]["status"] == "fail"
        assert "HTCLIENT" in results[1]["message"]


class TestNativeRunnerExpectA11y:
    def test_match_passes(self) -> None:
        calls = FakeWinCalls(uia_script=[[{"role": "Button", "name": "Close", "visible": True}]])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="Button", name="Close"),
        ]))
        assert results[1]["status"] == "pass"

    def test_no_match_fails_with_tree_summary(self) -> None:
        calls = FakeWinCalls(uia_script=[[{"role": "Button", "name": "Open", "visible": True}]])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="Button", name="Close"),
        ]))
        assert results[1]["status"] == "fail"
        assert "Open" in results[1]["message"]


class TestNativeRunnerExpectA11yWithin:
    def test_appears_within_timeout_reports_elapsed_message(self) -> None:
        calls = FakeWinCalls(uia_script=[
            [], [], [{"role": "MenuItem", "name": "Cut", "visible": True}],
        ])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_a11y_within", 1, role="MenuItem", name="Cut", timeout_ms=2000),
        ]))
        assert results[1]["status"] == "pass"
        assert "ms" in results[1]["message"]

    def test_never_appears_times_out_and_fails(self) -> None:
        calls = FakeWinCalls(uia_script=[[]])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_a11y_within", 1, role="MenuItem", name="Cut", timeout_ms=100),
        ]))
        assert results[1]["status"] == "fail"
        assert "never appeared" in results[1]["message"]


class TestNativeRunnerExpectClosed:
    def test_window_that_closes_passes(self) -> None:
        calls = FakeWinCalls(closes_after_n_polls=1)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_closed", 1, timeout_ms=1000),
        ]))
        assert results[1]["status"] == "pass"

    def test_window_that_never_closes_fails(self) -> None:
        # closes_after_n_polls=None -> is_window_alive always True.
        calls = FakeWinCalls()
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_closed", 1, timeout_ms=100),
        ]))
        assert results[1]["status"] == "fail"
        assert "did not actually close" in results[1]["message"]


class TestNativeRunnerExpectIdleStable:
    """vimcode#1634's idle-flicker oracle."""

    def test_identical_captures_pass(self) -> None:
        calls = FakeWinCalls(capture_script=[b"same"] * 5)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_idle_stable", 1, ms=150, interval_ms=30),
        ]))
        assert results[1]["status"] == "pass"

    def test_differing_capture_fails_and_attaches_the_differing_frame(self) -> None:
        calls = FakeWinCalls(capture_script=[b"frame-a", b"frame-a", b"frame-b"])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_idle_stable", 1, ms=150, interval_ms=30),
        ]))
        entry = results[1]
        assert entry["status"] == "fail"
        assert "flicker" in entry["message"]
        # #3484's acceptance bar: "a capture is attached to each failing
        # step" — and it must be the actual differing frame, not some
        # unrelated later capture.
        assert base64.b64decode(entry["capture_b64"]) == b"frame-b"


class TestNativeRunnerExpectMenuLatency:
    """vimcode#1635's right-click-menu-latency oracle."""

    def test_menu_appears_reports_latency_and_sends_right_click(self) -> None:
        calls = FakeWinCalls(uia_script=[
            [], [{"role": "MenuItem", "name": "Cut", "visible": True}],
        ])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_menu_latency", 1, x=10, y=20, max_ms=1000),
        ]))
        assert results[1]["status"] == "pass"
        assert "ms after right-click" in results[1]["message"]
        assert calls.clicks == [(calls._hwnd, 10, 20, "right")]

    def test_menu_never_appears_fails_and_attaches_capture(self) -> None:
        calls = FakeWinCalls(uia_script=[[]], capture_script=[b"stuck-frame"])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_menu_latency", 1, x=10, y=20, max_ms=50),
        ]))
        assert results[1]["status"] == "fail"
        assert "capture_b64" in results[1]


class TestNativeRunnerExpectPanelSwitch:
    """vimcode#1636's dead-activity-bar-click oracle."""

    def test_panel_switch_reports_latency_and_sends_left_click(self) -> None:
        calls = FakeWinCalls(uia_script=[
            [], [{"role": "Pane", "name": "Explorer", "visible": True}],
        ])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_panel_switch", 1, x=2, y=5, role="Pane", name="Explorer", timeout_ms=1000),
        ]))
        assert results[1]["status"] == "pass"
        assert "activity-bar click" in results[1]["message"]
        assert calls.clicks == [(calls._hwnd, 2, 5, "left")]

    def test_dead_click_never_switches_fails(self) -> None:
        calls = FakeWinCalls(uia_script=[[{"role": "Pane", "name": "Search", "visible": True}]])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_panel_switch", 1, x=2, y=5, role="Pane", name="Explorer", timeout_ms=50),
        ]))
        assert results[1]["status"] == "fail"


class TestNativeRunnerTeardownAndSafety:
    def test_teardown_kills_only_the_launched_pid(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0)]))
        assert len(calls.killed) == 1
        launched_pid = calls.killed[0]
        assert launched_pid >= 1001  # FakeWinCalls' own pid counter

    def test_teardown_does_not_kill_when_launch_never_ran(self) -> None:
        calls = FakeWinCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("key", 0, key="a")]))  # fails: no launch
        assert calls.killed == []

    def test_teardown_still_kills_when_a_later_step_raises(self) -> None:
        calls = FakeWinCalls(menu_items=None)
        runner = _runner(calls)
        runner.run(_spec([
            _step("launch", 0), _step("expect_menu", 1, items=("File",)),
        ]))
        assert len(calls.killed) == 1

    def test_no_image_name_kill_path_exists_in_the_module(self) -> None:
        """Safety invariant from the module docstring: teardown must only
        ever be able to kill the exact PID this driver itself launched —
        never by matching a process by its executable's filename. A
        `taskkill /IM`-style call, or any process-enumeration API used to
        find a victim by name, anywhere in this module's actual CODE (not
        its prose docstrings, which legitimately describe the invariant by
        name) would be one bad assumption away from killing an operator's
        own Windows Terminal/conhost session."""
        import ast

        import coord.win_native_driver as module

        source = open(module.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        code_only = "\n".join(
            ast.unparse(node) for node in ast.walk(tree)
            if isinstance(node, (ast.Call, ast.Import, ast.ImportFrom))
        ).lower()
        for forbidden in ("taskkill", "process_iter", "getmodulebasename", "os.system"):
            assert forbidden not in code_only, f"found forbidden pattern {forbidden!r} in actual code"

        # And the one sanctioned kill path's signature is exactly `(self,
        # pid: int)` — not e.g. `(self, pid, name=None)` that a later edit
        # could widen into a by-name fallback.
        import inspect

        sig = inspect.signature(module.Win32Calls.kill)
        assert list(sig.parameters) == ["self", "pid"]


class TestNativeRunnerDriverLevelTimeout:
    def test_steps_past_the_deadline_are_aborted_not_run(self) -> None:
        calls = FakeWinCalls()
        runner = NativeRunner(
            calls, "./app.exe", "/cwd", deadline=time.monotonic() - 1,
        )
        results = runner.run(_spec([
            _step("launch", 0), _step("key", 1, key="a"),
        ]))
        assert all(r["status"] == "fail" for r in results)
        assert "driver-level timeout" in results[0]["message"]
        assert calls.launched == []  # never even got to run `launch`


# ── Win32Calls platform guard + optional-dependency guard ──────────────────


class TestWin32CallsPlatformGuard:
    def test_construction_off_windows_raises(self) -> None:
        if os.name == "nt":
            pytest.skip("this guard only fires off Windows")
        with pytest.raises(WinNativeRuntimeError, match="Windows"):
            Win32Calls()


class _FakeUser32:
    """``OpenInputDesktop``/``CloseDesktop`` stand-in — desktop unlocked."""

    def OpenInputDesktop(self, *_args, **_kwargs):
        return 0x1234  # truthy handle

    def CloseDesktop(self, _hdesk) -> None:
        pass


class _ProcessEntry32Mirror(ctypes.Structure):
    """Field-for-field mirror of the ``PROCESSENTRY32`` defined inline in
    :meth:`Win32Calls._logonui_running_in_session` — not the same Python
    class, but same layout, so ``ctypes.cast`` on the ``byref`` pointer the
    real method passes in reads/writes the same memory the fakes below
    populate."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_char * 260),
    ]


class _FakeKernel32NoSession:
    """Mimics the *real* kernel32 (#3521): ``WTSGetActiveConsoleSessionId``
    is present and callable here — unlike the old, wrong ``wtsapi32``
    lookup, which would raise ``AttributeError`` on real Windows."""

    exe_name = b"explorer.exe"

    def __init__(self, session_id: int = 1) -> None:
        self._session_id = session_id

    def WTSGetActiveConsoleSessionId(self) -> int:
        return self._session_id

    def CreateToolhelp32Snapshot(self, *_args, **_kwargs):
        return 1  # non-zero, non -1 "handle"

    def Process32First(self, _snapshot, entry_ref) -> bool:
        entry = ctypes.cast(entry_ref, ctypes.POINTER(_ProcessEntry32Mirror)).contents
        entry.szExeFile = self.exe_name
        return True

    def Process32Next(self, _snapshot, _entry_ref) -> bool:
        return False  # only the one process — no LogonUI.exe

    def ProcessIdToSessionId(self, _pid, session_ref) -> bool:
        session_ref._obj.value = self._session_id
        return True

    def CloseHandle(self, _handle) -> None:
        pass


def _ensure_mbcs_codec_available() -> None:
    """``_logonui_running_in_session`` decodes ``szExeFile`` with Windows'
    ``mbcs`` codec, which only exists on real Windows. Register an
    ascii-compatible alias off-Windows so these tests can exercise that
    real decode path (all the fake exe names here are ASCII) instead of
    mocking around it — a no-op if ``mbcs`` is already natively available."""
    import codecs

    try:
        codecs.lookup("mbcs")
    except LookupError:
        codecs.register(lambda name: codecs.lookup("ascii") if name == "mbcs" else None)


def _make_win32_calls(user32, kernel32) -> Win32Calls:
    """Build a :class:`Win32Calls` bypassing ``__init__``'s platform guard
    (construction requires real Windows) with real ``ctypes``/``ctypes.
    wintypes`` — both work fine off-Windows — but faked ``user32``/
    ``kernel32`` handles, exactly mirroring what ``__init__`` would have
    set on a real Windows host."""
    import ctypes.wintypes  # noqa: F401 - imported for side effect, used by the class

    _ensure_mbcs_codec_available()
    calls = object.__new__(Win32Calls)
    calls._ctypes = ctypes
    calls._user32 = user32
    calls._kernel32 = kernel32
    return calls


class TestSessionAvailable:
    """#3521: ``WTSGetActiveConsoleSessionId`` lives on kernel32, not
    wtsapi32 — the old code crashed every win-native run with an
    uncaught ``AttributeError`` before this fix. These tests fake
    ``Win32Calls`` so the wtsapi32-shaped mistake would raise if
    reintroduced, and confirm the probe is defensive end to end."""

    def test_uses_kernel32_and_reports_available_when_unlocked(self) -> None:
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        available, reason = calls.session_available()
        assert available is True
        assert reason == ""

    def test_a_raising_probe_yields_unavailable_with_reason_not_an_exception(
        self,
    ) -> None:
        class _ExplodingUser32:
            def OpenInputDesktop(self, *_args, **_kwargs):
                raise AttributeError(
                    "function 'WTSGetActiveConsoleSessionId' not found"
                )

        calls = _make_win32_calls(_ExplodingUser32(), _FakeKernel32NoSession())
        available, reason = calls.session_available()
        assert available is False
        assert "failed" in reason.lower()
        assert "WTSGetActiveConsoleSessionId" in reason

    def test_invalid_session_id_reports_unavailable(self) -> None:
        INVALID_SESSION_ID = 0xFFFFFFFF
        calls = _make_win32_calls(
            _FakeUser32(), _FakeKernel32NoSession(session_id=INVALID_SESSION_ID),
        )
        available, reason = calls.session_available()
        assert available is False
        assert "no active console session" in reason

    def test_logonui_running_reports_locked(self) -> None:
        class _FakeKernel32Locked(_FakeKernel32NoSession):
            exe_name = b"LogonUI.exe"

        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32Locked())
        available, reason = calls.session_available()
        assert available is False
        assert "LogonUI.exe" in reason

    def test_open_input_desktop_failure_reports_unavailable(self) -> None:
        class _FakeUser32Locked:
            def OpenInputDesktop(self, *_args, **_kwargs):
                return 0  # falsy handle — no interactive desktop

        calls = _make_win32_calls(_FakeUser32Locked(), _FakeKernel32NoSession())
        available, reason = calls.session_available()
        assert available is False
        assert "OpenInputDesktop" in reason


class TestImportUia:
    def test_missing_comtypes_raises_actionable_error(self, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "comtypes.client" or name == "comtypes":
                raise ModuleNotFoundError("no module named comtypes")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        from coord.win_native_driver import _import_uia

        with pytest.raises(WinNativeRuntimeError, match="win-native.*extra"):
            _import_uia()


# ── run_native_spec top-level entry point ───────────────────────────────────


class TestRunNativeSpec:
    def test_runs_against_an_injected_fake_and_returns_normalized_tests(self) -> None:
        calls = FakeWinCalls(menu_items=["File", "Edit", "View", "Help"])
        spec_text = (
            "steps:\n"
            "  - type: launch\n"
            "  - type: expect_menu\n"
            "    id: menu-1199\n"
            "    items: [File, Help]\n"
        )
        tests = run_native_spec(
            spec_text, launch_command="vimcode.exe", cwd="/repo", calls=calls,
        )
        assert tests == [
            {"id": "000 launch", "status": "pass", "message": ""},
            {"id": "menu-1199", "status": "pass", "message": ""},
        ]

    def test_malformed_spec_raises_spec_error_before_any_launch(self) -> None:
        calls = FakeWinCalls()
        with pytest.raises(WinNativeSpecError):
            run_native_spec("steps: []\n", launch_command="x.exe", cwd="/repo", calls=calls)
        assert calls.launched == []

    def test_locked_session_reports_unavailable_not_fail(self) -> None:
        """#3510: `run_native_spec` surfaces the session precheck the exact
        same way as `NativeRunner.run` — a locked session is `unavailable`,
        never folded into an ordinary failing step."""
        calls = FakeWinCalls(session_ok=False, session_reason="desktop is locked")
        tests = run_native_spec(
            "steps:\n  - type: launch\n",
            launch_command="vimcode.exe", cwd="/repo", calls=calls,
        )
        assert tests == [
            {"id": "session", "status": "unavailable", "message": "desktop is locked"}
        ]
        assert calls.launched == []
