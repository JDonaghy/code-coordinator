"""Tests for coord/mac_native_driver.py — the ``mac-native`` acceptance
driver's real engine (#3485): the native-spec YAML parser, the spec-to-macOS
translation (key naming, AX-match logic), and the step executor
(:class:`~coord.mac_native_driver.NativeRunner`) driven against a scripted
fake :class:`~coord.mac_native_driver.MacCalls` rather than a real
Quartz/AX call — per the issue's own acceptance bar ("unit tests with the OS
calls faked"). A real run against a real ``.app`` on real macOS hardware
(macmini) is out of reach for this repo's test suite and is exercised at the
operator level instead — the same split
``tests/test_win_native_driver.py``/``tests/test_tui_pty_driver.py`` call
out for their own platform-specific real runs.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import time

import pytest

from coord.key_spec import UnsupportedKey
from coord.mac_native_driver import (
    MacKeyEncoding,
    MacNativeRuntimeError,
    MacNativeSpecError,
    MacOSCalls,
    NativeRunner,
    NativeSpec,
    NativeStep,
    _encode_mac_chord,
    _find_a11y_match,
    _mac_key_encodings,
    _summarize_elements,
    parse_native_spec,
    run_native_spec,
)

# ── parse_native_spec ───────────────────────────────────────────────────────


class TestParseNativeSpec:
    VALID_YAML = """
name: vimcode mac smoke
width: 900
height: 700
steps:
  - type: launch
  - type: click
    x: 400
    y: 10
    button: right
  - type: wait
    ms: 100
  - type: capture
  - type: expect_a11y
    id: close-button
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
        assert spec.name == "vimcode mac smoke"
        assert (spec.width, spec.height) == (900, 700)
        assert [s.kind for s in spec.steps] == [
            "launch", "click", "wait", "capture", "expect_a11y",
            "expect_a11y_within", "expect_closed",
        ]

    def test_click_fields_parsed(self) -> None:
        spec = parse_native_spec(self.VALID_YAML)
        click = spec.steps[1]
        assert (click.x, click.y, click.button) == (400, 10, "right")

    def test_named_id_used_verbatim(self) -> None:
        spec = parse_native_spec(self.VALID_YAML)
        a11y = next(s for s in spec.steps if s.kind == "expect_a11y")
        assert a11y.step_id == "close-button"
        assert (a11y.role, a11y.name) == ("Button", "Close")

    def test_unnamed_step_gets_positional_id(self) -> None:
        spec = parse_native_spec(self.VALID_YAML)
        assert spec.steps[0].step_id == "000 launch"

    def test_defaults_when_top_level_fields_omitted(self) -> None:
        spec = parse_native_spec("steps:\n  - type: launch\n")
        assert spec.name == ""
        assert (spec.width, spec.height) == (1024, 768)

    def test_explicit_zero_x_is_honored_not_treated_as_absent(self) -> None:
        spec = parse_native_spec(
            "steps:\n  - type: click\n    x: 0\n    y: 0\n"
        )
        step = spec.steps[0]
        assert (step.x, step.y) == (0, 0)

    def test_invalid_yaml_raises_spec_error(self) -> None:
        with pytest.raises(MacNativeSpecError, match="not valid YAML"):
            parse_native_spec("steps: [")

    def test_non_mapping_top_level_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="mapping"):
            parse_native_spec("- just\n- a\n- list\n")

    def test_missing_steps_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="steps"):
            parse_native_spec("name: nothing here\n")

    def test_empty_steps_list_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="steps"):
            parse_native_spec("steps: []\n")

    def test_step_not_a_mapping_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="steps\\[0\\]"):
            parse_native_spec("steps:\n  - just a string\n")

    def test_unknown_step_type_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="unknown step type"):
            parse_native_spec("steps:\n  - type: teleport\n")

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="key"):
            parse_native_spec("steps:\n  - type: key\n")

    def test_click_missing_x_y_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="x"):
            parse_native_spec("steps:\n  - type: click\n")

    def test_unrecognized_button_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="button"):
            parse_native_spec(
                "steps:\n  - type: click\n    x: 0\n    y: 0\n    button: super-click\n"
            )

    def test_win_native_only_steps_are_unknown_here(self) -> None:
        # #3485's acceptance bar: no macOS-specific spec fork, but also no
        # Win32-only concepts (native HMENU/WM_NCHITTEST) leaking in either
        # — this driver's vocabulary is the shared subset, not a superset.
        with pytest.raises(MacNativeSpecError, match="unknown step type"):
            parse_native_spec(
                "steps:\n  - type: expect_menu\n    items: [File]\n"
            )
        with pytest.raises(MacNativeSpecError, match="unknown step type"):
            parse_native_spec(
                "steps:\n  - type: expect_hit\n    x: 0\n    y: 0\n    ht: HTCLOSE\n"
            )

    def test_type_text_expect_file_frontmost_dock_region_tofu_parse(self) -> None:
        # #3650
        spec = parse_native_spec(
            "steps:\n"
            "  - type: type_text\n"
            "    text: hello\n"
            "  - type: expect_file\n"
            "    path: /tmp/probe.txt\n"
            "    contains: hi\n"
            "  - type: expect_frontmost\n"
            "  - type: expect_dock_icon\n"
            "  - type: expect_region_not_uniform\n"
            "    x: 1\n    y: 2\n    width: 3\n    height: 4\n"
            "    tolerance: 10\n"
            "  - type: expect_no_tofu\n"
            "    x: 5\n    y: 6\n    width: 7\n    height: 8\n"
        )
        kinds = [s.kind for s in spec.steps]
        assert kinds == [
            "type_text", "expect_file", "expect_frontmost", "expect_dock_icon",
            "expect_region_not_uniform", "expect_no_tofu",
        ]
        type_text, expect_file, _frontmost, _dock, region, tofu = spec.steps
        assert type_text.text == "hello"
        assert (expect_file.path, expect_file.contains) == ("/tmp/probe.txt", "hi")
        assert (region.x, region.y, region.width, region.height, region.tolerance) == (1, 2, 3, 4, 10)
        assert (tofu.x, tofu.y, tofu.width, tofu.height) == (5, 6, 7, 8)

    def test_type_text_requires_text(self) -> None:
        with pytest.raises(MacNativeSpecError, match="text"):
            parse_native_spec("steps:\n  - type: type_text\n")

    def test_expect_region_not_uniform_requires_geometry(self) -> None:
        with pytest.raises(MacNativeSpecError, match="width"):
            parse_native_spec(
                "steps:\n  - type: expect_region_not_uniform\n    x: 0\n    y: 0\n"
            )


