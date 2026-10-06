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
import ntpath
import os
import select
import struct
import subprocess
import sys
import time

import pytest

from coord.key_spec import UnsupportedKey
from coord.win_native_driver import (
    IMAGE_SUBSYSTEM_WINDOWS_CUI,
    IMAGE_SUBSYSTEM_WINDOWS_GUI,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    NativeRunner,
    NativeSpec,
    NativeStep,
    WinKeyEncoding,
    WinNativeRuntimeError,
    WinNativeSession,
    WinNativeSpecError,
    Win32Calls,
    _DRIVE_REMOTE,
    _detect_console_subsystem,
    _encode_win_chord,
    _execute_staging,
    _find_a11y_match,
    _get_drive_type,
    _guess_exe_path,
    _is_remote_exe_token,
    _is_rooted_or_drive_qualified,
    _is_unc_path,
    _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    _leading_token,
    _looks_shell_composed,
    _normalize_posix_exe_token,
    _pe_subsystem,
    _plan_staging,
    _popen_command_and_cwd,
    _StagingPlan,
    _STALE_SESSION_MAX_AGE_S,
    _strip_cd_prefix,
    _summarize_elements,
    _win_key_encodings,
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


# ── _win_key_encodings / _encode_win_chord (#3639) ──────────────────────────


def _enc(vk=None, unicode_char=None, shift=False, ctrl=False, alt=False, win=False):
    return WinKeyEncoding(vk=vk, unicode_char=unicode_char, shift=shift, ctrl=ctrl, alt=alt, win=win)


class TestWinKeyEncodings:
    def test_named_keys_case_insensitive(self) -> None:
        assert _win_key_encodings("enter") == [_enc(vk=0x0D)]
        assert _win_key_encodings("ENTER") == [_enc(vk=0x0D)]
        assert _win_key_encodings("Esc") == [_enc(vk=0x1B)]

    def test_ctrl_combo(self) -> None:
        assert _win_key_encodings("ctrl+c") == [_enc(vk=ord("C"), ctrl=True)]

    def test_lowercase_literal_needs_no_shift(self) -> None:
        assert _win_key_encodings("a") == [_enc(vk=ord("A"))]

    def test_uppercase_literal_needs_shift(self) -> None:
        assert _win_key_encodings("A") == [_enc(vk=ord("A"), shift=True)]

    def test_unrecognized_key_raises(self) -> None:
        with pytest.raises(WinNativeSpecError, match="unrecognized key"):
            _win_key_encodings("moonwalk")

    def test_alt_m(self) -> None:
        # #3636: `Alt+m` previously raised "unrecognized key".
        assert _win_key_encodings("alt+m") == [_enc(vk=ord("M"), alt=True)]

    def test_alt_up_and_alt_f4(self) -> None:
        assert _win_key_encodings("Alt+Up") == [_enc(vk=0x26, alt=True)]
        assert _win_key_encodings("Alt+F4") == [_enc(vk=0x73, alt=True)]

    def test_shift_right_and_shift_end(self) -> None:
        assert _win_key_encodings("Shift+Right") == [_enc(vk=0x27, shift=True)]
        assert _win_key_encodings("Shift+End") == [_enc(vk=0x23, shift=True)]

    def test_ctrl_shift_right(self) -> None:
        assert _win_key_encodings("Ctrl+Shift+Right") == [_enc(vk=0x27, ctrl=True, shift=True)]

    def test_shift_f3(self) -> None:
        assert _win_key_encodings("shift+f3") == [_enc(vk=0x72, shift=True)]

    def test_punctuation_maps_to_real_vk_oem_codes_never_a_silent_no_op(self) -> None:
        # #3635/#3639: these previously returned `{"ok": true}` with no
        # effect at all. `.`/`:` now carry a real VK_OEM_* code (closing
        # the "ctrl+/ does nothing" shape of the bug too); `$`/`@` are
        # digit-row Shift symbols, mapped to their underlying digit's VK.
        assert _win_key_encodings(".") == [_enc(vk=0xBE)]
        assert _win_key_encodings(":") == [_enc(vk=0xBA, shift=True)]
        assert _win_key_encodings("@") == [_enc(vk=ord("2"), shift=True)]
        assert _win_key_encodings("$") == [_enc(vk=ord("4"), shift=True)]

    def test_ctrl_slash_now_produces_a_real_accelerator(self) -> None:
        # #3639 review finding: `ctrl+/` previously went through
        # KEYEVENTF_UNICODE with wVk=0 while Ctrl was held, which does not
        # generate a Windows accelerator at all — silently nothing, still
        # reported as a pass.
        assert _win_key_encodings("ctrl+/") == [_enc(vk=0xBF, ctrl=True)]

    def test_bare_unmapped_character_still_goes_through_unicode(self) -> None:
        assert _win_key_encodings("é") == [_enc(unicode_char="é")]

    def test_modifier_plus_unmappable_char_raises_unsupported(self) -> None:
        with pytest.raises(UnsupportedKey):
            _win_key_encodings("ctrl+é")

    def test_chord_sequence_ctrl_k_ctrl_w(self) -> None:
        assert _win_key_encodings("ctrl+k ctrl+w") == [
            _enc(vk=ord("K"), ctrl=True),
            _enc(vk=ord("W"), ctrl=True),
        ]

    def test_cmd_maps_to_vk_lwin(self) -> None:
        assert _win_key_encodings("cmd+r") == [_enc(vk=ord("R"), win=True)]

    def test_encode_win_chord_raises_unsupported_for_an_unknown_named_key(self) -> None:
        # Every grammar-level named key (enter/esc/tab/.../f1-f24) happens
        # to have a real VK_* code today, so this path can't be reached
        # through `_win_key_encodings` right now — exercised directly so the
        # raise itself (never a silent fallthrough) is proven reachable
        # (#2096: a gate must be able to fail).
        from coord.key_spec import KeyChord

        bogus = KeyChord(modifiers=frozenset(), base="nonexistent", is_char=False)
        with pytest.raises(UnsupportedKey):
            _encode_win_chord(bogus)



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

    def OpenProcess(self, *_args, **_kwargs):
        # #3617: `kill`'s own `OpenProcess`/`TerminateProcess` — no real
        # process to terminate in these tests, so this reports "no such
        # handle" (falsy), the same as a pid that's already gone.
        return 0

    def TerminateProcess(self, *_args, **_kwargs) -> None:
        raise AssertionError("TerminateProcess must not be called when OpenProcess returned no handle")


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
    calls._staged_session_dirs = {}  # #3617 — normally set by `__init__`
    calls._job_handles = {}  # #3634 — normally set by `__init__`
    calls.staging_warning = None  # #3617 — normally set by `__init__`
    return calls


class _FakeUser32SendInput:
    """Records every ``SendInput`` call as the decoded sequence of
    ``(vk, scan, flags)`` tuples it was asked to post — real ``ctypes``
    structs, read back through the SAME ``_INPUT``/``_KEYBDINPUT`` layout
    :meth:`Win32Calls.send_key` builds, so this is a genuine assertion on
    the platform event sequence (#3639 review non-blocking concern: the
    win/mac tests used to stop at the intermediate encoding dataclass)."""

    def __init__(self, sent_count: int | None = None) -> None:
        self.calls: list[list[tuple[int, int, int]]] = []
        #: Override the count `SendInput` reports as actually inserted —
        #: `None` means "report the full count" (success).
        self._sent_count = sent_count

    def SetForegroundWindow(self, _hwnd) -> None:
        pass

    def SendInput(self, count, arr, _struct_size) -> int:
        batch = [(arr[i].ki.wVk, arr[i].ki.wScan, arr[i].ki.dwFlags) for i in range(count)]
        self.calls.append(batch)
        return self._sent_count if self._sent_count is not None else count


class TestSendKeyRealSequence:
    """:meth:`Win32Calls.send_key` against a fake ``user32`` that decodes
    the real ``SendInput`` struct it was actually handed — not just the
    intermediate :class:`WinKeyEncoding`."""

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    VK_CONTROL = 0x11
    VK_SHIFT = 0x10

    def test_ctrl_shift_right_posts_both_modifiers_then_key_then_releases_in_reverse(self) -> None:
        user32 = _FakeUser32SendInput()
        calls = _make_win32_calls(user32, kernel32=None)
        calls.send_key(1, "ctrl+shift+right")
        [batch] = user32.calls
        # down: ctrl, shift, right(=0x27) ; up: shift, ctrl, right — modifier
        # ups unwind in the OPPOSITE order from their downs.
        assert batch == [
            (self.VK_CONTROL, 0, 0),
            (self.VK_SHIFT, 0, 0),
            (0x27, 0, 0),
            (0x27, 0, self.KEYEVENTF_KEYUP),
            (self.VK_SHIFT, 0, self.KEYEVENTF_KEYUP),
            (self.VK_CONTROL, 0, self.KEYEVENTF_KEYUP),
        ]

    def test_colon_posts_the_real_oem_vk_with_shift_not_keyeventf_unicode(self) -> None:
        # #3639: `:` now has a real VK_OEM_1 (0xBA) + implied Shift.
        user32 = _FakeUser32SendInput()
        calls = _make_win32_calls(user32, kernel32=None)
        calls.send_key(1, ":")
        [batch] = user32.calls
        assert batch == [
            (self.VK_SHIFT, 0, 0),
            (0xBA, 0, 0),
            (0xBA, 0, self.KEYEVENTF_KEYUP),
            (self.VK_SHIFT, 0, self.KEYEVENTF_KEYUP),
        ]

    def test_bare_unmapped_char_still_uses_keyeventf_unicode(self) -> None:
        user32 = _FakeUser32SendInput()
        calls = _make_win32_calls(user32, kernel32=None)
        calls.send_key(1, "é")
        [batch] = user32.calls
        code = ord("é")
        assert batch == [
            (0, code, self.KEYEVENTF_UNICODE),
            (0, code, self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP),
        ]

    def test_chord_sequence_sends_each_chord_as_its_own_sendinput_call(self) -> None:
        user32 = _FakeUser32SendInput()
        calls = _make_win32_calls(user32, kernel32=None)
        calls.send_key(1, "ctrl+k ctrl+w")
        assert len(user32.calls) == 2

    def test_delete_and_backspace_post_different_vks(self) -> None:
        user32 = _FakeUser32SendInput()
        calls = _make_win32_calls(user32, kernel32=None)
        calls.send_key(1, "delete")
        delete_vk = user32.calls[0][0][0]
        calls.send_key(1, "backspace")
        backspace_vk = user32.calls[1][0][0]
        assert delete_vk != backspace_vk

    def test_send_input_short_count_raises_runtime_error_not_a_silent_ok(self) -> None:
        # #3639 blocking review finding: `SendInput`'s return value used to
        # be discarded entirely — a short count (UIPI-blocked input) must
        # now raise rather than report success.
        user32 = _FakeUser32SendInput(sent_count=0)
        calls = _make_win32_calls(user32, kernel32=None)
        with pytest.raises(WinNativeRuntimeError, match="SendInput"):
            calls.send_key(1, "a")


class _FakeKernel32ProcessTree:
    """``CreateToolhelp32Snapshot``/``Process32First``/``Process32Next``
    stand-in that replays a scripted list of ``(pid, parent_pid,
    exe_name)`` rows — enough for :meth:`Win32Calls._snapshot_processes`
    and :meth:`Win32Calls._descendant_pids` (#3542) to walk a fake process
    tree exactly the way they'd walk a real one."""

    def __init__(self, processes: list[tuple[int, int, bytes]]) -> None:
        self._processes = processes
        self._iter = iter(())

    def CreateToolhelp32Snapshot(self, *_args, **_kwargs):
        return 1  # non-zero, non -1 "handle"

    def Process32First(self, _snapshot, entry_ref) -> bool:
        self._iter = iter(self._processes)
        return self._advance(entry_ref)

    def Process32Next(self, _snapshot, entry_ref) -> bool:
        return self._advance(entry_ref)

    def _advance(self, entry_ref) -> bool:
        try:
            pid, ppid, name = next(self._iter)
        except StopIteration:
            return False
        entry = ctypes.cast(entry_ref, ctypes.POINTER(_ProcessEntry32Mirror)).contents
        entry.th32ProcessID = pid
        entry.th32ParentProcessID = ppid
        entry.szExeFile = name
        return True

    def CloseHandle(self, _handle) -> None:
        pass


class _FakeUser32Windows:
    """``EnumWindows``/``GetWindowThreadProcessId``/``IsWindowVisible``
    stand-in: *windows* maps a fake ``hwnd`` to ``(owner_pid, visible)``.
    ``EnumWindows`` replays them in insertion order and stops as soon as
    the real callback returns ``False`` (a match), exactly like the real
    Win32 ``EnumWindows`` short-circuiting on its callback's return value.
    """

    def __init__(self, windows: dict[int, tuple[int, bool]]) -> None:
        self._windows = windows

    def EnumWindows(self, callback, lparam) -> None:
        for hwnd, (_owner_pid, _visible) in self._windows.items():
            if not callback(hwnd, lparam):
                break

    def GetWindowThreadProcessId(self, hwnd, owner_pid_ref) -> None:
        owner_pid_ref._obj.value = self._windows[hwnd][0]

    def IsWindowVisible(self, hwnd) -> bool:
        return self._windows[hwnd][1]


class TestFindTopWindowFollowsDescendantProcesses:
    """#3542: ``Win32Calls.launch()`` is ``subprocess.Popen(command,
    shell=True)``, which on Windows always spawns ``cmd.exe`` as the
    immediate child and returns *its* pid — the real GUI app
    (``vimcode.exe``) is a grandchild with a different pid, and ``cmd.exe``
    itself never owns a window. These tests reproduce that exact shape with
    a fake process tree + fake window table and confirm
    ``find_top_window`` now searches the whole descendant tree rather than
    matching the returned pid alone."""

    def test_follows_shell_wrapped_launch_to_the_real_apps_window(self) -> None:
        cmd_pid, vimcode_pid = 4242, 4321
        kernel32 = _FakeKernel32ProcessTree([
            (1, 0, b"System"),
            (cmd_pid, 1, b"cmd.exe"),
            (vimcode_pid, cmd_pid, b"vimcode.exe"),
        ])
        # cmd.exe (cmd_pid) owns no window at all — only its child does.
        user32 = _FakeUser32Windows({777: (vimcode_pid, True)})
        calls = _make_win32_calls(user32, kernel32)
        assert calls.find_top_window(cmd_pid, timeout_s=1.0) == 777

    def test_follows_a_grandchild_process_two_levels_deep(self) -> None:
        wt_pid, conhost_pid, app_pid = 10, 20, 30
        kernel32 = _FakeKernel32ProcessTree([
            (wt_pid, 1, b"wt.exe"),
            (conhost_pid, wt_pid, b"OpenConsole.exe"),
            (app_pid, conhost_pid, b"vimcode.exe"),
        ])
        user32 = _FakeUser32Windows({99: (app_pid, True)})
        calls = _make_win32_calls(user32, kernel32)
        assert calls.find_top_window(wt_pid, timeout_s=1.0) == 99

    def test_still_matches_when_the_launched_pid_owns_the_window_directly(
        self,
    ) -> None:
        """Backward-compatible: an exe launched without an intervening
        shell still has its own pid in its own descendant set (the root is
        always included), so the pre-#3542 direct-match case keeps
        working."""
        pid = 555
        kernel32 = _FakeKernel32ProcessTree([(pid, 1, b"vimcode.exe")])
        user32 = _FakeUser32Windows({1: (pid, True)})
        calls = _make_win32_calls(user32, kernel32)
        assert calls.find_top_window(pid, timeout_s=1.0) == 1

    def test_ignores_invisible_windows_even_on_a_matching_descendant(
        self,
    ) -> None:
        cmd_pid, vimcode_pid = 1, 2
        kernel32 = _FakeKernel32ProcessTree([
            (cmd_pid, 0, b"cmd.exe"), (vimcode_pid, cmd_pid, b"vimcode.exe"),
        ])
        user32 = _FakeUser32Windows({9: (vimcode_pid, False)})
        calls = _make_win32_calls(user32, kernel32)
        with pytest.raises(WinNativeRuntimeError, match="no visible top-level window"):
            calls.find_top_window(cmd_pid, timeout_s=0.05)

    def test_raises_when_no_descendant_owns_any_window(self) -> None:
        cmd_pid = 1
        kernel32 = _FakeKernel32ProcessTree([(cmd_pid, 0, b"cmd.exe")])
        user32 = _FakeUser32Windows({})
        calls = _make_win32_calls(user32, kernel32)
        with pytest.raises(WinNativeRuntimeError, match=f"pid={cmd_pid}"):
            calls.find_top_window(cmd_pid, timeout_s=0.05)

    def test_unrelated_processes_window_is_not_matched(self) -> None:
        """A visible window owned by some other, unrelated process must
        never be treated as a match just because it exists."""
        cmd_pid, vimcode_pid, unrelated_pid = 1, 2, 999
        kernel32 = _FakeKernel32ProcessTree([
            (cmd_pid, 0, b"cmd.exe"), (vimcode_pid, cmd_pid, b"vimcode.exe"),
        ])
        user32 = _FakeUser32Windows({8: (unrelated_pid, True)})
        calls = _make_win32_calls(user32, kernel32)
        with pytest.raises(WinNativeRuntimeError):
            calls.find_top_window(cmd_pid, timeout_s=0.05)


class TestDescendantPids:
    def test_includes_root_and_all_transitive_children(self) -> None:
        kernel32 = _FakeKernel32ProcessTree([
            (1, 0, b"System"),
            (10, 1, b"cmd.exe"),
            (20, 10, b"vimcode.exe"),
            (30, 20, b"helper.exe"),
            (999, 1, b"unrelated.exe"),
        ])
        calls = _make_win32_calls(_FakeUser32Windows({}), kernel32)
        assert calls._descendant_pids(10) == {10, 20, 30}

    def test_pid_with_no_children_returns_itself_only(self) -> None:
        kernel32 = _FakeKernel32ProcessTree([(10, 1, b"vimcode.exe")])
        calls = _make_win32_calls(_FakeUser32Windows({}), kernel32)
        assert calls._descendant_pids(10) == {10}


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


# ── UNC cwd (#3543) ──────────────────────────────────────────────────────────


class TestIsUncPath:
    def test_unc_path_is_detected(self) -> None:
        assert _is_unc_path(r"\\wsl.localhost\Ubuntu-24.04\home\me\repo")

    def test_drive_letter_path_is_not_unc(self) -> None:
        assert not _is_unc_path(r"C:\Users\me\repo")

    def test_empty_string_is_not_unc(self) -> None:
        assert not _is_unc_path("")


class TestIsRootedOrDriveQualified:
    r""":func:`_is_rooted_or_drive_qualified` — #3633 CI: the
    version-independent stand-in for `ntpath.isabs`, which Python 3.13
    narrowed so that a `/`-rooted token (`/home/me/x.exe`, the WSL shape
    #3633 normalizes and stages) is `True` on 3.12 and `False` on 3.13.

    These assertions are deliberately spelled out against literal path
    strings rather than deferred to `ntpath`, so they pin ONE answer on
    every supported interpreter — which is the whole point of the
    function."""

    @pytest.mark.parametrize(
        "path",
        [
            "/home/me/.coord/cargo-target/vimcode/release/vimcode.exe",
            "/home/me",
            "/",
            r"\home\me\vimcode.exe",
            r"\\wsl.localhost\Ubuntu-24.04\home\me\vimcode.exe",
            "//wsl.localhost/Ubuntu-24.04/home/me/vimcode.exe",
            r"C:\Tools\vimcode.exe",
            "C:/Tools/vimcode.exe",
            # Drive-relative (no root): `ntpath.isabs` is False on BOTH
            # 3.12 and 3.13, but it is not cwd-relative either and
            # `ntpath.join` treats it specially, so the relative-exe
            # branch must not see it.
            r"C:vimcode.exe",
        ],
    )
    def test_non_relative_shapes(self, path: str) -> None:
        assert _is_rooted_or_drive_qualified(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "vimcode.exe",
            r"target\release\vimcode.exe",
            "target/release/vimcode.exe",
            r"..\target\release\vimcode.exe",
            "../target/release/vimcode.exe",
            ".",
            "",
        ],
    )
    def test_genuinely_relative_shapes(self, path: str) -> None:
        assert _is_rooted_or_drive_qualified(path) is False

    def test_it_does_not_agree_with_ntpath_isabs_on_the_3_13_split(self) -> None:
        """The regression this function exists for, stated directly: a
        `/`-rooted token's classification must NOT be whatever this
        interpreter's `ntpath.isabs` happens to say about it (3.12 says
        True, 3.13 says False) — it must be `True` either way."""
        posix_token = "/home/me/.coord/cargo-target/vimcode/release/vimcode.exe"
        assert _is_rooted_or_drive_qualified(posix_token) is True
        # Whichever way this interpreter answers, our own answer stood.
        assert ntpath.isabs(posix_token) in (True, False)


