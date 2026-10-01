"""Tests for coord/gtk_native_driver.py — the ``gtk-native`` acceptance
driver's real engine (#3486): the native-spec YAML parser, the spec-to-Linux
translation (key naming, AT-SPI-match logic), and the step executor
(:class:`~coord.gtk_native_driver.NativeRunner`) driven against a scripted
fake :class:`~coord.gtk_native_driver.GtkCalls` rather than real
``xdotool``/AT-SPI/``xwd`` calls — per the issue's own acceptance bar ("unit
tests with the OS calls faked"). A real run against vimcode's real GTK build
on a real Linux fleet host under a headless display is out of reach for this
repo's test suite and is exercised at the operator level instead — the same
split ``tests/test_win_native_driver.py``/``tests/test_mac_native_driver.py``
call out for their own platform-specific real runs.
"""

from __future__ import annotations

import base64
import os
import time

import pytest

from coord.gtk_native_driver import (
    GtkNativeRuntimeError,
    GtkNativeSpecError,
    LinuxGtkCalls,
    NativeRunner,
    NativeSpec,
    NativeStep,
    _find_a11y_match,
    _summarize_elements,
    _xdotool_key_for,
    parse_native_spec,
    run_native_spec,
)

# ── parse_native_spec ───────────────────────────────────────────────────────


class TestParseNativeSpec:
    VALID_YAML = """
name: vimcode gtk smoke
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
    role: push button
    name: Close
  - type: expect_a11y_within
    role: menu item
    name: Cut
    timeout_ms: 500
  - type: expect_closed
    timeout_ms: 2000
"""

    def test_parses_all_step_types_and_top_level_fields(self) -> None:
        spec = parse_native_spec(self.VALID_YAML)
        assert spec.name == "vimcode gtk smoke"
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
        assert (a11y.role, a11y.name) == ("push button", "Close")

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
        with pytest.raises(GtkNativeSpecError, match="not valid YAML"):
            parse_native_spec("steps: [")

    def test_non_mapping_top_level_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="mapping"):
            parse_native_spec("- just\n- a\n- list\n")

    def test_missing_steps_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="steps"):
            parse_native_spec("name: nothing here\n")

    def test_empty_steps_list_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="steps"):
            parse_native_spec("steps: []\n")

    def test_step_not_a_mapping_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="steps\\[0\\]"):
            parse_native_spec("steps:\n  - just a string\n")

    def test_unknown_step_type_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="unknown step type"):
            parse_native_spec("steps:\n  - type: teleport\n")

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="key"):
            parse_native_spec("steps:\n  - type: key\n")

    def test_click_missing_x_y_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="x"):
            parse_native_spec("steps:\n  - type: click\n")

    def test_unrecognized_button_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="button"):
            parse_native_spec(
                "steps:\n  - type: click\n    x: 0\n    y: 0\n    button: super-click\n"
            )

    def test_win_native_only_steps_are_unknown_here(self) -> None:
        # #3486's acceptance bar: no GTK-specific spec fork, but also no
        # Win32-only concepts (native HMENU/WM_NCHITTEST) leaking in either
        # — this driver's vocabulary is the shared subset, not a superset.
        with pytest.raises(GtkNativeSpecError, match="unknown step type"):
            parse_native_spec(
                "steps:\n  - type: expect_menu\n    items: [File]\n"
            )
        with pytest.raises(GtkNativeSpecError, match="unknown step type"):
            parse_native_spec(
                "steps:\n  - type: expect_hit\n    x: 0\n    y: 0\n    ht: HTCLOSE\n"
            )


# ── _xdotool_key_for ─────────────────────────────────────────────────────────