# ── _mac_key_encodings / _encode_mac_chord (#3639) ──────────────────────────


def _enc(vkey=None, unicode_char=None, shift=False, ctrl=False, alt=False, cmd=False):
    return MacKeyEncoding(vkey=vkey, unicode_char=unicode_char, shift=shift, ctrl=ctrl, alt=alt, cmd=cmd)


class TestMacKeyEncodings:
    def test_named_keys_case_insensitive(self) -> None:
        assert _mac_key_encodings("enter") == [_enc(vkey=0x24)]
        assert _mac_key_encodings("ENTER") == [_enc(vkey=0x24)]
        assert _mac_key_encodings("Esc") == [_enc(vkey=0x35)]

    def test_ctrl_combo(self) -> None:
        assert _mac_key_encodings("ctrl+c") == [_enc(vkey=0x08, ctrl=True)]

    def test_lowercase_literal_needs_no_shift(self) -> None:
        assert _mac_key_encodings("a") == [_enc(vkey=0x00)]

    def test_uppercase_literal_needs_shift(self) -> None:
        assert _mac_key_encodings("A") == [_enc(vkey=0x00, shift=True)]

    def test_digit_key(self) -> None:
        assert _mac_key_encodings("5") == [_enc(vkey=0x17)]

    def test_unrecognized_key_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="unrecognized key"):
            _mac_key_encodings("moonwalk")

    def test_alt_m(self) -> None:
        # #3625: `alt+m` previously raised "unrecognized key".
        assert _mac_key_encodings("alt+m") == [_enc(vkey=0x2E, alt=True)]

    def test_cmd_shift_p(self) -> None:
        assert _mac_key_encodings("cmd+shift+p") == [_enc(vkey=0x23, shift=True, cmd=True)]

    def test_ctrl_shift_right(self) -> None:
        assert _mac_key_encodings("ctrl+shift+right") == [_enc(vkey=0x7C, ctrl=True, shift=True)]

    def test_shift_f3(self) -> None:
        assert _mac_key_encodings("shift+f3") == [_enc(vkey=0x63, shift=True)]

    def test_bare_unmapped_character_goes_through_unicode_string(self) -> None:
        # Every ASCII punctuation character now has a real macOS keycode
        # (see the chord/digit-symbol tests below), so only a character
        # entirely outside that table — e.g. a non-ASCII letter — still
        # goes through the plain Unicode-string event path when unadorned.
        assert _mac_key_encodings("é") == [_enc(unicode_char="é")]

    def test_punctuation_with_real_keycode_maps_colon_and_at(self) -> None:
        # `:` is Shift+; and `@` is Shift+2 on a US ANSI layout — both now
        # carry a real vkey (+ implied Shift) rather than vkey=0.
        assert _mac_key_encodings(":") == [_enc(vkey=0x29, shift=True)]
        assert _mac_key_encodings("@") == [_enc(vkey=0x13, shift=True)]

    def test_shifted_digit_row_symbol_maps_to_digit_vkey(self) -> None:
        # `$` is Shift+4 on a US ANSI layout.
        assert _mac_key_encodings("$") == [_enc(vkey=0x15, shift=True)]

    def test_cmd_bracket_and_slash_use_real_keycodes_not_vkey_zero(self) -> None:
        # #3639 review finding: `cmd+[`/`cmd+]`/`cmd+/` previously posted
        # vkey=0 (kVK_ANSI_A) + the Cmd flag — i.e. silently delivered as
        # Cmd+A. They must now carry the real punctuation keycode.
        assert _mac_key_encodings("cmd+[") == [_enc(vkey=0x21, cmd=True)]
        assert _mac_key_encodings("cmd+]") == [_enc(vkey=0x1E, cmd=True)]
        assert _mac_key_encodings("cmd+/") == [_enc(vkey=0x2C, cmd=True)]

    def test_modifier_plus_unmappable_char_raises_unsupported(self) -> None:
        # A character with no real macOS keycode at all (outside the
        # letter/digit/punctuation tables) combined with a non-shift
        # modifier must raise rather than silently post vkey=0.
        with pytest.raises(UnsupportedKey):
            _mac_key_encodings("cmd+é")

    def test_chord_sequence_ctrl_k_ctrl_w(self) -> None:
        assert _mac_key_encodings("ctrl+k ctrl+w") == [
            _enc(vkey=0x28, ctrl=True),
            _enc(vkey=0x0D, ctrl=True),
        ]

    def test_delete_and_backspace_use_different_keycodes(self) -> None:
        # #3627: forward-delete and backspace are different physical keys.
        delete = _mac_key_encodings("delete")[0]
        backspace = _mac_key_encodings("backspace")[0]
        assert delete.vkey != backspace.vkey
        assert delete.vkey == 0x75
        assert backspace.vkey == 0x33

    def test_f21_has_no_macos_keycode_and_raises_unsupported(self) -> None:
        with pytest.raises(UnsupportedKey):
            _mac_key_encodings("f21")

    def test_encode_mac_chord_raises_unsupported_for_an_unknown_named_key(self) -> None:
        # Exercises `_encode_mac_chord` directly (what `_mac_key_encodings`
        # calls per chord) so the raise itself (never a silent fallthrough)
        # is proven reachable even for a named key the grammar accepts but
        # this platform has no keycode for (#2096: a gate must be able to
        # fail).
        from coord.key_spec import KeyChord

        bogus = KeyChord(modifiers=frozenset(), base="nonexistent", is_char=False)
        with pytest.raises(UnsupportedKey):
            _encode_mac_chord(bogus)


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
        assert _find_a11y_match(self.ELEMENTS, "Button", "Close") is None

    def test_summarize_empty_tree(self) -> None:
        assert _summarize_elements([]) == "(empty tree)"

    def test_summarize_non_empty_tree(self) -> None:
        summary = _summarize_elements(self.ELEMENTS)
        assert "File" in summary and "Edit" in summary


