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
import time

import pytest

from coord.mac_native_driver import (
    MacNativeRuntimeError,
    MacNativeSpecError,
    MacOSCalls,
    NativeRunner,
    NativeSpec,
    NativeStep,
    _find_a11y_match,
    _summarize_elements,
    _vkey_for,
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


# ── _vkey_for ────────────────────────────────────────────────────────────────


class TestVkeyFor:
    def test_named_keys_case_insensitive(self) -> None:
        assert _vkey_for("enter") == (0x24, False)
        assert _vkey_for("ENTER") == (0x24, False)
        assert _vkey_for("Esc") == (0x35, False)

    def test_ctrl_combo(self) -> None:
        assert _vkey_for("ctrl+c") == (0x08, False)

    def test_lowercase_literal_needs_no_shift(self) -> None:
        assert _vkey_for("a") == (0x00, False)

    def test_uppercase_literal_needs_shift(self) -> None:
        assert _vkey_for("A") == (0x00, True)

    def test_digit_key(self) -> None:
        assert _vkey_for("5") == (0x17, False)

    def test_unrecognized_key_raises(self) -> None:
        with pytest.raises(MacNativeSpecError, match="unrecognized key"):
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
    ) -> None:
        self.launch_fails = launch_fails
        self.window_never_appears = window_never_appears
        self._ax_script = list(ax_script or [[]])
        self._capture_script = list(capture_script or [b"frame-0"])
        self._closes_after_n_polls = closes_after_n_polls
        self._alive_poll_count = 0
        self._session_ok = session_ok
        self._session_reason = session_reason

        self.launched: list[tuple[str, str]] = []
        self.moved: list[tuple[int, int, int, int, int, int]] = []
        self.clicks: list[tuple[int, int, int, str]] = []
        self.keys: list[tuple[int, str]] = []
        self.killed: list[int] = []
        self._next_pid = 1000
        self._window_id = 5555

    def session_available(self) -> tuple[bool, str]:
        return self._session_ok, self._session_reason

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

    def send_click(self, window_id: int, x: int, y: int, button: str) -> None:
        self.clicks.append((window_id, x, y, button))

    def send_key(self, pid: int, key: str) -> None:
        self.keys.append((pid, key))

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
        assert calls.clicks == [(calls._window_id, 42, 7, "right")]

    def test_click_defaults_to_left_button(self) -> None:
        calls = FakeMacCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0), _step("click", 1, x=1, y=1)]))
        assert calls.clicks == [(calls._window_id, 1, 1, "left")]

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