class TestXdotoolKeyFor:
    def test_named_keys_case_insensitive(self) -> None:
        assert _xdotool_key_for("enter") == "Return"
        assert _xdotool_key_for("ENTER") == "Return"
        assert _xdotool_key_for("Esc") == "Escape"

    def test_ctrl_combo(self) -> None:
        assert _xdotool_key_for("ctrl+c") == "ctrl+c"

    def test_single_lowercase_letter_passes_through(self) -> None:
        assert _xdotool_key_for("a") == "a"

    def test_single_uppercase_letter_passes_through(self) -> None:
        # Unlike Win32/macOS virtual keycodes, xdotool shifts uppercase
        # keysyms for itself — no separate needs-shift bit to track.
        assert _xdotool_key_for("A") == "A"

    def test_digit_key(self) -> None:
        assert _xdotool_key_for("5") == "5"

    def test_unrecognized_key_raises(self) -> None:
        with pytest.raises(GtkNativeSpecError, match="unrecognized key"):
            _xdotool_key_for("moonwalk")


# ── _find_a11y_match / _summarize_elements ──────────────────────────────────


class TestFindA11yMatch:
    ELEMENTS = [
        {"role": "menu bar", "name": "", "visible": True},
        {"role": "menu item", "name": "File", "visible": True},
        {"role": "menu item", "name": "Edit", "visible": True},
        {"role": "push button", "name": "Close", "visible": False},
    ]

    def test_matches_role_and_name_substring(self) -> None:
        match = _find_a11y_match(self.ELEMENTS, "menu item", "Fil")
        assert match is not None
        assert match["name"] == "File"

    def test_role_match_is_case_insensitive(self) -> None:
        assert _find_a11y_match(self.ELEMENTS, "MENU ITEM", "file") is not None

    def test_empty_name_matches_any(self) -> None:
        assert _find_a11y_match(self.ELEMENTS, "menu bar", "") is not None

    def test_no_match_returns_none(self) -> None:
        assert _find_a11y_match(self.ELEMENTS, "menu item", "Help") is None

    def test_invisible_element_is_skipped(self) -> None:
        assert _find_a11y_match(self.ELEMENTS, "push button", "Close") is None

    def test_summarize_empty_tree(self) -> None:
        assert _summarize_elements([]) == "(empty tree)"

    def test_summarize_non_empty_tree(self) -> None:
        summary = _summarize_elements(self.ELEMENTS)
        assert "File" in summary and "Edit" in summary


# ── a scripted fake GtkCalls, for exercising NativeRunner deterministically ─


class FakeGtkCalls:
    """A scripted :class:`~coord.gtk_native_driver.GtkCalls` (#3486). Every
    real-OS call is recorded so tests can assert exactly what
    :class:`NativeRunner` sent, and every behavior (AT-SPI tree, capture
    bytes, window aliveness) is injected rather than actually touching an
    OS."""

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
        self.moved: list[tuple[int, int, int, int, int]] = []
        self.clicks: list[tuple[int, int, int, str]] = []
        self.keys: list[tuple[int, str]] = []
        self.killed: list[int] = []
        self._next_pid = 1000
        self._window_id = 5555

    def session_available(self) -> tuple[bool, str]:
        return self._session_ok, self._session_reason

    def launch(self, command: str, cwd: str) -> int:
        if self.launch_fails:
            raise GtkNativeRuntimeError("could not start process")
        self.launched.append((command, cwd))
        self._next_pid += 1
        return self._next_pid

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        if self.window_never_appears:
            raise GtkNativeRuntimeError(f"no window for pid={pid}")
        return self._window_id

    def move_window(self, window_id: int, x: int, y: int, width: int, height: int) -> None:
        self.moved.append((window_id, x, y, width, height))

    def is_window_alive(self, window_id: int) -> bool:
        if self._closes_after_n_polls is None:
            return True
        self._alive_poll_count += 1
        return self._alive_poll_count <= self._closes_after_n_polls

    def send_click(self, window_id: int, x: int, y: int, button: str) -> None:
        self.clicks.append((window_id, x, y, button))

    def send_key(self, window_id: int, key: str) -> None:
        self.keys.append((window_id, key))

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


def _runner(calls: FakeGtkCalls, **kwargs) -> NativeRunner:
    return NativeRunner(calls, "./vimcode --gtk", "/cwd", **kwargs)