# ── a scripted fake MacCalls, for exercising NativeRunner deterministically ─


class FakeMacCalls:
    """A scripted :class:`~coord.mac_native_driver.MacCalls` (#3485). Every
    real-OS call is recorded so tests can assert exactly what
    :class:`NativeRunner` sent, and every behavior (AX tree, capture bytes,
    window aliveness) is injected rather than actually touching an OS."""

    def __init__(
        self,
        *,
        launch_fails: bool = False,
        window_never_appears: bool = False,
        ax_script: list[list[dict]] | None = None,
        capture_script: list[bytes] | None = None,
        closes_after_n_polls: int | None = None,
        session_ok: bool = True,
        session_reason: str = "",
        ax_trust_ok: bool = True,
        ax_trust_reason: str = "",
        frontmost: bool = True,
        frontmost_pid: int | None = None,
        dock_icon_ok: bool = True,
        dock_icon_reason: str = "",
    ) -> None:
        self.launch_fails = launch_fails
        self.window_never_appears = window_never_appears
        self._ax_script = list(ax_script or [[]])
        self._capture_script = list(capture_script or [b"frame-0"])
        self._closes_after_n_polls = closes_after_n_polls
        self._alive_poll_count = 0
        self._session_ok = session_ok
        self._session_reason = session_reason
        # #3566: whether this process identity holds Accessibility trust —
        # defaults to trusted so every test that isn't specifically about
        # the trust precheck is unaffected.
        self._ax_trust_ok = ax_trust_ok
        self._ax_trust_reason = ax_trust_reason
        # #3566: whether the launched pid is frontmost — `frontmost_pid`
        # lets a test name a SPECIFIC other pid "stealing" focus (mirroring
        # the real incident's iTerm2 pid) rather than just a bare bool.
        self._frontmost = frontmost
        self._frontmost_pid = frontmost_pid if frontmost_pid is not None else 424242
        self._dock_icon_ok = dock_icon_ok
        self._dock_icon_reason = dock_icon_reason

        self.launched: list[tuple[str, str]] = []
        self.moved: list[tuple[int, int, int, int, int, int]] = []
        self.clicks: list[tuple[int, int, int, int, str]] = []
        self.keys: list[tuple[int, str]] = []
        self.typed: list[tuple[int, str]] = []
        self.killed: list[int] = []
        self.frontmost_checks: list[int] = []
        self._next_pid = 1000
        self._window_id = 5555

    def session_available(self) -> tuple[bool, str]:
        return self._session_ok, self._session_reason

    def ax_trust_available(self) -> tuple[bool, str]:
        return self._ax_trust_ok, self._ax_trust_reason

    def is_frontmost(self, pid: int) -> tuple[bool, int]:
        self.frontmost_checks.append(pid)
        if self._frontmost:
            return True, pid
        return False, self._frontmost_pid

    def launch(self, command: str, cwd: str) -> int:
        if self.launch_fails:
            raise MacNativeRuntimeError("could not start process")
        self.launched.append((command, cwd))
        self._next_pid += 1
        return self._next_pid

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        if self.window_never_appears:
            raise MacNativeRuntimeError(f"no window for pid={pid}")
        return self._window_id

    def move_window(
        self, pid: int, window_id: int, x: int, y: int, width: int, height: int,
    ) -> None:
        self.moved.append((pid, window_id, x, y, width, height))

    def is_window_alive(self, window_id: int) -> bool:
        if self._closes_after_n_polls is None:
            return True
        self._alive_poll_count += 1
        return self._alive_poll_count <= self._closes_after_n_polls

    def send_click(self, pid: int, window_id: int, x: int, y: int, button: str) -> None:
        self.clicks.append((pid, window_id, x, y, button))

    def send_key(self, pid: int, key: str) -> None:
        self.keys.append((pid, key))

    def type_text(self, pid: int, text: str) -> None:
        self.typed.append((pid, text))

    def has_dock_icon(self, pid: int) -> tuple[bool, str]:
        return self._dock_icon_ok, self._dock_icon_reason

    def ax_elements(self, pid: int) -> list[dict]:
        if len(self._ax_script) > 1:
            return self._ax_script.pop(0)
        return self._ax_script[0]

    def capture(self, window_id: int) -> bytes:
        if len(self._capture_script) > 1:
            return self._capture_script.pop(0)
        return self._capture_script[0]

    def kill(self, pid: int) -> None:
        self.killed.append(pid)


def _step(kind: str, index: int = 0, **kwargs) -> NativeStep:
    return NativeStep(kind=kind, index=index, **kwargs)


def _spec(steps: list[NativeStep], **kwargs) -> NativeSpec:
    defaults = {"name": "test", "width": 800, "height": 600}
    defaults.update(kwargs)
    return NativeSpec(steps=tuple(steps), **defaults)


def _runner(calls: FakeMacCalls, **kwargs) -> NativeRunner:
    return NativeRunner(calls, "./VimCode.app/Contents/MacOS/VimCode", "/cwd", **kwargs)