class TestGetDriveType:
    """:func:`_get_drive_type` — the injectable `GetDriveTypeW` seam
    #3633 widens `_is_remote_exe_token` with. The real `ctypes.windll`
    call only exists on Windows; off it (this worktree runs on Linux)
    it must return `0` (`DRIVE_UNKNOWN`) rather than raise or guess."""

    def test_non_windows_returns_unknown(self) -> None:
        assert _get_drive_type(r"Z:\home\me\repo") == 0

    def test_a_unc_path_with_no_drive_letter_returns_unknown(self) -> None:
        assert _get_drive_type(r"\\wsl.localhost\Ubuntu\home\me") == 0


class TestNormalizePosixExeToken:
    r""":func:`_normalize_posix_exe_token` — #3633's fix option (b):
    rewrite a bare `/`-rooted WSL-style exe token onto the
    `\\wsl.localhost\<distro>\...` UNC shape, borrowing the distro name
    from `cwd` rather than fabricating one."""

    UNC_CWD = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"

    def test_borrows_the_distro_prefix_from_cwd(self) -> None:
        normalized = _normalize_posix_exe_token(
            "/home/me/.coord/cargo-target/vimcode/release/vimcode.exe", self.UNC_CWD,
        )
        assert normalized == (
            r"\\wsl.localhost\Ubuntu-24.04\home\me\.coord\cargo-target\vimcode"
            r"\release\vimcode.exe"
        )

    def test_returns_none_when_cwd_has_no_wsl_localhost_prefix(self) -> None:
        r"""A same-host, non-WSL UNC share (`\\fileserver\tools\...`) has
        no distro to borrow — nothing to safely guess."""
        assert _normalize_posix_exe_token(
            "/home/me/app.exe", r"\\fileserver\tools\repo",
        ) is None

    def test_returns_none_for_a_non_unc_cwd(self) -> None:
        assert _normalize_posix_exe_token("/home/me/app.exe", r"C:\Users\me\repo") is None


class TestIsRemoteExeToken:
    r""":func:`_is_remote_exe_token` — #3633: the widened "is this exe
    reachable only over the network" predicate the review asked for,
    replacing the pre-fix `_is_unc_path`-only check that mislabelled a
    mapped network drive (`Z:\...`, dell64's own observed shape) as
    "already genuinely local"."""

    def test_a_unc_path_is_remote(self) -> None:
        assert _is_remote_exe_token(r"\\wsl.localhost\Ubuntu-24.04\home\me\app.exe")

    def test_a_mapped_drive_letter_is_remote_via_injected_drive_type(self) -> None:
        assert _is_remote_exe_token(
            r"Z:\home\me\.coord\cargo-target\vimcode\release\vimcode.exe",
            get_drive_type=lambda _path: _DRIVE_REMOTE,
        )

    def test_a_fixed_drive_letter_is_not_remote(self) -> None:
        _DRIVE_FIXED = 3
        assert not _is_remote_exe_token(
            r"C:\Tools\vimcode.exe", get_drive_type=lambda _path: _DRIVE_FIXED,
        )

    def test_drive_type_unknown_is_not_remote(self) -> None:
        assert not _is_remote_exe_token(
            r"C:\Tools\vimcode.exe", get_drive_type=lambda _path: 0,
        )


class TestPopenCommandAndCwd:
    """#3543: `translate_to_windows_path` renders a WSL-hosted repo's `cwd`
    as a UNC path (`\\wsl.localhost\\...`), but `cmd.exe` — what
    `subprocess.Popen(..., shell=True)` always launches on Windows —
    categorically refuses a UNC current directory at its own startup and
    silently falls back to `%windir%`, breaking every relative path in the
    launched command. These are the "fails first" unit-level reproduction
    the issue's acceptance bar asks for: without the `pushd` fold-in below,
    `_popen_command_and_cwd` would hand `Popen` a `cwd=` cmd.exe cannot
    use."""

    def test_unc_cwd_is_folded_into_a_pushd_prefix_and_cwd_is_cleared(self) -> None:
        unc = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"
        command, cwd = _popen_command_and_cwd("../target/app.exe sample.txt", unc)
        assert command == f'pushd "{unc}" && ../target/app.exe sample.txt'
        # Never hand cmd.exe a UNC `cwd=` — it refuses it regardless of
        # what the command string itself does.
        assert cwd is None

    def test_drive_letter_cwd_passes_through_unchanged(self) -> None:
        command, cwd = _popen_command_and_cwd("app.exe", r"C:\Users\me\repo")
        assert command == "app.exe"
        assert cwd == r"C:\Users\me\repo"

    def test_empty_cwd_passes_through_as_none(self) -> None:
        command, cwd = _popen_command_and_cwd("app.exe", "")
        assert command == "app.exe"
        assert cwd is None