class TestNativeRunnerLaunch:
    def test_launch_success_reports_passing_entry_and_moves_window(self) -> None:
        calls = FakeGtkCalls()
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")], width=900, height=700))
        assert results == [{"id": "000 launch", "status": "pass", "message": ""}]
        assert calls.launched == [("./vimcode --gtk", "/cwd")]
        assert calls.moved  # window was resized/positioned per the spec

    def test_launch_process_failure_reports_failing_entry(self) -> None:
        calls = FakeGtkCalls(launch_fails=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results[0]["status"] == "fail"
        assert "could not start process" in results[0]["message"]

    def test_launch_window_never_appears_reports_failing_entry(self) -> None:
        calls = FakeGtkCalls(window_never_appears=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results[0]["status"] == "fail"
        assert "no window" in results[0]["message"]

    def test_step_before_launch_fails_with_a_clear_message(self) -> None:
        calls = FakeGtkCalls()
        runner = _runner(calls)
        results = runner.run(_spec([_step("key", key="a")]))
        assert results[0]["status"] == "fail"
        assert "no window" in results[0]["message"]


class TestNativeRunnerSessionPrecheck:
    """#3510: a missing display is reported as a distinct ``unavailable``
    lane verdict, never an ordinary failed step — and no step (not even
    ``launch``) runs when it's missing."""

    def test_missing_display_reports_unavailable_and_runs_no_step(self) -> None:
        calls = FakeGtkCalls(session_ok=False, session_reason="no $DISPLAY set")
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results == [
            {"id": "session", "status": "unavailable", "message": "no $DISPLAY set"}
        ]
        assert calls.launched == []

    def test_missing_display_never_a_failed_step(self) -> None:
        calls = FakeGtkCalls(session_ok=False, session_reason="no display")
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert all(r["status"] != "fail" for r in results)

    def test_available_display_runs_normally(self) -> None:
        calls = FakeGtkCalls(session_ok=True)
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch")]))
        assert results == [{"id": "000 launch", "status": "pass", "message": ""}]
        assert calls.launched


class TestNativeRunnerActions:
    def test_key_step_forwards_to_calls(self) -> None:
        calls = FakeGtkCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0), _step("key", 1, key="enter")]))
        assert calls.keys == [(calls._window_id, "enter")]

    def test_click_step_forwards_coordinates_and_button(self) -> None:
        calls = FakeGtkCalls()
        runner = _runner(calls)
        runner.run(_spec([
            _step("launch", 0), _step("click", 1, x=42, y=7, button="right"),
        ]))
        assert calls.clicks == [(calls._window_id, 42, 7, "right")]

    def test_click_defaults_to_left_button(self) -> None:
        calls = FakeGtkCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0), _step("click", 1, x=1, y=1)]))
        assert calls.clicks == [(calls._window_id, 1, 1, "left")]

    def test_wait_step_sleeps_approximately_ms(self) -> None:
        calls = FakeGtkCalls()
        runner = _runner(calls)
        start = time.monotonic()
        runner.run(_spec([_step("launch", 0), _step("wait", 1, ms=50)]))
        assert time.monotonic() - start >= 0.05

    def test_capture_step_attaches_capture_b64_on_pass(self) -> None:
        calls = FakeGtkCalls(capture_script=[b"one-frame"])
        runner = _runner(calls)
        results = runner.run(_spec([_step("launch", 0), _step("capture", 1)]))
        capture_entry = results[1]
        assert capture_entry["status"] == "pass"
        assert base64.b64decode(capture_entry["capture_b64"]) == b"one-frame"