class TestNativeRunnerLaunch:
    def test_launch_success_reports_passing_entry_and_moves_window(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")], width=900, height=700))
        assert results == [{"id": "000 launch", "status": "pass", "message": ""}]
        assert calls.launched == [("./VimCode.app/Contents/MacOS/VimCode", "/cwd")]
        assert calls.moved  # window was resized/positioned per the spec

    def test_launch_process_failure_reports_failing_entry(self) -> None:
        calls = FakeMacCalls(launch_fails=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results[0]["status"] == "fail"
        assert "could not start process" in results[0]["message"]

    def test_launch_window_never_appears_reports_failing_entry(self) -> None:
        calls = FakeMacCalls(window_never_appears=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results[0]["status"] == "fail"
        assert "no window" in results[0]["message"]

    def test_step_before_launch_fails_with_a_clear_message(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        results = runner.run(_spec([_step("key", key="a")]))
        assert results[0]["status"] == "fail"
        assert "no window" in results[0]["message"]


class TestNativeRunnerSessionPrecheck:
    """#3510: a locked screen or absent GUI session is reported as a
    distinct ``unavailable`` lane verdict, never an ordinary failed step —
    and no step (not even ``launch``) runs when it's locked."""

    def test_locked_screen_reports_unavailable_and_runs_no_step(self) -> None:
        calls = FakeMacCalls(session_ok=False, session_reason="the screen is locked")
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results == [
            {"id": "session", "status": "unavailable", "message": "the screen is locked"}
        ]
        assert calls.launched == []

    def test_locked_screen_never_a_failed_step(self) -> None:
        calls = FakeMacCalls(session_ok=False, session_reason="locked")
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert all(r["status"] != "fail" for r in results)

    def test_unlocked_session_runs_normally(self) -> None:
        calls = FakeMacCalls(session_ok=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results == [{"id": "000 launch", "status": "pass", "message": ""}]
        assert calls.launched


class TestNativeRunnerAxTrustPrecheck:
    """#3566: the Accessibility-trust precheck — the literal incident this
    issue is about. A denied grant must be reported as a distinct
    ``unavailable`` verdict, checked AFTER the session precheck but still
    BEFORE any step (not even ``launch``) runs."""

    def test_denied_trust_reports_unavailable_and_runs_no_step(self) -> None:
        calls = FakeMacCalls(
            ax_trust_ok=False,
            ax_trust_reason="AXIsProcessTrusted() is False for this process identity",
        )
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results == [{
            "id": "ax-trust", "status": "unavailable",
            "message": "AXIsProcessTrusted() is False for this process identity",
        }]
        assert calls.launched == []

    def test_denied_trust_never_a_failed_step(self) -> None:
        calls = FakeMacCalls(ax_trust_ok=False, ax_trust_reason="denied")
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert all(r["status"] != "fail" for r in results)

    def test_session_precheck_runs_before_trust_precheck(self) -> None:
        """A locked screen is reported as 'session' unavailable even when
        trust is ALSO denied — the session check still runs first (#3510
        predates this precheck and keeps its own id/ordering)."""
        calls = FakeMacCalls(
            session_ok=False, session_reason="the screen is locked",
            ax_trust_ok=False, ax_trust_reason="denied",
        )
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results == [
            {"id": "session", "status": "unavailable", "message": "the screen is locked"}
        ]

    def test_trusted_identity_runs_normally(self) -> None:
        calls = FakeMacCalls(ax_trust_ok=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results == [{"id": "000 launch", "status": "pass", "message": ""}]
        assert calls.launched


class TestNativeRunnerActions:
    def test_key_step_forwards_to_calls(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0), _step("key", 1, key="enter")]))
        assert calls.keys == [(calls._next_pid, "enter")]

    def test_click_step_forwards_coordinates_and_button(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        runner.run(_spec([
            _step("launch", 0), _step("click", 1, x=42, y=7, button="right"),
        ]))
        assert calls.clicks == [(calls._next_pid, calls._window_id, 42, 7, "right")]

    def test_click_defaults_to_left_button(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0), _step("click", 1, x=1, y=1)]))
        assert calls.clicks == [(calls._next_pid, calls._window_id, 1, 1, "left")]

    def test_wait_step_sleeps_approximately_ms(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        start = time.monotonic()
        runner.run(_spec([_step("launch", 0), _step("wait", 1, ms=50)]))
        assert time.monotonic() - start >= 0.05

    def test_capture_step_attaches_capture_b64_on_pass(self) -> None:
        calls = FakeMacCalls(capture_script=[b"one-frame"])
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch", 0), _step("capture", 1)]))
        capture_entry = results[1]
        assert capture_entry["status"] == "pass"
        assert base64.b64decode(capture_entry["capture_b64"]) == b"one-frame"

    def test_type_text_step_forwards_to_calls(self) -> None:
        # #3650
        calls = FakeMacCalls()
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("type_text", 1, text="hello world"),
        ]))
        assert results[1]["status"] == "pass"
        assert calls.typed == [(calls._next_pid, "hello world")]

    def test_type_text_refuses_when_not_frontmost(self) -> None:
        # #3650/#3566: same refusal path every other action step already
        # has — never a blind retry into whatever window is in front.
        calls = FakeMacCalls(frontmost=False, frontmost_pid=9999)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("type_text", 1, text="hi"),
        ]))
        assert results[1]["status"] == "fail"
        assert "not frontmost" in results[1]["message"]
        assert calls.typed == []


class TestNativeRunnerExpectFile:
    """#3650 — delegates to :func:`coord.native_fs_wait.wait_for_file`; see
    ``tests/test_native_fs_wait.py`` for that function's own coverage."""

    def test_passes_once_file_exists(self, tmp_path) -> None:
        target = tmp_path / "probe.txt"
        target.write_text("ready")
        calls = FakeMacCalls()
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_file", 1, path=str(target), timeout_ms=1000),
        ]))
        assert results[1]["status"] == "pass"

    def test_fails_when_file_never_appears(self, tmp_path) -> None:
        target = tmp_path / "never.txt"
        calls = FakeMacCalls()
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_file", 1, path=str(target), timeout_ms=100),
        ]))
        assert results[1]["status"] == "fail"
        assert "did not appear" in results[1]["message"]

    def test_explicit_zero_timeout_is_honored_not_defaulted_to_5000(self, tmp_path) -> None:
        """Regression: `step.timeout_ms or 5000` used to silently turn an
        explicit "check once, right now" ``timeout_ms: 0`` into a 5-second
        wait (`_int_default` already supplies the 5000 default for an
        *absent* field — the `or` only mattered for this literal-zero
        case). A missing file with ``timeout_ms: 0`` must fail fast."""
        import time

        target = tmp_path / "never.txt"
        calls = FakeMacCalls()
        runner = _runner(calls)
        start = time.monotonic()
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_file", 1, path=str(target), timeout_ms=0),
        ]))
        elapsed = time.monotonic() - start
        assert results[1]["status"] == "fail"
        assert elapsed < 2.0