class TestWin32CallsLaunchAvoidsUncCwd:
    """#3543: `Win32Calls.launch`/`launch_in_terminal` must never hand
    `subprocess.Popen(..., shell=True)` a UNC `cwd=` — reproduces the exact
    bridge-to-Windows shape (`run_native_spec_via_bridge` translates this
    repo's WSL-hosted worktree to a UNC path and hands it to `launch`)
    against a scripted fake `subprocess.Popen`, since the real cmd.exe
    UNC-refusal behaviour can only be observed on a real Windows host."""

    def test_launch_never_passes_a_unc_cwd_to_popen(self, monkeypatch) -> None:
        unc = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        captured: dict = {}

        class _FakeProc:
            pid = 4242

        def fake_popen(command, *, shell, cwd=None, **kwargs):
            captured["command"] = command
            captured["cwd"] = cwd
            captured["shell"] = shell
            return _FakeProc()

        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", fake_popen,
        )
        pid = calls.launch("cd .smoke && ../target/app.exe sample.txt", unc)
        assert pid == 4242
        assert captured["shell"] is True
        assert captured["cwd"] is None  # never a UNC cwd handed to Popen
        assert captured["command"] == (
            f'pushd "{unc}" && cd .smoke && ../target/app.exe sample.txt'
        )

    def test_launch_in_terminal_never_passes_a_unc_cwd_to_popen(
        self, monkeypatch,
    ) -> None:
        unc = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        captured: dict = {}

        class _FakeProc:
            pid = 9999

        def fake_popen(command, *, shell, cwd=None, **kwargs):
            captured["command"] = command
            captured["cwd"] = cwd
            return _FakeProc()

        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", fake_popen,
        )
        pid = calls.launch_in_terminal("app.exe", unc, "windows-terminal")
        assert pid == 9999
        assert captured["cwd"] is None
        assert captured["command"] == f'pushd "{unc}" && wt.exe app.exe'

    def test_launch_with_drive_letter_cwd_still_uses_popens_cwd(
        self, monkeypatch,
    ) -> None:
        """Backward-compatible: the common (non-WSL) case keeps relying on
        `Popen`'s own `cwd=`, not a `pushd` prefix."""
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        captured: dict = {}

        class _FakeProc:
            pid = 1

        def fake_popen(command, *, shell, cwd=None, **kwargs):
            captured["command"] = command
            captured["cwd"] = cwd
            return _FakeProc()

        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", fake_popen,
        )
        calls.launch("app.exe", r"C:\Users\me\repo")
        assert captured["command"] == "app.exe"
        assert captured["cwd"] == r"C:\Users\me\repo"


# ── #3617: local-filesystem launch staging ──────────────────────────────────