class TestNativeRunnerExpectA11y:
    def test_match_passes(self) -> None:
        calls = FakeGtkCalls(
            ax_script=[[{"role": "push button", "name": "Close", "visible": True}]]
        )
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="push button", name="Close"),
        ]))
        assert results[1]["status"] == "pass"

    def test_no_match_fails_with_tree_summary(self) -> None:
        calls = FakeGtkCalls(
            ax_script=[[{"role": "push button", "name": "Open", "visible": True}]]
        )
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="push button", name="Close"),
        ]))
        assert results[1]["status"] == "fail"
        assert "Open" in results[1]["message"]

    def test_failing_step_attaches_capture_evidence(self) -> None:
        calls = FakeGtkCalls(
            ax_script=[[]], capture_script=[b"failure-frame"],
        )
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="push button", name="Close"),
        ]))
        assert results[1]["status"] == "fail"
        assert base64.b64decode(results[1]["capture_b64"]) == b"failure-frame"


class TestNativeRunnerExpectA11yWithin:
    def test_appears_within_timeout_reports_elapsed_message(self) -> None:
        calls = FakeGtkCalls(ax_script=[
            [], [], [{"role": "menu item", "name": "Cut", "visible": True}],
        ])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_a11y_within", 1, role="menu item", name="Cut", timeout_ms=2000),
        ]))
        assert results[1]["status"] == "pass"
        assert "ms" in results[1]["message"]

    def test_never_appears_times_out_and_fails(self) -> None:
        calls = FakeGtkCalls(ax_script=[[]])
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0),
            _step("expect_a11y_within", 1, role="menu item", name="Cut", timeout_ms=100),
        ]))
        assert results[1]["status"] == "fail"
        assert "never appeared" in results[1]["message"]


class TestNativeRunnerExpectClosed:
    def test_window_that_closes_passes(self) -> None:
        calls = FakeGtkCalls(closes_after_n_polls=1)
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_closed", 1, timeout_ms=1000),
        ]))
        assert results[1]["status"] == "pass"

    def test_window_that_never_closes_fails(self) -> None:
        # closes_after_n_polls=None -> is_window_alive always True.
        calls = FakeGtkCalls()
        runner = _runner(calls)
        results = runner.run(_spec([
            _step("launch", 0), _step("expect_closed", 1, timeout_ms=100),
        ]))
        assert results[1]["status"] == "fail"
        assert "did not actually close" in results[1]["message"]