class TestNativeRunnerExpectFrontmost:
    def test_passes_when_frontmost(self) -> None:
        calls = FakeMacCalls(frontmost=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch", 0), _step("expect_frontmost", 1)]))
        assert results[1]["status"] == "pass"

    def test_fails_when_another_pid_is_frontmost(self) -> None:
        calls = FakeMacCalls(frontmost=False, frontmost_pid=424242)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch", 0), _step("expect_frontmost", 1)]))
        assert results[1]["status"] == "fail"
        assert "424242" in results[1]["message"]


class TestNativeRunnerExpectDockIcon:
    def test_passes_when_dock_icon_present(self) -> None:
        calls = FakeMacCalls(dock_icon_ok=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch", 0), _step("expect_dock_icon", 1)]))
        assert results[1]["status"] == "pass"

    def test_fails_when_no_dock_icon_the_vimcode_1824_shape(self) -> None:
        calls = FakeMacCalls(dock_icon_ok=False, dock_icon_reason="ApplicationType is Background")
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch", 0), _step("expect_dock_icon", 1)]))
        assert results[1]["status"] == "fail"
        assert "Background" in results[1]["message"]


def _png_bytes(rows: list[list[tuple[int, int, int]]]) -> bytes:
    """A minimal 8-bit RGB PNG — see ``tests/test_native_pixels.py``'s own
    encoder for the full explanation; duplicated narrowly here so this test
    module doesn't reach into another module's test-only helpers."""
    import struct
    import zlib

    height = len(rows)
    width = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for (r, g, b) in row:
            raw += bytes((r, g, b))
    compressed = zlib.compress(bytes(raw))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


class TestNativeRunnerExpectRegionNotUniform:
    """#3650 — decodes via :func:`coord.native_pixels.decode_png`, judged by
    the one shared :func:`coord.native_pixels.region_not_uniform`."""

    def test_passes_for_a_varied_region(self) -> None:
        rows = [[(255, 0, 0), (0, 255, 0)], [(0, 0, 255), (10, 10, 10)]]
        calls = FakeMacCalls(capture_script=[_png_bytes(rows)])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_region_not_uniform", 1, x=0, y=0, width=2, height=2),
        ]))
        assert results[1]["status"] == "pass"

    def test_fails_for_a_uniform_region_the_vimcode_1676_black_bar_shape(self) -> None:
        rows = [[(0, 0, 0)] * 4 for _ in range(4)]
        calls = FakeMacCalls(capture_script=[_png_bytes(rows)])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_region_not_uniform", 1, x=0, y=0, width=4, height=4),
        ]))
        assert results[1]["status"] == "fail"
        assert "uniform" in results[1]["message"]


class TestNativeRunnerExpectNoTofu:
    def test_passes_for_textured_content(self) -> None:
        rows = [
            [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 10, 10)],
            [(255, 255, 255), (128, 128, 128), (64, 64, 64), (0, 0, 0)],
            [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)],
        ]
        calls = FakeMacCalls(capture_script=[_png_bytes(rows)])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_no_tofu", 1, x=0, y=0, width=4, height=3),
        ]))
        assert results[1]["status"] == "pass"

    def test_fails_for_a_classic_tofu_box(self) -> None:
        size = 8
        rows = [
            [(0, 0, 0) if (px in (0, size - 1) or py in (0, size - 1)) else (255, 255, 255)
             for px in range(size)]
            for py in range(size)
        ]
        calls = FakeMacCalls(capture_script=[_png_bytes(rows)])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_no_tofu", 1, x=0, y=0, width=size, height=size),
        ]))
        assert results[1]["status"] == "fail"
        assert "tofu" in results[1]["message"]


class TestNativeRunnerFrontmostRefusal:
    """#3566: the first live `mac-native` bugbash attempt sent keys/clicks
    while `AXIsProcessTrusted() == False` with the real frontmost pid
    belonging to the operator's iTerm2 — `NativeRunner` must refuse any
    key/click unless the launched pid is confirmed frontmost immediately
    before the event, every single time (never a cached "was frontmost
    once" flag)."""

    def test_key_refused_when_not_frontmost(self) -> None:
        calls = FakeMacCalls(frontmost=False, frontmost_pid=777)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch", 0), _step("key", 1, key="a")]))
        assert results[1]["status"] == "fail"
        assert "not frontmost" in results[1]["message"]
        assert "777" in results[1]["message"]
        assert calls.keys == []  # never actually sent

    def test_click_refused_when_not_frontmost(self) -> None:
        calls = FakeMacCalls(frontmost=False, frontmost_pid=777)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("click", 1, x=1, y=1),
        ]))
        assert results[1]["status"] == "fail"
        assert "not frontmost" in results[1]["message"]
        assert calls.clicks == []  # never actually sent

    def test_focus_failure_is_a_plain_step_failure_not_a_retry(self) -> None:
        """A refused step must behave exactly like any other failing step
        (one result entry, run continues to the next step) — there is no
        retry path anywhere in `NativeRunner` that could resend into
        whatever window is in front."""
        calls = FakeMacCalls(frontmost=False)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("key", 1, key="a"), _step("wait", 2, ms=1),
        ]))
        assert [r["status"] for r in results] == ["pass", "fail", "pass"]
        # exactly one frontmost check per key/click step — no retry loop
        assert calls.frontmost_checks == [calls._next_pid]

    def test_key_and_click_allowed_when_frontmost(self) -> None:
        calls = FakeMacCalls(frontmost=True)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("key", 1, key="a"), _step("click", 2, x=1, y=1),
        ]))
        assert [r["status"] for r in results] == ["pass", "pass", "pass"]
        assert calls.keys == [(calls._next_pid, "a")]
        assert calls.clicks == [(calls._next_pid, calls._window_id, 1, 1, "left")]