class TestPlanStaging:
    """:func:`_plan_staging` — pure Windows-path string math, no real
    filesystem I/O, so these use fabricated path strings directly (works
    identically on any host OS, since `ntpath` is a pure algorithm)."""

    UNC = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"
    SESSION = r"C:\Users\me\AppData\Local\Temp\coord-app-drive\abc123"

    def test_cd_fixture_dir_then_relative_exe_is_staged(self) -> None:
        """The real coordinator.yml shape (#3617's own motivating case,
        also covered pre-existing-ly by
        `TestWin32CallsLaunchAvoidsUncCwd`'s `cd .smoke && ...`): `cwd`
        moves to the session root itself — NEVER `session_root/.smoke`
        (#3617 review: that was the double-`cd` bug — `command` still
        carries its own unmodified `cd .smoke && ...` prefix, which is
        what actually navigates into the staged fixture dir once launched
        from `cwd`) — the exe is staged at the SAME path relative to the
        new session root that it held relative to the old (UNC) `cwd`,
        and the fixture dir itself is queued for a wholesale copy."""
        plan = _plan_staging(
            "cd .smoke && ../target/release/vimcode.exe sample.txt",
            self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is True
        assert plan.cwd == self.SESSION
        assert plan.source_exe == f"{self.UNC}\\target\\release\\vimcode.exe"
        assert plan.dest_exe == f"{self.SESSION}\\target\\release\\vimcode.exe"
        assert (f"{self.UNC}\\.smoke", f"{self.SESSION}\\.smoke") in plan.fixture_copies

    def test_an_absolute_cd_dir_is_left_alone(self) -> None:
        """#3617 review nit: `cd C:\\foo && app.exe` would otherwise
        collapse `ntpath.join(session_root, cd_dir)` down to `cd_dir`
        alone, discarding `session_root` entirely and landing
        `source_exe`/`dest_exe` on the exact same absolute path (a
        `shutil.copy2` `SameFileError`)."""
        plan = _plan_staging(
            r"cd C:\foo && app.exe", self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is False

    def test_bare_exe_with_no_cd_prefix_is_staged_relative_to_cwd(self) -> None:
        """No `cd` at all: the exe is resolved directly against `cwd`,
        `cwd` itself becomes the session root unchanged in shape, and the
        conventional `.smoke` dir is still queued (opportunistically,
        whether or not the command ever names it) in case the exe reads
        it without a `cd`."""
        plan = _plan_staging("vimcode.exe sample.txt", self.UNC, session_root=self.SESSION)
        assert plan.staged is True
        assert plan.cwd == self.SESSION
        assert plan.source_exe == f"{self.UNC}\\vimcode.exe"
        assert plan.dest_exe == f"{self.SESSION}\\vimcode.exe"
        assert (f"{self.UNC}\\.smoke", f"{self.SESSION}\\.smoke") in plan.fixture_copies

    def test_quoted_exe_with_embedded_space_is_staged(self) -> None:
        plan = _plan_staging('"My App.exe" sample.txt', self.UNC, session_root=self.SESSION)
        assert plan.staged is True
        assert plan.source_exe == f"{self.UNC}\\My App.exe"
        assert plan.dest_exe == f"{self.SESSION}\\My App.exe"

    def test_non_unc_cwd_is_never_staged(self) -> None:
        """A same-host (non-WSL) `win-native` agent's `cwd` is already
        local — nothing to stage, and this driver must not even attempt
        to resolve `%LOCALAPPDATA%` for it."""
        plan = _plan_staging("vimcode.exe", r"C:\Users\me\repo", session_root=self.SESSION)
        assert plan.staged is False

    def test_a_second_shell_operator_is_left_alone(self) -> None:
        """Anything beyond the ONE recognized `cd <dir> && ` prefix is an
        opaque shell pipeline this driver will not guess at rewriting."""
        plan = _plan_staging(
            "cd .smoke && a.exe && b.exe", self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is False

    def test_a_pipe_or_redirection_is_left_alone(self) -> None:
        for command in ("a.exe | b.exe", "a.exe > out.txt", "a.exe & b.exe"):
            assert _plan_staging(command, self.UNC, session_root=self.SESSION).staged is False

    def test_a_genuinely_local_absolute_exe_token_is_left_alone(self) -> None:
        """Nothing under `cwd`'s own tree to stage for an exe that's
        already an absolute, LOCAL path — left alone rather than guessed
        at."""
        plan = _plan_staging(
            r"C:\Tools\vimcode.exe sample.txt", self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is False

    def test_an_absolute_unc_exe_token_is_staged_by_basename(self) -> None:
        """#3633: a route's relative `../target/.../X.exe` doesn't exist
        under this fleet's actual (shared) `CARGO_TARGET_DIR`, so a worker
        substitutes the real, absolute build path instead — one the
        Windows side still only ever reaches as a UNC path. That still
        pays the same `\\wsl$` 9P cost staging exists to avoid, so it must
        be staged too, not left alone the way a genuinely local absolute
        exe is. Staged by basename alone (an absolute token carries no
        position relative to `cwd` to preserve), and `command`'s own
        leading token is rewritten to the staged, quoted copy."""
        absolute_exe = (
            r"\\wsl.localhost\Ubuntu-24.04\home\me\.coord\cargo-target\vimcode"
            r"\x86_64-pc-windows-msvc\release\vimcode.exe"
        )
        plan = _plan_staging(
            f"{absolute_exe} sample.txt", self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is True
        assert plan.cwd == self.SESSION
        assert plan.source_exe == absolute_exe
        assert plan.dest_exe == f"{self.SESSION}\\vimcode.exe"
        assert plan.command == f'"{self.SESSION}\\vimcode.exe" sample.txt'
        # The fixture dir is still queued opportunistically, exactly as
        # the relative-exe case does.
        assert (f"{self.UNC}\\.smoke", f"{self.SESSION}\\.smoke") in plan.fixture_copies

    def test_a_cd_prefix_then_absolute_unc_exe_is_staged_and_command_rewritten(
        self,
    ) -> None:
        """The same #3633 shape, but preceded by the fleet's own
        `cd <fixture-dir> && ` convention — the `cd` prefix is carried
        through unchanged ahead of the rewritten, staged exe token."""
        absolute_exe = (
            r"\\wsl.localhost\Ubuntu-24.04\home\me\.coord\cargo-target\vimcode"
            r"\x86_64-pc-windows-msvc\release\vimcode.exe"
        )
        plan = _plan_staging(
            f"cd .smoke && {absolute_exe} sample.txt", self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is True
        assert plan.cwd == self.SESSION
        assert plan.dest_exe == f"{self.SESSION}\\vimcode.exe"
        assert plan.command == f'cd ".smoke" && "{self.SESSION}\\vimcode.exe" sample.txt'
        assert (f"{self.UNC}\\.smoke", f"{self.SESSION}\\.smoke") in plan.fixture_copies

    def test_a_mapped_network_drive_exe_token_is_staged(self) -> None:
        """#3633's own dell64 evidence: every launch actually ran from
        `Z:\\home\\john\\.coord\\cargo-target\\...` — a drive letter
        MAPPED to the WSL tree (`net use Z: \\wsl$\\Ubuntu...`).
        `ntpath.isabs` is `True` for that and `_is_unc_path` is `False`,
        so the pre-fix check mislabelled it "already genuinely local" and
        never staged it — the one shape #3633's own bug report showed,
        reproduced here via the injected `get_drive_type` fake (the real
        `GetDriveTypeW` call only exists on Windows)."""
        mapped_drive_exe = r"Z:\home\john\.coord\cargo-target\vimcode\x86_64-pc-windows-msvc\release\vimcode.exe"
        plan = _plan_staging(
            f"{mapped_drive_exe} sample.txt", self.UNC, session_root=self.SESSION,
            get_drive_type=lambda _path: _DRIVE_REMOTE,
        )
        assert plan.staged is True
        assert plan.cwd == self.SESSION
        assert plan.source_exe == mapped_drive_exe
        assert plan.dest_exe == f"{self.SESSION}\\vimcode.exe"
        assert plan.command == f'"{self.SESSION}\\vimcode.exe" sample.txt'

    def test_a_fixed_drive_letter_exe_token_is_left_alone(self) -> None:
        """The same `Z:\\...` shape as above, but `get_drive_type` now
        reports a genuinely local (fixed) drive — nothing to stage."""
        _DRIVE_FIXED = 3
        plan = _plan_staging(
            r"Z:\Tools\vimcode.exe sample.txt", self.UNC, session_root=self.SESSION,
            get_drive_type=lambda _path: _DRIVE_FIXED,
        )
        assert plan.staged is False
        assert "already" in plan.skip_reason

    def test_a_posix_wsl_exe_token_is_normalized_and_staged(self) -> None:
        r"""#3633's fix option (b): a bare `/`-rooted WSL-style exe token
        that reached the Windows side unmapped/untranslated — normalized
        onto the `\\wsl.localhost\<distro>\...` UNC shape (borrowing
        `cwd`'s own distro prefix) and staged exactly like a UNC token."""
        posix_exe = "/home/me/.coord/cargo-target/vimcode/release/vimcode.exe"
        plan = _plan_staging(f"{posix_exe} sample.txt", self.UNC, session_root=self.SESSION)
        assert plan.staged is True
        assert plan.cwd == self.SESSION
        assert plan.source_exe == (
            r"\\wsl.localhost\Ubuntu-24.04\home\me\.coord\cargo-target\vimcode\release\vimcode.exe"
        )
        assert plan.dest_exe == f"{self.SESSION}\\vimcode.exe"
        assert plan.command == f'"{self.SESSION}\\vimcode.exe" sample.txt'

    def test_a_posix_exe_token_is_left_alone_without_a_wsl_distro_to_borrow(self) -> None:
        r"""When `cwd` itself isn't a `\\wsl.localhost\<distro>\...` UNC
        path (a same-host, non-WSL UNC share), there's no distro name to
        borrow for the `/`-rooted token — left alone rather than
        guessed at."""
        plan = _plan_staging(
            "/home/me/app.exe sample.txt", r"\\fileserver\tools\repo",
            session_root=self.SESSION,
        )
        assert plan.staged is False

    @pytest.mark.parametrize(
        "exe_token",
        [
            "/home/me/.coord/cargo-target/vimcode/release/vimcode.exe",
            r"\home\me\.coord\cargo-target\vimcode\release\vimcode.exe",
            "//wsl.localhost/Ubuntu-24.04/home/me/vimcode.exe",
            r"\\wsl.localhost\Ubuntu-24.04\home\me\vimcode.exe",
            r"C:\Tools\vimcode.exe",
            r"C:vimcode.exe",
        ],
    )
    def test_a_rooted_exe_token_never_stages_outside_the_session_root(
        self, exe_token: str,
    ) -> None:
        r"""#3633 CI regression. `_plan_staging` used `ntpath.isabs` to
        decide "is this token absolute?", and Python 3.13 narrowed that
        to mean drive-plus-root/UNC only — so on 3.13 (and 3.13 alone) a
        `/`-rooted WSL token fell past the absolute-exe branch into the
        *relative* one, where `ntpath.join(session_root, "\\home\\me\\…")`
        discards `session_root` wholesale and produced
        `dest_exe = C:\home\me\…`: the exe staged to the DRIVE ROOT,
        outside this session's own directory — the exact escape the
        `..`-guard exists to prevent, reached by a different route.

        Whatever any given rooted shape's verdict is, the invariant holds
        on every interpreter: if it stages at all, it stages INSIDE
        `session_root`."""
        plan = _plan_staging(
            f"{exe_token} sample.txt", self.UNC, session_root=self.SESSION,
            get_drive_type=lambda _path: _DRIVE_REMOTE,
        )
        if plan.staged:
            assert plan.dest_exe.startswith(f"{self.SESSION}\\")
            assert ntpath.basename(plan.dest_exe) == "vimcode.exe"

    def test_an_altsep_spelled_unc_exe_token_is_respelled_and_staged(self) -> None:
        r"""A UNC token written with forward slashes
        (`//wsl.localhost/Ubuntu/...`) is the same path in a different
        spelling — respelled onto `\\…` and staged, not handed to the
        `/`-rooted branch (which would graft a SECOND
        `\\wsl.localhost\<distro>` prefix in front of it)."""
        plan = _plan_staging(
            "//wsl.localhost/Ubuntu-24.04/home/me/vimcode.exe sample.txt",
            self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is True
        assert plan.source_exe == r"\\wsl.localhost\Ubuntu-24.04\home\me\vimcode.exe"
        assert plan.dest_exe == f"{self.SESSION}\\vimcode.exe"
        assert plan.command == f'"{self.SESSION}\\vimcode.exe" sample.txt'

    def test_a_root_relative_cd_prefix_is_left_alone(self) -> None:
        r"""`cd \foo && ...` is not cwd-relative, so joining it onto
        `session_root` would discard everything but the drive — skipped
        for the same reason `cd C:\foo && ...` is. (`ntpath.isabs` says
        `True` for it on 3.12 and `False` on 3.13, which is why the guard
        no longer asks `ntpath`.)"""
        plan = _plan_staging(
            r"cd \foo && vimcode.exe sample.txt", self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is False
        assert "not cwd-relative" in plan.skip_reason

    def test_relative_exe_plan_leaves_command_none_meaning_unchanged(self) -> None:
        """The common (relative-exe) case never sets `plan.command` —
        `None` is the caller's cue (`Win32Calls._stage_if_needed`) to keep
        using the *command* it already has, since it already resolves
        correctly against the staged `cwd` unchanged."""
        plan = _plan_staging("vimcode.exe sample.txt", self.UNC, session_root=self.SESSION)
        assert plan.command is None

    def test_empty_command_is_left_alone(self) -> None:
        assert _plan_staging("", self.UNC, session_root=self.SESSION).staged is False

    def test_an_exe_that_escapes_cwd_with_no_cd_to_cancel_it_is_left_alone(self) -> None:
        """A bare (no `cd` prefix) `../target/release/vimcode.exe` climbs
        ABOVE `cwd` itself — staged at the same offset from
        `session_root`, it would land OUTSIDE this session's own
        directory (in the shared parent every session's own root lives
        under), a leak `kill`'s session-scoped delete would never reach.
        Left alone rather than risked."""
        plan = _plan_staging(
            "../target/release/vimcode.exe sample.txt", self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is False

    def test_a_cd_dir_that_cancels_out_the_exes_escape_is_still_staged(self) -> None:
        """The actual motivating shape (`test_cd_fixture_dir_then_relative
        _exe_is_staged` above): the SAME `../target/...` exe token, but
        with a `cd .smoke` ahead of it that cancels the `..` back inside
        `cwd` — this one must still stage, distinguishing "escapes `cwd`
        entirely" from "escapes the post-`cd` directory but stays inside
        `cwd`"."""
        plan = _plan_staging(
            "cd .smoke && ../target/release/vimcode.exe sample.txt",
            self.UNC, session_root=self.SESSION,
        )
        assert plan.staged is True


class TestStripCdPrefixAndLeadingToken:
    """The two small parsers :func:`_plan_staging` is built from."""

    def test_strip_cd_prefix_recognizes_the_convention(self) -> None:
        assert _strip_cd_prefix("cd .smoke && a.exe") == (".smoke", "a.exe")
        assert _strip_cd_prefix("CD .smoke && a.exe") == (".smoke", "a.exe")  # case-insensitive
        assert _strip_cd_prefix('cd "my dir" && a.exe') == ("my dir", "a.exe")

    def test_strip_cd_prefix_is_a_no_op_without_the_exact_shape(self) -> None:
        assert _strip_cd_prefix("a.exe") == ("", "a.exe")
        assert _strip_cd_prefix("echo cd .smoke && a.exe") == ("", "echo cd .smoke && a.exe")

    def test_leading_token_handles_quoting_and_bare_tokens(self) -> None:
        assert _leading_token('"My App.exe" sample.txt') == ("My App.exe", "sample.txt")
        assert _leading_token("vimcode.exe sample.txt") == ("vimcode.exe", "sample.txt")
        assert _leading_token("vimcode.exe") == ("vimcode.exe", "")
        assert _leading_token("   ") == ("", "")

    def test_looks_shell_composed(self) -> None:
        assert _looks_shell_composed("a.exe && b.exe") is True
        assert _looks_shell_composed("a.exe | b.exe") is True
        assert _looks_shell_composed("a.exe sample.txt") is False


class TestExecuteStaging:
    """:func:`_execute_staging` — the real filesystem side, exercised
    against plain local `tmp_path` directories (the function itself is
    OS-agnostic string/`os`/`shutil` plumbing; only :class:`Win32Calls`
    ever feeds it genuinely Windows-shaped strings, on real Windows)."""

    def test_copies_the_exe_and_every_fixture_dir_that_exists(self, tmp_path) -> None:
        source_exe_dir = tmp_path / "target" / "release"
        source_exe_dir.mkdir(parents=True)
        source_exe = source_exe_dir / "vimcode.exe"
        source_exe.write_text("binary")
        fixture_dir = tmp_path / ".smoke"
        fixture_dir.mkdir()
        (fixture_dir / "sample.txt").write_text("hi")
        (fixture_dir / "settings.json").write_text("{}")

        session = tmp_path / "session"
        plan = _StagingPlan(
            staged=True,
            cwd=str(session / ".smoke"),
            source_exe=str(source_exe),
            dest_exe=str(session / "target" / "release" / "vimcode.exe"),
            fixture_copies=((str(fixture_dir), str(session / ".smoke")),),
        )
        _execute_staging(plan)

        assert (session / "target" / "release" / "vimcode.exe").is_file()
        assert (session / ".smoke" / "sample.txt").read_text() == "hi"
        assert (session / ".smoke" / "settings.json").read_text() == "{}"

    def test_a_missing_fixture_dir_is_skipped_not_an_error(self, tmp_path) -> None:
        source_exe = tmp_path / "vimcode.exe"
        source_exe.write_text("binary")
        session = tmp_path / "session"
        plan = _StagingPlan(
            staged=True, cwd=str(session), source_exe=str(source_exe),
            dest_exe=str(session / "vimcode.exe"),
            fixture_copies=((str(tmp_path / "no-such-fixture-dir"), str(session / ".smoke")),),
        )
        _execute_staging(plan)  # must not raise
        assert (session / "vimcode.exe").is_file()
        assert not (session / ".smoke").exists()

    def test_a_missing_exe_raises(self, tmp_path) -> None:
        session = tmp_path / "session"
        plan = _StagingPlan(
            staged=True, cwd=str(session),
            source_exe=str(tmp_path / "no-such.exe"),
            dest_exe=str(session / "no-such.exe"),
        )
        with pytest.raises(WinNativeRuntimeError, match="exe not found"):
            _execute_staging(plan)

    def test_an_unstaged_plan_is_a_pure_no_op(self, tmp_path) -> None:
        calls_made = []

        def _boom(*a, **kw):
            calls_made.append((a, kw))
            raise AssertionError("must never touch the filesystem for an unstaged plan")

        _execute_staging(
            _StagingPlan(staged=False),
            isfile=_boom, makedirs=_boom, copy_file=_boom, copy_tree=_boom,
        )
        assert calls_made == []

    def test_an_absolute_token_plan_is_actually_copied_to_its_basename_dest(
        self, tmp_path,
    ) -> None:
        """#3633 review: the stubbed end-to-end test for the absolute-exe
        case (`TestWin32CallsLocalStaging
        .test_worker_substituted_absolute_unc_exe_is_staged_and_rewritten_end_to_end`)
        never actually calls `_execute_staging`, so nothing in the suite
        proved the absolute-token plan's copy really lands at
        `dest_exe = session_root\\<basename>` (no nested subdirectory,
        unlike the relative-exe case above). This exercises exactly that
        shape with real `tmp_path` I/O."""
        source_exe = tmp_path / "cargo-target" / "vimcode" / "release" / "vimcode.exe"
        source_exe.parent.mkdir(parents=True)
        source_exe.write_text("binary")
        session = tmp_path / "session"
        plan = _StagingPlan(
            staged=True, cwd=str(session),
            command=f'"{session / "vimcode.exe"}" sample.txt',
            source_exe=str(source_exe),
            dest_exe=str(session / "vimcode.exe"),
        )
        _execute_staging(plan)
        assert (session / "vimcode.exe").is_file()
        assert (session / "vimcode.exe").read_text() == "binary"

    def test_a_copy_that_reports_success_but_leaves_no_file_raises(self, tmp_path) -> None:
        """#3633 review (non-blocking): pre-copy, `_execute_staging` only
        ever checked `source_exe` — a `copy_file` that returns without
        raising but doesn't actually leave `dest_exe` behind degraded,
        pre-this-fix, straight into a confusing late `find_top_window`
        timeout once `command` had already been rewritten to point at
        it. A post-copy check must catch that here instead."""
        source_exe = tmp_path / "vimcode.exe"
        source_exe.write_text("binary")
        session = tmp_path / "session"
        plan = _StagingPlan(
            staged=True, cwd=str(session), source_exe=str(source_exe),
            dest_exe=str(session / "vimcode.exe"),
        )
        with pytest.raises(WinNativeRuntimeError, match="isn't there afterward"):
            _execute_staging(plan, copy_file=lambda *_a, **_kw: None)


class TestWin32CallsLocalStaging:
    """:class:`Win32Calls`'s own wiring of the #3617 staging helpers into
    `launch`/`launch_in_terminal`/`kill` — the planning/execution logic
    itself is `TestPlanStaging`/`TestExecuteStaging`'s job; these confirm
    `Win32Calls` actually calls them, uses their result for `Popen`, and
    cleans the staged directory up on `kill`."""

    UNC = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"

    def _fake_popen(self, captured: dict):
        class _FakeProc:
            pid = 7777

        def fake_popen(command, *, shell, cwd=None, env=None, **kwargs):
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env
            return _FakeProc()

        return fake_popen

    def test_launch_stages_and_rewrites_cwd_when_localappdata_is_set(
        self, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        captured_plan_args: dict = {}
        executed: list = []

        def fake_plan_staging(command, cwd, *, session_root):
            captured_plan_args["command"] = command
            captured_plan_args["cwd"] = cwd
            captured_plan_args["session_root"] = session_root
            # Mirrors the real planner's own shape (`cwd` IS the session
            # root — `command` keeps its own `cd .smoke &&`, which does
            # the navigating). This test only proves `Win32Calls` USES
            # whatever plan it is handed; that the real planner produces
            # this shape rather than the pre-review `session_root\.smoke`
            # one is `TestLaunchRealPlanStagingEndToEnd`'s job.
            return _StagingPlan(
                staged=True, cwd=session_root,
                source_exe=f"{cwd}\\target\\vimcode.exe",
                dest_exe=f"{session_root}\\target\\vimcode.exe",
            )

        def fake_execute_staging(plan):
            executed.append(plan)

        monkeypatch.setattr("coord.win_native_driver._plan_staging", fake_plan_staging)
        monkeypatch.setattr("coord.win_native_driver._execute_staging", fake_execute_staging)
        captured_popen: dict = {}
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(captured_popen),
        )

        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        pid = calls.launch("cd .smoke && vimcode.exe sample.txt", self.UNC)

        assert pid == 7777
        assert captured_plan_args["cwd"] == self.UNC
        assert len(executed) == 1  # staging was actually performed, not just planned
        # The STAGED cwd (a local path under the session root), not the
        # original UNC one, is what reached `Popen` — via `cwd=None` +
        # `_popen_command_and_cwd`'s own non-UNC passthrough, since the
        # staged cwd is a real local path, not a UNC one needing `pushd`.
        assert "coord-app-drive" in captured_plan_args["session_root"]
        assert captured_popen["cwd"] == captured_plan_args["session_root"]
        assert "wsl.localhost" not in captured_popen["cwd"]
        # The staged dir is tracked against the real PID `Popen` returned,
        # for `kill` to clean up later.
        assert calls._staged_session_dirs[7777] is not None

    def test_launch_isolates_appdata_to_the_staged_smoke_dir(
        self, monkeypatch, tmp_path,
    ) -> None:
        """#3637: the previously-documented lane setup step (a project-
        local ``.vimcode`` fixture next to the opened file) was silently
        ignored — a real Windows app reads its own settings from
        ``%APPDATA%\\<app>\\...`` unconditionally, never a cwd-relative
        folder. `launch` must therefore redirect the LAUNCHED process's
        own ``%APPDATA%`` to the staged ``.smoke`` directory, so a route
        that stages ``.smoke/vimcode/settings.json`` lands exactly where
        the real app looks — isolated per session, never the real, shared
        profile."""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

        def fake_plan_staging(command, cwd, *, session_root):
            return _StagingPlan(
                staged=True, cwd=session_root,
                source_exe=f"{cwd}\\target\\vimcode.exe",
                dest_exe=f"{session_root}\\target\\vimcode.exe",
            )

        monkeypatch.setattr("coord.win_native_driver._plan_staging", fake_plan_staging)
        monkeypatch.setattr("coord.win_native_driver._execute_staging", lambda plan: None)
        captured_popen: dict = {}
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(captured_popen),
        )

        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        calls.launch("cd .smoke && vimcode.exe sample.txt", self.UNC)

        env = captured_popen["env"]
        assert env is not None
        # Isolated to a directory UNDER this session's own staged root —
        # never the real, ambient %APPDATA% this process itself inherited.
        assert env["APPDATA"] != os.environ.get("APPDATA")
        assert env["APPDATA"].endswith(".smoke")
        assert ".smoke" in env["APPDATA"]
        assert os.path.isdir(env["APPDATA"])  # created even with nothing to copy
        # Everything else about the environment is passed through
        # unchanged — only %APPDATA% itself is redirected.
        for key, value in os.environ.items():
            if key != "APPDATA":
                assert env.get(key) == value

    def test_launch_falls_back_to_pushd_when_localappdata_is_unset(
        self, monkeypatch,
    ) -> None:
        """#3617 is a performance optimization, not a correctness
        requirement — when `%LOCALAPPDATA%` can't be resolved at all, a
        caller still gets a working (if UNC-slow) launch via the
        pre-#3617 `pushd` wrap, rather than this failing outright.

        Both resolution tiers are forced to fail EXPLICITLY: the env var
        is deleted, and `_staging_root`'s own `known_folder_resolver`
        kwarg (which exists for exactly this) is pinned to `lambda: None`.
        Deleting the env var alone would be platform-dependent — off
        Windows the `SHGetKnownFolderPath` fallback returns `None` only
        because `os.name != "nt"`, so on a real Windows host the resolver
        would succeed, `_plan_staging` would be reached and `_boom` would
        fire (#3617 review round 2)."""
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        real_staging_root = Win32Calls._staging_root
        monkeypatch.setattr(
            Win32Calls, "_staging_root",
            lambda self: real_staging_root(self, known_folder_resolver=lambda: None),
        )

        def _boom(*a, **kw):
            raise AssertionError("must never attempt to stage without %LOCALAPPDATA%")

        monkeypatch.setattr("coord.win_native_driver._plan_staging", _boom)
        captured_popen: dict = {}
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(captured_popen),
        )
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        calls.launch("cd .smoke && vimcode.exe sample.txt", self.UNC)
        assert captured_popen["command"] == f'pushd "{self.UNC}" && cd .smoke && vimcode.exe sample.txt'
        assert captured_popen["cwd"] is None
        # ... and the fallback is OBSERVABLE, never silent (#3617 review).
        assert calls.staging_warning is not None
        assert "LOCALAPPDATA" in calls.staging_warning
        # #3637: with no staged session to isolate %APPDATA% into, the
        # launched process keeps the ambient (unisolated) environment
        # rather than this silently claiming isolation it never performed
        # — `calls.staging_warning` (asserted above) is what makes this
        # observable rather than a quiet, unflagged regression.
        assert captured_popen["env"] is None

    def test_launch_in_terminal_also_stages(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

        def fake_plan_staging(command, cwd, *, session_root):
            return _StagingPlan(
                staged=True, cwd=session_root,
                source_exe=f"{cwd}\\vimcode.exe", dest_exe=f"{session_root}\\vimcode.exe",
            )

        monkeypatch.setattr("coord.win_native_driver._plan_staging", fake_plan_staging)
        monkeypatch.setattr("coord.win_native_driver._execute_staging", lambda plan: None)
        captured_popen: dict = {}
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(captured_popen),
        )
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        pid = calls.launch_in_terminal("vimcode.exe", self.UNC, "windows-terminal")
        assert pid == 7777
        assert captured_popen["command"].startswith("wt.exe vimcode.exe")
        assert "wsl.localhost" not in captured_popen["cwd"]

    def test_kill_deletes_the_staged_session_dir_it_tracked(self, tmp_path) -> None:
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        staged_dir = tmp_path / "session1"
        staged_dir.mkdir()
        (staged_dir / "vimcode.exe").write_text("binary")
        calls._staged_session_dirs[4242] = str(staged_dir)

        calls.kill(4242)

        assert not staged_dir.exists()
        assert 4242 not in calls._staged_session_dirs

    def test_kill_with_no_staged_dir_for_that_pid_is_a_no_op(self) -> None:
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        calls.kill(1)  # must not raise even with an empty tracking dict

    def test_kill_terminates_the_whole_descendant_tree_before_deleting(
        self, tmp_path,
    ) -> None:
        """#3617 review (non-blocking finding): a staged session's real
        exe is a GRANDCHILD of the pid `launch` returned (cmd.exe's own
        pid — #3542), which a bare `TerminateProcess(pid)` never reaches.
        Only when there's a staged dir to clean up, `kill` must terminate
        the WHOLE descendant tree first."""
        cmd_pid, vimcode_pid = 4242, 9999
        terminated: list[int] = []

        class _TrackingKernel32(_FakeKernel32ProcessTree):
            def OpenProcess(self, _access, _inherit, pid):
                return pid  # any non-zero/-1 "handle"

            def TerminateProcess(self, handle, _exit_code) -> None:
                terminated.append(handle)

        tracking_kernel32 = _TrackingKernel32([
            (cmd_pid, 1, b"cmd.exe"), (vimcode_pid, cmd_pid, b"vimcode.exe"),
        ])
        calls = _make_win32_calls(_FakeUser32(), tracking_kernel32)
        staged_dir = tmp_path / "session1"
        staged_dir.mkdir()
        calls._staged_session_dirs[cmd_pid] = str(staged_dir)

        calls.kill(cmd_pid)

        assert set(terminated) == {cmd_pid, vimcode_pid}
        assert not staged_dir.exists()

    def test_kill_without_a_staged_dir_still_terminates_the_whole_descendant_tree(
        self,
    ) -> None:
        """#3634: a 2026-10-05 bugbash lane showed `kill` leaving every
        grandchild (the real app under `cmd.exe`, #3542) running in the
        OVERWHELMINGLY COMMON (non-staged) case — `kill` used to walk
        `_descendant_pids` only when there was a staged directory to
        clean up afterward, as if that walk were solely in service of
        that cleanup rather than `kill`'s own general contract. It must
        now always terminate the whole tree, staged or not."""
        cmd_pid, vimcode_pid = 4242, 9999
        terminated: list[int] = []

        class _TrackingKernel32(_FakeKernel32ProcessTree):
            def OpenProcess(self, _access, _inherit, pid):
                return pid  # any non-zero/-1 "handle"

            def TerminateProcess(self, handle, _exit_code) -> None:
                terminated.append(handle)

        tracking_kernel32 = _TrackingKernel32([
            (cmd_pid, 1, b"cmd.exe"), (vimcode_pid, cmd_pid, b"vimcode.exe"),
        ])
        calls = _make_win32_calls(_FakeUser32(), tracking_kernel32)

        calls.kill(cmd_pid)  # no staged dir tracked for cmd_pid at all

        assert set(terminated) == {cmd_pid, vimcode_pid}

    def test_kill_closes_the_job_object_handle_assigned_at_launch(self) -> None:
        """#3634: when `launch`/`launch_in_terminal` successfully assigned
        *pid* to a ``KILL_ON_JOB_CLOSE`` Job Object, `kill` must close
        that SAME handle — the mechanism that lets Windows itself tear
        down the whole tree even if this process can no longer walk it."""
        closed: list[int] = []

        class _TrackingKernel32(_FakeKernel32NoSession):
            def CloseHandle(self, handle) -> None:
                closed.append(handle)

        calls = _make_win32_calls(_FakeUser32(), _TrackingKernel32())
        calls._job_handles[4242] = 0xABCD

        calls.kill(4242)

        assert 0xABCD in closed
        assert 4242 not in calls._job_handles


class TestLaunchRealPlanStagingEndToEnd:
    """#3617 review: the ONE integration test that previously covered
    `Win32Calls.launch`'s staging wiring (now
    `TestWin32CallsLocalStaging.test_launch_stages_and_rewrites_cwd_when_
    localappdata_is_set`) monkeypatched away `_plan_staging` itself, so it
    could never see a bug IN the plan `_plan_staging` actually produces —
    which is exactly how the double-`cd` defect (#3617 review finding 1)
    shipped undetected. This test drives the REAL `_plan_staging` (only
    `_execute_staging`'s filesystem side is stubbed, to avoid needing a
    real exe on disk) against the fleet's own real `win-native` `run:`
    shape and asserts on the EXACT ``(command, cwd)`` tuple that reaches
    ``Popen`` — this is the test that would have been red against the
    double-`cd` regression."""

    UNC = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"

    #: The fleet's actual `win-native` route `run:` string (per the #3617
    #: review finding, `~/.coord/coordinator.yml`'s own `routes:` entry —
    #: not reproducible here verbatim since that file lives outside this
    #: repo checkout, but this is its exact text).
    FLEET_RUN_COMMAND = "cd .smoke && ../target/x86_64-pc-windows-msvc/release/vimcode.exe sample.txt"

    def test_fleet_run_command_reaches_popen_as_a_single_working_cd(
        self, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        executed: list = []
        monkeypatch.setattr(
            "coord.win_native_driver._execute_staging", lambda plan: executed.append(plan),
        )
        captured_popen: dict = {}

        class _FakeProc:
            pid = 5555

        def fake_popen(command, *, shell, cwd=None, **kwargs):
            captured_popen["command"] = command
            captured_popen["cwd"] = cwd
            return _FakeProc()

        monkeypatch.setattr("coord.win_native_driver.subprocess.Popen", fake_popen)

        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        pid = calls.launch(self.FLEET_RUN_COMMAND, self.UNC)

        assert pid == 5555
        assert len(executed) == 1
        plan = executed[0]
        # The staged session root — NOT `session_root\.smoke` (the
        # double-`cd` bug: that would make `command`'s own `cd .smoke`
        # resolve to a `.smoke\.smoke` staging never creates, cmd.exe's
        # `cd` fail with a nonzero errorlevel, and `&&` short-circuit
        # before the exe ever runs).
        assert plan.cwd not in (None, "")
        assert not plan.cwd.endswith("\\.smoke")
        # `command` reaches `Popen` COMPLETELY UNCHANGED — its own `cd
        # .smoke && ` is what does the navigating, from `cwd`.
        assert captured_popen["command"] == self.FLEET_RUN_COMMAND
        assert captured_popen["cwd"] == plan.cwd
        assert "wsl.localhost" not in captured_popen["cwd"]
        # The staged exe's path, relative to the session root, is
        # IDENTICAL to the real exe's path relative to the original UNC
        # `cwd` — the whole point of staging "at the same offset".
        assert plan.dest_exe == ntpath.join(
            plan.cwd, "target", "x86_64-pc-windows-msvc", "release", "vimcode.exe",
        )
        # A `cmd.exe` actually given `captured_popen["command"]` from
        # `captured_popen["cwd"]` resolves exactly like this (mirroring
        # cmd.exe's own `cd`-then-relative-path semantics without needing
        # a real Windows host): `cd .smoke` -> `cwd\.smoke`, then
        # `../target/.../vimcode.exe` resolves back to
        # `cwd\target\...\vimcode.exe` — i.e. `plan.dest_exe` exactly.
        post_cd = ntpath.join(captured_popen["cwd"], ".smoke")
        resolved_exe = ntpath.normpath(
            ntpath.join(post_cd, "..", "target", "x86_64-pc-windows-msvc", "release", "vimcode.exe")
        )
        assert resolved_exe == ntpath.normpath(plan.dest_exe)

    def test_worker_substituted_absolute_unc_exe_is_staged_and_rewritten_end_to_end(
        self, monkeypatch, tmp_path,
    ) -> None:
        """#3633: when the route's own relative exe path doesn't exist
        (this fleet's actual `CARGO_TARGET_DIR` lives elsewhere) and a
        worker substitutes the real, absolute build path instead, that
        path reaching `Popen` must be the STAGED copy, not the original
        UNC one — unlike the relative-exe case above, this one DOES need
        `command` itself rewritten, since the absolute token carries no
        `cwd`-relative position to simply re-root."""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        executed: list = []
        monkeypatch.setattr(
            "coord.win_native_driver._execute_staging", lambda plan: executed.append(plan),
        )
        captured_popen: dict = {}

        class _FakeProc:
            pid = 6666

        def fake_popen(command, *, shell, cwd=None, **kwargs):
            captured_popen["command"] = command
            captured_popen["cwd"] = cwd
            return _FakeProc()

        monkeypatch.setattr("coord.win_native_driver.subprocess.Popen", fake_popen)

        worker_substituted_command = (
            "cd .smoke && "
            r"\\wsl.localhost\Ubuntu-24.04\home\me\.coord\cargo-target\vimcode"
            r"\x86_64-pc-windows-msvc\release\vimcode.exe sample.txt"
        )
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        pid = calls.launch(worker_substituted_command, self.UNC)

        assert pid == 6666
        assert len(executed) == 1
        plan = executed[0]
        assert "wsl.localhost" not in captured_popen["command"]
        assert captured_popen["command"] == plan.command
        assert captured_popen["command"] == f'cd ".smoke" && "{plan.dest_exe}" sample.txt'
        assert captured_popen["cwd"] == plan.cwd
        assert "wsl.localhost" not in captured_popen["cwd"]


class TestStageIfNeededFallsBackRatherThanRaising:
    """#3617 review (non-blocking finding): `_stage_if_needed`'s own
    docstring says staging is "a performance optimization, not a
    correctness requirement" — `_execute_staging` raising must therefore
    be caught and folded into a graceful fallback, not left to propagate
    and turn a previously-working (if UNC-slow) launch into a hard
    failure. Concretely: `cd .smoke && HOME=$PWD/home ../target/release/
    <exe> ...` (three of the fleet's four sibling routes) makes
    `_plan_staging`'s leading-token guess land on `HOME=$PWD/home`, which
    is never a real file — the resulting `_execute_staging` "exe not
    found" raise must not kill the launch."""

    UNC = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"

    def test_execute_staging_raising_falls_back_instead_of_propagating(
        self, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())

        command, cwd, staged_dir, appdata_dir = calls._stage_if_needed(
            "cd .smoke && HOME=$PWD/home ../target/release/vimcode.exe", self.UNC,
        )

        assert staged_dir is None
        assert appdata_dir is None
        assert command == "cd .smoke && HOME=$PWD/home ../target/release/vimcode.exe"
        assert cwd == self.UNC
        assert calls.staging_warning is not None
        assert "exe not found" in calls.staging_warning

    def test_unparseable_command_skips_staging_and_appdata_isolation(
        self, monkeypatch, tmp_path,
    ) -> None:
        """#3637: when `_plan_staging` itself declines (``plan.staged is
        False`` — here, a command with more than one shell operator), there
        is no staged session directory to point an isolated ``%APPDATA%``
        at either. `calls.staging_warning` must say so explicitly rather
        than silently claiming an isolation that never happened (#2096)."""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())

        command, cwd, staged_dir, appdata_dir = calls._stage_if_needed(
            "cd .smoke && a.exe && b.exe", self.UNC,
        )

        assert staged_dir is None
        assert appdata_dir is None
        assert command == "cd .smoke && a.exe && b.exe"
        assert cwd == self.UNC
        assert calls.staging_warning is not None
        assert "#3637" in calls.staging_warning


class TestStagingRootAndSweep:
    """:meth:`Win32Calls._staging_root`/:meth:`~Win32Calls._sweep_stale_sessions`
    — real `os` calls, but exercised against plain local `tmp_path`
    directories (this method only ever runs for real on Windows, where
    `os.path` already IS `ntpath` — nothing here is Windows-path-specific)."""

    def _calls(self):
        return _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())

    def test_staging_root_uses_localappdata_not_hardcoded(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        calls = self._calls()
        root = calls._staging_root()
        assert root == os.path.join(str(tmp_path), "Temp", "coord-app-drive")

    def test_staging_root_raises_when_localappdata_is_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        calls = self._calls()
        with pytest.raises(WinNativeRuntimeError, match="LOCALAPPDATA"):
            calls._staging_root()

    def test_sweep_deletes_only_sessions_older_than_the_max_age(
        self, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        calls = self._calls()
        root = calls._staging_root()
        os.makedirs(root)
        old_dir = os.path.join(root, "old-session")
        fresh_dir = os.path.join(root, "fresh-session")
        os.makedirs(old_dir)
        os.makedirs(fresh_dir)
        stale_time = time.time() - (_STALE_SESSION_MAX_AGE_S + 3600)
        os.utime(old_dir, (stale_time, stale_time))

        calls._sweep_stale_sessions()

        assert not os.path.exists(old_dir)
        assert os.path.exists(fresh_dir)

    def test_sweep_on_a_missing_root_is_a_no_op(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "does-not-exist-yet"))
        calls = self._calls()
        calls._sweep_stale_sessions()  # must not raise


class TestWin32CallsLaunchUnboundNeverTouchesSelfForNonUncPaths:
    """#3617 review: `launch`/`launch_in_terminal` must still be callable
    completely unbound (`self=None`) for a non-UNC `cwd` — the exact shape
    `TestLaunchPipeInheritanceRealSubprocess` already relies on — since
    the new staging lookup is gated on `_is_unc_path(cwd)` BEFORE `self`
    is ever touched."""

    def test_launch_unbound_with_non_unc_cwd_never_touches_self(self, monkeypatch) -> None:
        class _FakeProc:
            pid = 1

        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen",
            lambda *a, **kw: _FakeProc(),
        )
        assert Win32Calls.launch(None, "vimcode.exe", r"C:\Users\me\repo") == 1


class TestWin32CallsLaunchDoesNotInheritStdHandles:
    """#3544: `Win32Calls.launch`/`launch_in_terminal` must never hand
    `subprocess.Popen(..., shell=True)` its own inherited stdin/stdout/
    stderr — reproduces the exact bridge shape (the Windows-side bridge
    runner's own stdout is a pipe the WSL-side `subprocess.run(
    capture_output=True)` is reading; without an explicit redirect here,
    `cmd.exe` and the GUI exe it execs duplicate that same pipe handle and
    never let it see EOF, so the bridge call hangs for the full
    `bridge_timeout` and the launched exe is left orphaned — see
    `coord.win_native_driver._NO_HANDLE_INHERITANCE` and
    `coord.win_native_bridge.run_native_spec_via_bridge`). These two tests
    only characterize *which kwargs* reach a scripted fake `Popen` — they
    cannot demonstrate the redirect actually prevents the hang.
    `TestLaunchPipeInheritanceRealSubprocess` below does that with a real,
    unmocked OS pipe + subprocess tree; the real handle-inheritance hang on
    the exact Windows/WSL topology the issue reports can still only be
    observed on a real WSL<->Windows pairing (confirmed on dell64 per
    #3544's repro, not re-run against this fix)."""

    def test_launch_redirects_all_three_std_handles_to_devnull(
        self, monkeypatch,
    ) -> None:
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        captured: dict = {}

        class _FakeProc:
            pid = 4242

        def fake_popen(command, *, shell, cwd=None, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", fake_popen,
        )
        calls.launch("vimcode.exe sample.txt", r"C:\Users\me\repo")
        assert captured.get("stdin") is subprocess.DEVNULL
        assert captured.get("stdout") is subprocess.DEVNULL
        assert captured.get("stderr") is subprocess.DEVNULL

    def test_launch_in_terminal_redirects_all_three_std_handles_to_devnull(
        self, monkeypatch,
    ) -> None:
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())
        captured: dict = {}

        class _FakeProc:
            pid = 9999

        def fake_popen(command, *, shell, cwd=None, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", fake_popen,
        )
        calls.launch_in_terminal("vimcode.exe", r"C:\Users\me\repo", "windows-terminal")
        assert captured.get("stdin") is subprocess.DEVNULL
        assert captured.get("stdout") is subprocess.DEVNULL
        assert captured.get("stderr") is subprocess.DEVNULL


class _FakeKernel32JobObjects(_FakeKernel32NoSession):
    """Adds working Job Object calls (#3634) on top of
    `_FakeKernel32NoSession`'s baseline — `CreateJobObjectW`/
    `SetInformationJobObject`/`OpenProcess`/`AssignProcessToJobObject` all
    succeed by default, recording exactly what
    `Win32Calls._assign_kill_on_close_job` requested, so a test can assert
    #3634's `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is really what gets set —
    not just "some flag" — and that the real pid is what gets assigned.
    Each ``*_fails`` flag scripts exactly one step of the chain failing,
    to exercise `_assign_kill_on_close_job`'s best-effort fallback at
    every stage."""

    def __init__(
        self, *, create_fails: bool = False, set_info_fails: bool = False,
        open_process_fails: bool = False, assign_fails: bool = False,
    ) -> None:
        super().__init__()
        self._create_fails = create_fails
        self._set_info_fails = set_info_fails
        self._open_process_fails = open_process_fails
        self._assign_fails = assign_fails
        self.jobs_created: list[int] = []
        self.limit_flags_set: list[int] = []
        self.assigned: list[tuple[int, int]] = []
        self.closed: list[int] = []
        self._next_job = 100

    def CreateJobObjectW(self, _attrs, _name):
        if self._create_fails:
            return 0
        job = self._next_job
        self._next_job += 1
        self.jobs_created.append(job)
        return job

    def SetInformationJobObject(self, _job, _info_class, info_ptr, _size):
        if self._set_info_fails:
            return 0
        info = ctypes.cast(
            info_ptr, ctypes.POINTER(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION),
        ).contents
        self.limit_flags_set.append(info.BasicLimitInformation.LimitFlags)
        return 1

    def OpenProcess(self, _access, _inherit, pid):
        if self._open_process_fails:
            return 0
        return pid  # any non-zero "handle" tied to the real pid

    def AssignProcessToJobObject(self, job, process_handle):
        if self._assign_fails:
            return 0
        self.assigned.append((job, process_handle))
        return 1

    def CloseHandle(self, handle) -> None:
        self.closed.append(handle)


class TestAssignKillOnCloseJob:
    """#3634: `Win32Calls.launch`/`launch_in_terminal` assign the freshly
    launched pid to a ``KILL_ON_JOB_CLOSE`` Job Object, so Windows itself
    tears down the whole process tree (including a ``cmd.exe /c``
    grandchild, #3542) the instant the handle closes — even if this
    process never runs its own ``kill()`` at all (a crash/force-kill has
    no Windows ``PR_SET_PDEATHSIG`` equivalent to lean on instead, #3583).
    """

    @staticmethod
    def _fake_popen(pid: int):
        class _FakeProc:
            def __init__(self) -> None:
                self.pid = pid

        return lambda *a, **kw: _FakeProc()

    def test_launch_assigns_the_pid_to_a_kill_on_close_job(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(4242),
        )
        kernel32 = _FakeKernel32JobObjects()
        calls = _make_win32_calls(_FakeUser32(), kernel32)

        pid = calls.launch("vimcode.exe", r"C:\Users\me\repo")

        assert pid == 4242
        assert calls._job_handles[4242] == kernel32.jobs_created[0]
        assert kernel32.limit_flags_set == [JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE]
        assert kernel32.assigned == [(kernel32.jobs_created[0], 4242)]

    def test_launch_in_terminal_also_assigns_a_job(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(5151),
        )
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32JobObjects())

        pid = calls.launch_in_terminal(
            "vimcode.exe", r"C:\Users\me\repo", "windows-terminal",
        )

        assert pid == 5151
        assert 5151 in calls._job_handles

    def test_create_job_object_failing_is_best_effort(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(7),
        )
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32JobObjects(create_fails=True))

        pid = calls.launch("vimcode.exe", r"C:\Users\me\repo")

        assert pid == 7
        assert calls._job_handles == {}

    def test_set_information_failing_closes_the_job_and_is_best_effort(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(8),
        )
        kernel32 = _FakeKernel32JobObjects(set_info_fails=True)
        calls = _make_win32_calls(_FakeUser32(), kernel32)

        pid = calls.launch("vimcode.exe", r"C:\Users\me\repo")

        assert pid == 8
        assert calls._job_handles == {}
        assert kernel32.jobs_created[0] in kernel32.closed

    def test_open_process_failing_closes_the_job_and_is_best_effort(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(9),
        )
        kernel32 = _FakeKernel32JobObjects(open_process_fails=True)
        calls = _make_win32_calls(_FakeUser32(), kernel32)

        pid = calls.launch("vimcode.exe", r"C:\Users\me\repo")

        assert pid == 9
        assert calls._job_handles == {}
        assert kernel32.jobs_created[0] in kernel32.closed

    def test_assign_process_failing_closes_the_job_and_is_best_effort(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(10),
        )
        kernel32 = _FakeKernel32JobObjects(assign_fails=True)
        calls = _make_win32_calls(_FakeUser32(), kernel32)

        pid = calls.launch("vimcode.exe", r"C:\Users\me\repo")

        assert pid == 10
        assert calls._job_handles == {}
        assert kernel32.jobs_created[0] in kernel32.closed

    def test_no_job_object_support_at_all_is_best_effort(self, monkeypatch) -> None:
        """A scripted fake kernel32 with no Job Object methods at all
        raises `AttributeError` — the exact shape of every OTHER fake in
        this module — and must not break `launch` at all; this is what
        keeps #3634 from regressing every pre-existing test using
        `_FakeKernel32NoSession`/`_FakeKernel32ProcessTree` directly."""
        monkeypatch.setattr(
            "coord.win_native_driver.subprocess.Popen", self._fake_popen(11),
        )
        calls = _make_win32_calls(_FakeUser32(), _FakeKernel32NoSession())

        pid = calls.launch("vimcode.exe", r"C:\Users\me\repo")

        assert pid == 11
        assert calls._job_handles == {}


class TestLaunchPipeInheritanceRealSubprocess:
    """#3544 review follow-up: a real, unmocked reproduction of the exact
    EOF-blocking mechanism the issue describes, using real OS pipes and a
    real subprocess tree instead of a scripted fake `Popen`.

    This is the closest in-repo stand-in for the issue's own mandatory
    acceptance line ("a Tier-1 shared conformance scenario or a Tier-2
    smoke-spec step that fails first, covering this exact behaviour") that
    this repo can host on its own: the actual Tier-2 `win-native` smoke
    spec (`tests/smoke-spec/win-gui.yaml`-style) lives in the *app* repo
    (vimcode) that declares the `win-native` acceptance driver, not here —
    `coord` only ships the driver engine, and this worktree runs on Linux
    with no real Windows/WSL pairing available. What *is* reproducible
    here, with no Windows dependency at all, is the general OS mechanism:
    an un-redirected standard handle can be inherited by a long-lived
    child and keep a reader from ever seeing EOF, independent of platform.
    That is the behaviour `Win32Calls.launch` must avoid, whatever the
    exact Win32-side inheritance rule turns out to be (see the review
    discussion captured in `_NO_HANDLE_INHERITANCE`'s own docstring, which
    is honest that the Windows mechanism remains unconfirmed on real
    hardware).

    - `test_shell_true_with_no_redirect_leaks_stdout_to_grandchild`
      reproduces the pre-fix shape in isolation (a bare
      `subprocess.Popen(cmd, shell=True)` with no stdin=/stdout=/stderr=,
      exactly what `Win32Calls.launch` did before #3544) and shows it
      really does let a long-lived grandchild hold the runner's own stdout
      pipe open past the runner's own exit — i.e. this test *fails first*
      against the pre-fix code shape (and would fail again if someone
      reintroduced it).
    - `test_real_launch_does_not_leak_stdout_to_grandchild` drives the
      actual production `Win32Calls.launch` (called unbound — it never
      touches `self`) through the identical pipe setup and asserts EOF
      arrives promptly even with its own long-lived grandchild still
      running: revert `_NO_HANDLE_INHERITANCE` and this test fails the
      same way the one above does.
    """

    @staticmethod
    def _read_until_eof_or_timeout(fd: int, timeout: float) -> tuple[bytes, bool]:
        """Read *fd* until EOF (empty read) or *timeout* seconds elapse.

        Returns ``(data_read, hit_eof)`` — ``hit_eof`` is `False` when the
        deadline passed with the pipe's write end still open (some writer
        — e.g. a leaked grandchild — is still holding it), `True` once a
        zero-length read confirms every write end has closed.
        """
        deadline = time.monotonic() + timeout
        data = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return data, False
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                return data, True
            data += chunk

    def test_shell_true_with_no_redirect_leaks_stdout_to_grandchild(self) -> None:
        """Pre-fix shape in isolation: a bare `Popen(cmd, shell=True)` with
        no std-handle redirection really does let a long-lived grandchild
        hold the parent's own stdout pipe open past the parent's exit."""
        r, w = os.pipe()
        try:
            runner = subprocess.Popen(
                [
                    sys.executable, "-c",
                    "import subprocess, sys\n"
                    "subprocess.Popen('sleep 2', shell=True)\n"
                    "sys.stdout.write('runner-done\\n')\n"
                    "sys.stdout.flush()\n",
                ],
                stdout=w,
            )
            os.close(w)
            w = -1
            assert runner.wait(timeout=10) == 0
            data, hit_eof = self._read_until_eof_or_timeout(r, timeout=0.8)
            assert data == b"runner-done\n"
            assert not hit_eof, (
                "expected the un-redirected grandchild to still be holding "
                "the pipe open at this point — if this starts passing, the "
                "repro no longer demonstrates the mechanism #3544 reports"
            )
        finally:
            if w != -1:
                os.close(w)
            os.close(r)

    def test_real_launch_does_not_leak_stdout_to_grandchild(self) -> None:
        """The actual fix: the real `Win32Calls.launch` (called unbound —
        it never touches `self`) must not let its own long-lived `sleep`
        grandchild hold the runner's stdout pipe open."""
        r, w = os.pipe()
        try:
            runner = subprocess.Popen(
                [
                    sys.executable, "-c",
                    "import sys\n"
                    "from coord.win_native_driver import Win32Calls\n"
                    "Win32Calls.launch(None, 'sleep 2', '.')\n"
                    "sys.stdout.write('runner-done\\n')\n"
                    "sys.stdout.flush()\n",
                ],
                stdout=w,
            )
            os.close(w)
            w = -1
            assert runner.wait(timeout=10) == 0
            data, hit_eof = self._read_until_eof_or_timeout(r, timeout=0.8)
            assert data == b"runner-done\n"
            assert hit_eof, (
                "pipe never hit EOF while the launched grandchild was "
                "still alive — Win32Calls.launch is leaking a standard "
                "handle to it again (#3544)"
            )
        finally:
            if w != -1:
                os.close(w)
            os.close(r)


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


class TestWinNativeSessionKillsOnOpenFailure:
    """#3634: a 2026-10-05 bugbash lane left 4 simultaneous stray
    ``vimcode.exe`` processes running because a failed/retried open never
    killed the process it had already launched — `WinNativeSession.__init__`
    raising (window discovery failing) left no session object for anyone to
    ever call `close()`/`kill()` on at all."""

    def test_window_never_appearing_kills_the_launched_pid_before_raising(self) -> None:
        calls = FakeWinCalls(window_never_appears=True)

        with pytest.raises(WinNativeRuntimeError):
            WinNativeSession("vimcode.exe", "/repo", calls=calls)

        assert calls.launched == [("vimcode.exe", "/repo")]
        assert calls.killed == [calls._next_pid]

    def test_move_window_failing_also_kills_the_launched_pid_before_raising(self) -> None:
        calls = FakeWinCalls()

        def _explode(hwnd, x, y, width, height):
            raise WinNativeRuntimeError("MoveWindow failed")

        calls.move_window = _explode  # type: ignore[method-assign]

        with pytest.raises(WinNativeRuntimeError):
            WinNativeSession("vimcode.exe", "/repo", calls=calls)

        assert calls.killed == [calls._next_pid]

    def test_kill_raising_during_open_failure_cleanup_does_not_mask_the_real_error(
        self,
    ) -> None:
        """Teardown-on-failure must itself never raise past the original
        error — the ORIGINAL `find_top_window` failure is what a caller
        needs to see, not a secondary problem in best-effort cleanup."""
        calls = FakeWinCalls(window_never_appears=True)

        def _explode(pid):
            raise OSError("kill itself failed")

        calls.kill = _explode  # type: ignore[method-assign]

        with pytest.raises(WinNativeRuntimeError, match="no window for pid"):
            WinNativeSession("vimcode.exe", "/repo", calls=calls)

    def test_successful_open_never_calls_kill(self) -> None:
        calls = FakeWinCalls()
        WinNativeSession("vimcode.exe", "/repo", calls=calls)
        assert calls.killed == []


class TestWinNativeSessionStagingWarning:
    """#3617 review: `WinNativeSession.staging_warning` is the seam
    :mod:`coord.app_drive_daemon` reads to put a skipped-staging warning
    in its ready file (and from there into
    :class:`coord.app_drive.SessionHandle`). It is a `getattr`-based read,
    so a rename on either side silently restores the pre-review silence —
    these are what make that rename fail instead.

    The rest of the chain (`serve()` -> ready file -> `SessionHandle` ->
    the on-disk session file) is covered by
    `tests/test_app_drive.py::TestStagingWarningPlumbing`."""

    def test_session_surfaces_the_calls_warning(self) -> None:
        calls = FakeWinCalls()
        calls.staging_warning = "staging skipped: no %LOCALAPPDATA%"
        session = WinNativeSession("vimcode.exe", "/repo", calls=calls)
        assert session.staging_warning == "staging skipped: no %LOCALAPPDATA%"

    def test_session_reports_none_when_staging_engaged(self) -> None:
        calls = FakeWinCalls()
        calls.staging_warning = None
        session = WinNativeSession("vimcode.exe", "/repo", calls=calls)
        assert session.staging_warning is None

    def test_session_reports_none_for_a_backend_without_the_attribute(self) -> None:
        """A scripted fake (and any non-`Win32Calls` implementation) has no
        `staging_warning` at all — must read as "no warning", never raise."""
        calls = FakeWinCalls()
        assert not hasattr(calls, "staging_warning")
        session = WinNativeSession("vimcode.exe", "/repo", calls=calls)
        assert session.staging_warning is None


# ── #3640: console-subsystem detection + WinNativeSession mode selection ────


def _pe_bytes(subsystem: int, *, magic: int = 0x10B) -> bytes:
    """A minimal, otherwise-garbage PE image carrying just enough real
    structure for :func:`coord.win_native_driver._pe_subsystem` to read
    *subsystem* back off it — the DOS stub's ``e_lfanew``, the ``PE\\0\\0``
    signature, a dummy (all-zero) ``IMAGE_FILE_HEADER``, and an optional
    header whose ``Magic``/``Subsystem`` fields are the only two actually
    populated. *magic* (``0x10b`` PE32 / ``0x20b`` PE32+) exercises both
    shapes — :func:`_pe_subsystem`'s own docstring explains why the
    ``Subsystem`` offset is identical in both."""
    dos_header = bytearray(64)
    dos_header[0:2] = b"MZ"
    pe_offset = 64
    struct.pack_into("<I", dos_header, 60, pe_offset)
    file_header = bytes(20)
    optional_header = bytearray(70)
    struct.pack_into("<H", optional_header, 0, magic)
    struct.pack_into("<H", optional_header, 68, subsystem)
    return bytes(dos_header) + b"PE\x00\x00" + file_header + bytes(optional_header)


class TestPeSubsystem:
    def test_reads_gui_subsystem_pe32(self, tmp_path) -> None:
        exe = tmp_path / "app.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_GUI, magic=0x10B))
        assert _pe_subsystem(str(exe)) == IMAGE_SUBSYSTEM_WINDOWS_GUI

    def test_reads_cui_subsystem_pe32(self, tmp_path) -> None:
        exe = tmp_path / "app.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_CUI, magic=0x10B))
        assert _pe_subsystem(str(exe)) == IMAGE_SUBSYSTEM_WINDOWS_CUI

    def test_reads_cui_subsystem_pe32_plus_at_the_same_offset(self, tmp_path) -> None:
        """#3640: the 64-bit (PE32+) optional header shape drops
        ``BaseOfData`` but widens ``ImageBase`` from 4 to 8 bytes — those
        two 4-byte deltas must cancel out, leaving ``Subsystem`` at the
        identical byte offset as PE32's."""
        exe = tmp_path / "app64.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_CUI, magic=0x20B))
        assert _pe_subsystem(str(exe)) == IMAGE_SUBSYSTEM_WINDOWS_CUI

    def test_missing_file_returns_none_not_raise(self) -> None:
        assert _pe_subsystem("/no/such/exe/anywhere.exe") is None

    def test_non_pe_file_returns_none(self, tmp_path) -> None:
        not_pe = tmp_path / "not-an-exe.exe"
        not_pe.write_bytes(b"this is not a PE image at all, just text padding" * 4)
        assert _pe_subsystem(str(not_pe)) is None

    def test_truncated_pe_returns_none(self, tmp_path) -> None:
        """A real ``MZ``/``PE`` signature but the file ends before the
        ``Subsystem`` field — must report "couldn't tell", never raise or
        read garbage past EOF."""
        truncated = tmp_path / "truncated.exe"
        truncated.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_CUI)[:80])
        assert _pe_subsystem(str(truncated)) is None


class TestGuessExePath:
    def test_extracts_the_only_exe_token(self) -> None:
        assert _guess_exe_path("vcd.exe sample.txt", "/cwd") == "/cwd/vcd.exe"

    def test_resolves_the_exe_token_relative_to_the_cd_prefixed_dir(self) -> None:
        """#3640's own reproduction: ``coord app-drive open win-native
        --launch "cd .smoke && ../target/x86_64-pc-windows-msvc/release/
        vcd.exe sample.txt"``. The exe is reachable as ``../target/...``
        only AFTER the ``cd .smoke`` — so the resolved path must be
        relative to ``cwd/.smoke``, never to ``cwd`` itself (the #3640
        review finding: the previous revision joined the token straight
        onto ``cwd``, landing one directory above the worktree)."""
        command = "cd .smoke && ../target/x86_64-pc-windows-msvc/release/vcd.exe sample.txt"
        token = "../target/x86_64-pc-windows-msvc/release/vcd.exe"
        assert _guess_exe_path(command, "/cwd") == f"/cwd/.smoke/{token}"

    def test_quoted_exe_with_embedded_space_resolves_the_whole_quoted_token(self) -> None:
        """#3640 review finding (b): a quoted path containing a space —
        the old regex-based extractor matched only ``App.exe``, the
        substring after the last space, where the shared
        ``_leading_token`` parser (correctly) returns the whole quoted
        token."""
        assert (
            _guess_exe_path('"My App.exe" sample.txt', "/cwd") == "/cwd/My App.exe"
        )

    def test_absolute_exe_token_is_returned_unchanged(self) -> None:
        assert _guess_exe_path(r"C:\tools\vimcode.exe", "/cwd") == r"C:\tools\vimcode.exe"

    def test_rooted_posix_style_exe_token_is_returned_unchanged_without_a_distro_to_borrow(
        self,
    ) -> None:
        """A ``/``-rooted token (a WSL-side absolute path, #3633's own
        shape) must not be joined onto *cwd* either — only a genuinely
        *cwd*-relative token should be. *cwd* here isn't a
        ``\\\\wsl.localhost\\...`` UNC path, so there's no distro prefix
        to normalize the token onto — left alone rather than guessed."""
        assert _guess_exe_path("/opt/tools/vcd.exe", "/cwd") == "/opt/tools/vcd.exe"

    def test_rooted_posix_style_exe_token_is_normalized_onto_the_cwd_distro(self) -> None:
        """#3640 review finding (c): a `/`-rooted WSL token must be
        normalized onto the ``\\\\wsl.localhost\\<distro>\\...`` UNC form
        *cwd* itself already carries — the shape the bridge host actually
        needs to read the file at all. Returning it verbatim (the
        previous revision's behavior) is always unreadable there, so the
        sniff silently abstained on every WSL-rooted token."""
        cwd = r"\\wsl.localhost\Ubuntu-24.04\home\me\repo"
        assert (
            _guess_exe_path("/home/me/other/vcd.exe", cwd)
            == r"\\wsl.localhost\Ubuntu-24.04\home\me\other\vcd.exe"
        )

    def test_no_exe_token_returns_none(self) -> None:
        assert _guess_exe_path("echo hello", "/cwd") is None

    def test_a_second_shell_operator_declines_to_guess(self) -> None:
        """Mirrors :func:`_plan_staging`'s own decline for the same
        shape — an opaque shell pipeline this driver will not guess at
        rewriting."""
        assert _guess_exe_path("a.exe && b.exe", "/cwd") is None

    def test_a_rooted_cd_prefix_declines_to_guess(self) -> None:
        """Mirrors :func:`_plan_staging`'s own decline for ``cd C:\\foo
        && ...`` — not *cwd*-relative, so there's nothing safe to join it
        onto."""
        assert _guess_exe_path(r"cd C:\foo && app.exe", "/cwd") is None


class TestDetectConsoleSubsystem:
    def test_true_for_a_cui_exe(self, tmp_path) -> None:
        exe = tmp_path / "vcd.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_CUI))
        assert _detect_console_subsystem(f"{exe} sample.txt", str(tmp_path)) is True

    def test_false_for_a_gui_exe(self, tmp_path) -> None:
        exe = tmp_path / "vimcode.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_GUI))
        assert _detect_console_subsystem(str(exe), str(tmp_path)) is False

    def test_none_when_exe_is_unreadable(self, tmp_path) -> None:
        assert _detect_console_subsystem("missing.exe", str(tmp_path)) is None

    def test_none_when_no_exe_token_in_command(self, tmp_path) -> None:
        assert _detect_console_subsystem("echo hello", str(tmp_path)) is None


class TestWinNativeSessionModeSelection:
    """#3640: `coord app-drive open win-native --launch '<path>/vcd.exe
    sample.txt'` hung forever because the ONLY launch path
    `WinNativeSession` had was the GUI one — it waits for a top-level
    window owned by the launched pid's own tree, which a console-
    subsystem exe's window never is (conhost.exe/Windows Terminal owns
    it instead). These exercise the fix at the one place both `coord
    app-drive open` and (indirectly, via `_build_backend`) the daemon
    share — no separate code path to drift out of sync with."""

    def test_auto_mode_launches_in_terminal_for_a_detected_console_exe(self, tmp_path) -> None:
        exe = tmp_path / "vcd.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_CUI))
        calls = FakeWinCalls()

        WinNativeSession(f"{exe} sample.txt", str(tmp_path), calls=calls)

        assert calls.launched == []
        assert calls.launched_in_terminal == [(f"{exe} sample.txt", str(tmp_path), "windows-terminal")]

    def test_auto_mode_launches_in_terminal_for_3640s_own_cd_prefixed_reproduction(
        self, tmp_path,
    ) -> None:
        """#3640's own reported reproduction, end to end, with the exe
        laid out exactly where the real worktree build puts it: a
        ``.smoke`` fixture dir and a sibling ``target/<triple>/release/``
        exe one level up. Pre-fix, ``_guess_exe_path`` resolved the exe
        token against *cwd* directly (ignoring the ``cd .smoke &&``
        prefix), landed one directory too high, found nothing, and this
        fell back to the broken ``mode="window"`` path — this test fails
        against that revision."""
        exe_dir = tmp_path / "target" / "x86_64-pc-windows-msvc" / "release"
        exe_dir.mkdir(parents=True)
        exe = exe_dir / "vcd.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_CUI))
        (tmp_path / ".smoke").mkdir()
        calls = FakeWinCalls()

        launch = "cd .smoke && ../target/x86_64-pc-windows-msvc/release/vcd.exe sample.txt"
        WinNativeSession(launch, str(tmp_path), calls=calls)

        assert calls.launched == []
        assert calls.launched_in_terminal == [(launch, str(tmp_path), "windows-terminal")]

    def test_auto_mode_launches_normally_for_a_detected_gui_exe(self, tmp_path) -> None:
        exe = tmp_path / "vimcode.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_GUI))
        calls = FakeWinCalls()

        WinNativeSession(str(exe), str(tmp_path), calls=calls)

        assert calls.launched == [(str(exe), str(tmp_path))]
        assert calls.launched_in_terminal == []

    def test_auto_mode_falls_back_to_window_when_exe_cannot_be_sniffed(self) -> None:
        """Pre-#3640 behavior, preserved exactly: an unreadable/undetectable
        target (the common case for every existing caller/test that never
        set up a real exe on disk) must still use the plain `launch` path,
        never block on a mode it couldn't determine."""
        calls = FakeWinCalls()

        WinNativeSession("vimcode.exe", "/no/such/dir", calls=calls)

        assert calls.launched == [("vimcode.exe", "/no/such/dir")]
        assert calls.launched_in_terminal == []

    def test_explicit_window_mode_overrides_auto_detection(self, tmp_path) -> None:
        """A caller that already knows better (or wants the pre-#3640
        behavior for some reason) can still force it — auto-detection is
        the ``None`` default's behavior, not a mandate."""
        exe = tmp_path / "vcd.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_CUI))
        calls = FakeWinCalls()

        WinNativeSession(str(exe), str(tmp_path), calls=calls, mode="window")

        assert calls.launched == [(str(exe), str(tmp_path))]
        assert calls.launched_in_terminal == []

    def test_explicit_terminal_app_overrides_the_auto_default(self, tmp_path) -> None:
        exe = tmp_path / "vcd.exe"
        exe.write_bytes(_pe_bytes(IMAGE_SUBSYSTEM_WINDOWS_CUI))
        calls = FakeWinCalls()

        WinNativeSession(str(exe), str(tmp_path), calls=calls, terminal_app="conhost")

        assert calls.launched_in_terminal == [(str(exe), str(tmp_path), "conhost")]

    def test_explicit_terminal_mode_without_terminal_app_raises_before_any_launch(self) -> None:
        calls = FakeWinCalls()

        with pytest.raises(WinNativeSpecError, match="terminal_app"):
            WinNativeSession("vimcode.exe", "/repo", calls=calls, mode="terminal")

        assert calls.launched == []
        assert calls.launched_in_terminal == []

    def test_unrecognized_terminal_app_raises_before_any_launch(self) -> None:
        calls = FakeWinCalls()

        with pytest.raises(WinNativeSpecError, match="terminal_app"):
            WinNativeSession(
                "vimcode.exe", "/repo", calls=calls, mode="terminal", terminal_app="iterm2",
            )

        assert calls.launched == []
        assert calls.launched_in_terminal == []

    def test_unrecognized_mode_raises_before_any_launch(self) -> None:
        calls = FakeWinCalls()

        with pytest.raises(WinNativeSpecError, match="mode"):
            WinNativeSession("vimcode.exe", "/repo", calls=calls, mode="headless")

        assert calls.launched == []
        assert calls.launched_in_terminal == []