class TestNativeRunnerTeardownAndSafety:
    def test_teardown_kills_only_the_launched_pid(self) -> None:
        calls = FakeGtkCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("launch", 0)]))
        assert len(calls.killed) == 1
        launched_pid = calls.killed[0]
        assert launched_pid >= 1001  # FakeGtkCalls' own pid counter

    def test_teardown_does_not_kill_when_launch_never_ran(self) -> None:
        calls = FakeGtkCalls()
        runner = _runner(calls)
        runner.run(_spec([_step("key", 0, key="a")]))  # fails: no launch
        assert calls.killed == []

    def test_teardown_still_kills_when_a_later_step_raises(self) -> None:
        calls = FakeGtkCalls(ax_script=[[]])
        runner = _runner(calls)
        runner.run(_spec([
            _step("launch", 0), _step("expect_a11y", 1, role="push button", name="Close"),
        ]))
        assert len(calls.killed) == 1

    def test_no_name_based_kill_path_exists_in_the_module(self) -> None:
        """Safety invariant from the module docstring: teardown must only
        ever be able to kill the exact PID this driver itself launched —
        never by matching a process by its binary or window-class name. A
        ``pkill``/``killall``-style call, or any process-enumeration used to
        find a victim by name, anywhere in this module's actual CODE (not
        its prose docstrings, which legitimately describe the invariant by
        name) would be one bad assumption away from killing something on a
        shared fleet host's Xvfb session that isn't this driver's own
        child."""
        import ast

        import coord.gtk_native_driver as module

        source = open(module.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        code_only = "\n".join(
            ast.unparse(node) for node in ast.walk(tree)
            if isinstance(node, (ast.Call, ast.Import, ast.ImportFrom))
        ).lower()
        for forbidden in ("killall", "pkill", "os.system", "wmctrl"):
            assert forbidden not in code_only, f"found forbidden pattern {forbidden!r} in actual code"

        # And the one sanctioned kill path's signature is exactly `(self,
        # pid: int)` — not e.g. `(self, pid, name=None)` that a later edit
        # could widen into a by-name fallback.
        import inspect

        sig = inspect.signature(module.LinuxGtkCalls.kill)
        assert list(sig.parameters) == ["self", "pid"]


class TestNativeRunnerDriverLevelTimeout:
    def test_steps_past_the_deadline_are_aborted_not_run(self) -> None:
        calls = FakeGtkCalls()
        runner = NativeRunner(
            calls, "./vimcode --gtk", "/cwd",
            deadline=time.monotonic() - 1,
        )
        results = runner.run(_spec([
            _step("launch", 0), _step("key", 1, key="a"),
        ]))
        assert all(r["status"] == "fail" for r in results)
        assert "driver-level timeout" in results[0]["message"]
        assert calls.launched == []  # never even got to run `launch`


# ── LinuxGtkCalls platform/display guard + optional-dependency guard ───────


class TestLinuxGtkCallsPlatformGuard:
    def test_construction_off_linux_raises(self, monkeypatch) -> None:
        if hasattr(os, "uname") and os.uname().sysname == "Linux":
            monkeypatch.setattr(
                "coord.gtk_native_driver._is_linux", lambda: False
            )
        with pytest.raises(GtkNativeRuntimeError, match="Linux"):
            LinuxGtkCalls()

    def test_construction_without_display_does_not_raise(self, monkeypatch) -> None:
        """#3510: a missing display is an "unavailable" lane verdict, not a
        construction-time crash — see
        ``TestLinuxGtkCallsSessionAvailable`` for the actual check."""
        monkeypatch.setattr("coord.gtk_native_driver._is_linux", lambda: True)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        LinuxGtkCalls()  # must not raise


class TestLinuxGtkCallsSessionAvailable:
    """#3510: :meth:`LinuxGtkCalls.session_available` is the one place that
    decides whether a display is usable — checked by `NativeRunner.run`
    before any step runs."""

    def test_missing_display_and_wayland_display_is_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.gtk_native_driver._is_linux", lambda: True)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        available, reason = LinuxGtkCalls().session_available()
        assert available is False
        assert "DISPLAY" in reason

    def test_display_set_is_available(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.gtk_native_driver._is_linux", lambda: True)
        monkeypatch.setenv("DISPLAY", ":99")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        available, reason = LinuxGtkCalls().session_available()
        assert (available, reason) == (True, "")

    def test_wayland_display_alone_is_available(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.gtk_native_driver._is_linux", lambda: True)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        available, reason = LinuxGtkCalls().session_available()
        assert (available, reason) == (True, "")


class TestImportAtspi:
    def test_missing_gi_raises_actionable_error(self, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "gi":
                raise ModuleNotFoundError("no module named gi")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        from coord.gtk_native_driver import _import_atspi

        with pytest.raises(GtkNativeRuntimeError, match="AT-SPI"):
            _import_atspi()


# ── run_native_spec top-level entry point ───────────────────────────────────


class TestRunNativeSpec:
    def test_runs_against_an_injected_fake_and_returns_normalized_tests(self) -> None:
        calls = FakeGtkCalls(
            ax_script=[[{"role": "push button", "name": "Close", "visible": True}]]
        )
        spec_text = (
            "steps:\n"
            "  - type: launch\n"
            "  - type: expect_a11y\n"
            "    id: close-button\n"
            "    role: push button\n"
            "    name: Close\n"
        )
        tests = run_native_spec(
            spec_text, launch_command="./vimcode --gtk", cwd="/repo", calls=calls,
        )
        assert tests == [
            {"id": "000 launch", "status": "pass", "message": ""},
            {"id": "close-button", "status": "pass", "message": ""},
        ]

    def test_malformed_spec_raises_spec_error_before_any_launch(self) -> None:
        calls = FakeGtkCalls()
        with pytest.raises(GtkNativeSpecError):
            run_native_spec(
                "steps: []\n", launch_command="./vimcode --gtk", cwd="/repo", calls=calls,
            )
        assert calls.launched == []