class TestNativeRunnerExpectA11y:
    def test_match_passes(self) -> None:
        calls = FakeMacCalls(ax_script=[[{"role": "Button", "name": "Close", "visible": True}]])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="Button", name="Close"),
        ]))
        assert results[1]["status"] == "pass"

    def test_no_match_fails_with_tree_summary(self) -> None:
        calls = FakeMacCalls(ax_script=[[{"role": "Button", "name": "Open", "visible": True}]])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="Button", name="Close"),
        ]))
        assert results[1]["status"] == "fail"
        assert "Open" in results[1]["message"]

    def test_failing_step_attaches_capture_evidence(self) -> None:
        calls = FakeMacCalls(
            ax_script=[[]], capture_script=[b"failure-frame"],
        )
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="Button", name="Close"),
        ]))
        assert results[1]["status"] == "fail"
        assert base64.b64decode(results[1]["capture_b64"]) == b"failure-frame"


class TestNativeRunnerExpectA11yWithin:
    def test_appears_within_timeout_reports_elapsed_message(self) -> None:
        calls = FakeMacCalls(ax_script=[
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
        calls = FakeMacCalls(ax_script=[[]])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_a11y_within", 1, role="MenuItem", name="Cut", timeout_ms=100),
        ]))
        assert results[1]["status"] == "fail"
        assert "never appeared" in results[1]["message"]


class TestNativeRunnerExpectClosed:
    def test_window_that_closes_passes(self) -> None:
        calls = FakeMacCalls(closes_after_n_polls=1)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_closed", 1, timeout_ms=1000),
        ]))
        assert results[1]["status"] == "pass"

    def test_window_that_never_closes_fails(self) -> None:
        # closes_after_n_polls=None -> is_window_alive always True.
        calls = FakeMacCalls()
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_closed", 1, timeout_ms=100),
        ]))
        assert results[1]["status"] == "fail"
        assert "did not actually close" in results[1]["message"]


class TestNativeRunnerTeardownAndSafety:
    def test_teardown_kills_only_the_launched_pid(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0)]))
        assert len(calls.killed) == 1
        launched_pid = calls.killed[0]
        assert launched_pid >= 1001  # FakeMacCalls' own pid counter

    def test_teardown_does_not_kill_when_launch_never_ran(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("key", 0, key="a")]))  # fails: no launch
        assert calls.killed == []

    def test_teardown_still_kills_when_a_later_step_raises(self) -> None:
        calls = FakeMacCalls(ax_script=[[]])
        runner = _runner(calls)
        runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="Button", name="Close"),
        ]))
        assert len(calls.killed) == 1

    def test_no_name_based_kill_path_exists_in_the_module(self) -> None:
        """Safety invariant from the module docstring: teardown must only
        ever be able to kill the exact PID this driver itself launched —
        never by matching a process by its bundle identifier or executable
        name. A `killall`/`pkill`-style call, `NSRunningApplication`
        bundle-id lookup, or any process-enumeration used to find a victim
        by name, anywhere in this module's actual CODE (not its prose
        docstrings, which legitimately describe the invariant by name)
        would be one bad assumption away from killing something on an
        operator's own macmini session that isn't this driver's own child."""
        import ast

        import coord.mac_native_driver as module

        source = open(module.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        code_only = "\n".join(
            ast.unparse(node) for node in ast.walk(tree)
            if isinstance(node, (ast.Call, ast.Import, ast.ImportFrom))
        ).lower()
        for forbidden in (
            "killall", "pkill", "nsrunningapplication", "bundleidentifier",
            "os.system",
        ):
            assert forbidden not in code_only, f"found forbidden pattern {forbidden!r} in actual code"

        # And the one sanctioned kill path's signature is exactly `(self,
        # pid: int)` — not e.g. `(self, pid, name=None)` that a later edit
        # could widen into a by-name fallback.
        import inspect

        sig = inspect.signature(module.MacOSCalls.kill)
        assert list(sig.parameters) == ["self", "pid"]

    def test_input_posted_to_pid_never_the_global_hid_tap(self) -> None:
        """#3566: `send_click`/`send_key` must post via `CGEventPostToPid`
        — addressed to the launched process — never the global
        `CGEventPost(kCGHIDEventTap, ...)` tap the first live bugbash
        incident effectively fell back to (clicks/keys landing on whatever
        window the real OS focus was on, which was the operator's
        iTerm2)."""
        import coord.mac_native_driver as module

        source = open(module.__file__, encoding="utf-8").read()
        assert "CGEventPostToPid" in source
        # No bare `CGEventPost(` call anywhere in the actual code (prose
        # mentions in docstrings/comments are fine and deliberately
        # excluded by only walking ast.Call nodes).
        import ast

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "CGEventPost", (
                    "found a bare CGEventPost(...) call — must be "
                    "CGEventPostToPid(...) instead (#3566)"
                )


class TestNativeRunnerDriverLevelTimeout:
    def test_steps_past_the_deadline_are_aborted_not_run(self) -> None:
        calls = FakeMacCalls()
        runner = NativeRunner(
            calls, "./VimCode.app/Contents/MacOS/VimCode", "/cwd",
            deadline=time.monotonic() - 1,
        )
        results = runner.run(_spec([
            _step("launch", 0), _step("key", 1, key="a"),
        ]))
        assert all(r["status"] == "fail" for r in results)
        assert "driver-level timeout" in results[0]["message"]
        assert calls.launched == []  # never even got to run `launch`


# ── MacOSCalls platform guard + optional-dependency guard ──────────────────


class TestMacOSCallsPlatformGuard:
    def test_construction_off_macos_raises(self) -> None:
        if hasattr(os, "uname") and os.uname().sysname == "Darwin":
            pytest.skip("this guard only fires off macOS")
        with pytest.raises(MacNativeRuntimeError, match="macOS"):
            MacOSCalls()


class _FakeQuartz:
    """Records every posted ``CGEvent`` as a plain dict — enough to assert
    the real ``send_key`` sequence (vkey, modifier flags, the Unicode
    override) without pyobjc installed (#3639 review non-blocking concern:
    the mac tests used to stop at the intermediate :class:`MacKeyEncoding`
    dataclass, leaving the brand-new punctuation-keycode path and the
    ``CGEventKeyboardSetUnicodeString`` call shape entirely unverified)."""

    kCGEventFlagMaskShift = 0x00020000
    kCGEventFlagMaskControl = 0x00040000
    kCGEventFlagMaskAlternate = 0x00080000
    kCGEventFlagMaskCommand = 0x00100000

    def __init__(self) -> None:
        self.posted: list[dict] = []

    def CGEventCreateKeyboardEvent(self, _source, vkey, key_down):
        return {"vkey": vkey, "down": key_down, "flags": 0, "unicode": None}

    def CGEventKeyboardSetUnicodeString(self, event, length, chars) -> None:
        event["unicode"] = chars
        event["length"] = length

    def CGEventSetFlags(self, event, flags) -> None:
        event["flags"] = flags

    def CGEventPostToPid(self, _pid, event) -> None:
        self.posted.append(dict(event))


def _make_mac_calls(quartz: _FakeQuartz) -> MacOSCalls:
    """Build a :class:`MacOSCalls` bypassing ``__init__``'s platform guard
    (construction requires real macOS) with a fake ``quartz``."""
    calls = object.__new__(MacOSCalls)
    calls._quartz = quartz
    return calls


# ── #3622: MacOSCalls.launch() must track the REAL process's pid, not
# ``/bin/sh``'s, for the lane's own prescribed compound ``--launch`` shape ──


class TestMacOSCallsLaunchExecWraps:
    """Tier-1 conformance guard for the bugbash finding (#3622): a bugbash
    run of ``coord app-drive open mac-native --launch 'cd .smoke && HOME=
    $PWD/home "<bin>" sample.txt'`` — the exact compound form this lane's
    own HARD RULE prescribes — always failed with ``no on-screen window
    appeared for pid=<N>``, because ``MacOSCalls.launch()`` tracked a bare
    ``subprocess.Popen(command, shell=True)``'s own pid, and ``/bin/sh``
    forks a child for the final (real-binary) command of a ``&&`` chain
    instead of exec'ing into it in place — so the tracked pid never
    matches the real window-owning process's pid.

    Both tests construct a real :class:`MacOSCalls` (bypassing
    ``__init__``'s macOS-only platform guard — exactly like
    :func:`_make_mac_calls` above; ``launch()`` itself touches no
    Quartz/AX call, so this is safe and real-process-exercising on any
    POSIX platform, not just real macOS hardware) and fail against the
    pre-fix ``launch()`` (a bare ``Popen(command, shell=True)``, which
    leaves two live processes — ``/bin/sh`` and the real child — with
    different pids) and pass against the fix (``Popen(
    wrap_launch_command(command), shell=True)``, which collapses the
    compound command's final simple command into a single ``execve()``).
    """

    @pytest.mark.skipif(os.name != "posix", reason="shell=True compound commands are POSIX here")
    def test_compound_launch_pid_is_the_real_binary_not_the_shell(self, tmp_path) -> None:
        marker = tmp_path / "pid.txt"
        subdir = tmp_path / ".smoke"
        subdir.mkdir()
        # The lane's own prescribed shape: a leading `cd` plus an inline
        # env-var assignment on the final simple command. The interpreter
        # path is double-quoted (#3629): this repo's own worktrees live
        # under a macOS `Application Support` path, so an unquoted
        # `sys.executable` here would itself get word-split, unrelated to
        # the compound-command shape this test means to exercise.
        command = (
            f'cd .smoke && HOME=$PWD/home "{sys.executable}" -c '
            "\"import os, pathlib, time; "
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); "
            "time.sleep(5)\""
        )
        calls = object.__new__(MacOSCalls)
        pid = calls.launch(command, str(tmp_path))
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.exists(), (
                "the launched process never wrote its own pid — the "
                "compound launch command failed to start at all"
            )
            real_pid = int(marker.read_text())
            assert pid == real_pid, (
                f"MacOSCalls.launch() returned pid={pid} but the real "
                f"binary's own pid is {real_pid} — the tracked pid is "
                f"/bin/sh's, not the real process (#3622)"
            )
        finally:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            try:
                os.kill(real_pid, 9)
            except (ProcessLookupError, NameError):
                pass

    def test_launch_hands_popen_the_exec_wrapped_command(self, monkeypatch) -> None:
        """Faster, non-spawning companion: pins that ``launch()`` really
        does route through :func:`coord.shell_exec_wrap.wrap_launch_command`
        rather than handing ``Popen`` the caller's command verbatim — so a
        future edit that silently drops the wrap call fails here even
        without spawning a real process."""
        captured: dict[str, object] = {}

        class _FakeProc:
            pid = 4242

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        calls = object.__new__(MacOSCalls)
        pid = calls.launch("cd .smoke && HOME=$PWD/home ./bin sample.txt", "/repo")
        assert pid == 4242
        assert captured["command"] == (
            'cd .smoke && exec env HOME="$PWD/home" ./bin sample.txt'
        )
        assert captured["kwargs"]["shell"] is True
        assert captured["kwargs"]["cwd"] == "/repo"


class TestSendKeyRealSequence:
    def test_cmd_bracket_posts_the_real_vkey_not_zero(self) -> None:
        # #3639 blocking review finding: `cmd+[` used to post vkey=0
        # (kVK_ANSI_A) plus the Cmd flag.
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        calls.send_key(1, "cmd+[")
        down, up = quartz.posted
        assert down["vkey"] == 0x21
        assert down["down"] is True
        assert down["flags"] == quartz.kCGEventFlagMaskCommand
        assert up["vkey"] == 0x21
        assert up["down"] is False
        assert up["flags"] == quartz.kCGEventFlagMaskCommand

    def test_cmd_slash_posts_the_real_vkey(self) -> None:
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        calls.send_key(1, "cmd+/")
        down, _up = quartz.posted
        assert down["vkey"] == 0x2C

    def test_bare_unmapped_char_passes_the_string_itself_not_a_list(self) -> None:
        # Pins the conventional pyobjc call shape (#3639 review concern):
        # the string itself, not a one-element list of codepoint ints.
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        calls.send_key(1, "é")
        down, up = quartz.posted
        assert down["vkey"] == 0
        assert down["unicode"] == "é"
        assert up["unicode"] == "é"

    def test_chord_sequence_posts_each_chord_as_its_own_down_up_pair(self) -> None:
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        calls.send_key(1, "ctrl+k ctrl+w")
        assert len(quartz.posted) == 4
        assert [e["vkey"] for e in quartz.posted] == [0x28, 0x28, 0x0D, 0x0D]

    def test_delete_and_backspace_post_different_vkeys(self) -> None:
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        calls.send_key(1, "delete")
        delete_vkey = quartz.posted[0]["vkey"]
        quartz.posted.clear()
        calls.send_key(1, "backspace")
        backspace_vkey = quartz.posted[0]["vkey"]
        assert delete_vkey != backspace_vkey


class TestMacOSCallsTypeText:
    """#3650 — one Unicode-string keyboard down/up pair per character,
    posted via ``CGEventPostToPid`` (same pid-addressed path as
    :meth:`MacOSCalls.send_key`/:meth:`send_click`, #3566)."""

    def test_posts_one_down_up_pair_per_character(self) -> None:
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        calls.type_text(1, "ab")
        assert len(quartz.posted) == 4
        assert [e["unicode"] for e in quartz.posted] == ["a", "a", "b", "b"]
        assert [e["down"] for e in quartz.posted] == [True, False, True, False]

    def test_handles_unicode_characters(self) -> None:
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        calls.type_text(1, "日")
        assert [e["unicode"] for e in quartz.posted] == ["日", "日"]
        assert [e["length"] for e in quartz.posted] == [1, 1]

    def test_non_bmp_character_passes_the_real_utf16_length_not_a_hardcoded_one(self) -> None:
        """Regression: a non-BMP character (any emoji — exactly an
        ``expect_no_tofu`` use case) encodes as a UTF-16 *surrogate pair*
        (2 code units). A hardcoded ``length=1`` would tell
        ``CGEventKeyboardSetUnicodeString`` to read only the first of
        those two units, silently cutting the character off."""
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        emoji = "\U0001F600"  # U+1F600 GRINNING FACE — above the BMP
        calls.type_text(1, emoji)
        assert [e["unicode"] for e in quartz.posted] == [emoji, emoji]
        assert [e["length"] for e in quartz.posted] == [2, 2]

    def test_empty_text_posts_nothing(self) -> None:
        quartz = _FakeQuartz()
        calls = _make_mac_calls(quartz)
        calls.type_text(1, "")
        assert quartz.posted == []


class TestMacOSCallsHasDockIcon:
    """#3650 — ``lsappinfo``'s own ``ApplicationType`` field."""

    def _make_calls(self) -> MacOSCalls:
        return object.__new__(MacOSCalls)

    def test_foreground_app_has_dock_icon(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["lsappinfo", "find"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="ASN:1-2-3\n", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout='"ApplicationType"="Foreground"\n', stderr="",
            )

        monkeypatch.setattr("coord.mac_native_driver.subprocess.run", fake_run)
        has_icon, reason = self._make_calls().has_dock_icon(1234)
        assert (has_icon, reason) == (True, "")

    def test_background_app_has_no_dock_icon(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["lsappinfo", "find"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="ASN:1-2-3\n", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout='"ApplicationType"="Background"\n', stderr="",
            )

        monkeypatch.setattr("coord.mac_native_driver.subprocess.run", fake_run)
        has_icon, reason = self._make_calls().has_dock_icon(1234)
        assert has_icon is False
        assert "Background" in reason

    def test_unknown_pid_has_no_dock_icon(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("coord.mac_native_driver.subprocess.run", fake_run)
        has_icon, reason = self._make_calls().has_dock_icon(999999)
        assert has_icon is False
        assert "no running application" in reason


class TestImportQuartz:
    def test_missing_quartz_raises_actionable_error(self, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "Quartz":
                raise ModuleNotFoundError("no module named Quartz")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        from coord.mac_native_driver import _import_quartz

        with pytest.raises(MacNativeRuntimeError, match="mac-native.*extra"):
            _import_quartz()


class TestImportAx:
    def test_missing_application_services_raises_actionable_error(self, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "ApplicationServices":
                raise ModuleNotFoundError("no module named ApplicationServices")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        from coord.mac_native_driver import _import_ax

        with pytest.raises(MacNativeRuntimeError, match="mac-native.*extra"):
            _import_ax()


# ── run_native_spec top-level entry point ───────────────────────────────────


class TestRunNativeSpec:
    def test_runs_against_an_injected_fake_and_returns_normalized_tests(self) -> None:
        calls = FakeMacCalls(ax_script=[[{"role": "Button", "name": "Close", "visible": True}]])
        spec_text = (
            "steps:\n"
            "  - type: launch\n"
            "  - type: expect_a11y\n"
            "    id: close-button\n"
            "    role: Button\n"
            "    name: Close\n"
        )
        tests = run_native_spec(
            spec_text, launch_command="VimCode.app", cwd="/repo", calls=calls,
        )
        assert tests == [
            {"id": "000 launch", "status": "pass", "message": ""},
            {"id": "close-button", "status": "pass", "message": ""},
        ]

    def test_malformed_spec_raises_spec_error_before_any_launch(self) -> None:
        calls = FakeMacCalls()
        with pytest.raises(MacNativeSpecError):
            run_native_spec("steps: []\n", launch_command="VimCode.app", cwd="/repo", calls=calls)
        assert calls.launched == []
