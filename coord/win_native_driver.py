"""``win-native`` acceptance driver — Tier 2's Windows-native tier (#3484).

``tui-pty`` (#3483) proved that an in-process harness can't see real terminal
byte behaviour. This module is the next rung down: on 2026-09-29 vimcode's
Windows window had a real native menu attached (``GetMenu`` -> 7 items) that
was invisible because quadraui#1199's custom caption left 0 px of
non-client area, and the window had no min/max/close — both passed *every*
test at the ``tui-tuidriver``/``tui-pty`` tiers, because neither ever asks
the real OS "is there a menu bar here?" or "what does a mouse click at this
pixel actually hit?". Those are Win32 questions, answerable only by asking
Win32.

This driver launches the driven repo's real compiled GUI/TUI exe, finds its
real top-level window, and probes it with real OS calls:

- ``GetMenu``/``GetMenuItemCount``/``GetMenuString`` — does a native menu
  bar exist, and what's on it.
- ``WM_NCHITTEST`` — what does the OS think is at a given screen point
  (``HTCLOSE``, ``HTCAPTION``, ``HTCLIENT``, ...).
- the UI Automation tree — focusable/named elements by role+name, the same
  way a screen reader (or a real user tabbing through the app) would see
  it.
- ``SendInput``/``PostMessage`` — real input, not an in-process event queue.
- ``PrintWindow`` — captures the window even when it's covered by another
  one, attached as evidence to every *failing* step (see
  :meth:`NativeRunner._attach_capture_if_possible`).

**Injectable OS-call seam.** Every actual Win32/UIA call is one method on
the :class:`WinCalls` protocol, implemented for real by :class:`Win32Calls`
(Windows-only, ``ctypes`` + the optional ``comtypes`` UI Automation client —
the ``win-native`` extra). :class:`NativeRunner` — the spec-step executor —
never calls a Win32 API directly; it only calls through ``WinCalls``. This
is the same seam :mod:`coord.tui_pty_driver` uses for ``PtyChild``: it is
what makes the spec-to-Win32 *translation* logic (parsing, step sequencing,
pass/fail, capture-on-failure) unit-testable on any platform, with a scripted
fake standing in for the OS (see ``tests/test_win_native_driver.py``) — a
real run against a real exe on real Windows hardware (dell64) is out of
reach for this repo's own test suite and is exercised at the operator level,
the same split :mod:`coord.tui_pty_driver`'s own docstring calls out for
ConPTY.

**Safety: kill only the PID this driver itself launched.** :meth:`WinCalls.kill`
takes a ``pid: int`` — the exact process id :meth:`WinCalls.launch`/
:meth:`WinCalls.launch_in_terminal` returned — and nothing in this module
ever looks a process up by its executable's own filename to terminate it.
Windows Terminal and conhost are both things an operator is very likely
also running their *own* session in; a teardown that matched on a shared
host process's filename would be one bad assumption away from killing the
operator's own work, not just this driver's child. See
``tests/test_win_native_driver.py``'s
``test_no_image_name_kill_path_exists_in_the_module`` — a source-level
regression guard, not just a behavioral one.

**UNC ``cwd`` (#3543).** A WSL-hosted agent's repo worktree translates
(``coord.win_native_bridge.translate_to_windows_path``) to a UNC path
(``\\wsl.localhost\\Ubuntu-24.04\\...``) — the normal case for dell64, this
fleet's only WSL-hosted ``windows``-capability agent. ``cmd.exe`` (what
``subprocess.Popen(..., shell=True)`` always launches on Windows)
categorically refuses a UNC current directory at its own startup and
silently falls back to ``%windir%`` instead, breaking every relative path
in ``run_command``. :func:`_popen_command_and_cwd` folds cmd.exe's own
``pushd`` UNC-to-drive-letter workaround into the launched command for a
UNC ``cwd`` rather than ever handing cmd.exe one directly; see
:class:`Win32Calls`'s :meth:`~Win32Calls.launch`/
:meth:`~Win32Calls.launch_in_terminal`.

**Local-filesystem staging (#3617).** ``pushd``-into-a-UNC-path (above)
only stops cmd.exe's own startup refusal — it does nothing about the cost
of actually *running* from one. Once launched, Windows reads the exe and
every file it touches (a route's fixture/working files) over ``\\wsl$``'s
9P protocol, which the dell64 operator found "horrible" (2026-10-05) next
to real local NTFS. Whenever ``cwd`` is a UNC path AND the launch command
is a shape this driver can parse with confidence (:func:`_plan_staging`'s
own docstring names exactly which two shapes qualify — a bare leading exe
token, optionally preceded by one ``cd <fixture-dir> && ``, the fleet's
own convention for entering a route's working directory first), both
:meth:`Win32Calls.launch` and :meth:`~Win32Calls.launch_in_terminal` copy
the resolved exe (plus that fixture dir, and/or a conventional ``.smoke``
one, when present) onto the real local Windows filesystem — a fresh
``%LOCALAPPDATA%\\Temp\\coord-app-drive\\<session>\\`` directory — and
launch from there instead, with ``cwd`` rewritten to match. Anything more
complex (a second shell operator, an absolute/off-``cwd`` exe) is left
alone, falling back to the ``pushd`` wrap above unchanged — this driver
only ever rewrites the ONE leading executable token of a command it can
parse with confidence, never an opaque shell pipeline.

:meth:`Win32Calls.kill` deletes the staged session directory it created,
once it has signalled that launch's own pid — the normal (``close``/
``NativeRunner`` teardown) path. There is no Windows analogue of #3583's
``PR_SET_PDEATHSIG`` for "also delete a directory" on an abnormal (e.g.
SIGKILLed) exit, so :meth:`~Win32Calls.launch`/
:meth:`~Win32Calls.launch_in_terminal` additionally sweep (and delete) any
session directory older than a day on every call — the same "idle
self-expiry is the backstop, the explicit teardown is the normal path"
shape :data:`coord.app_drive.DEFAULT_IDLE_TIMEOUT` already uses, just
applied to a directory instead of a process.

**Spec steps** (:func:`parse_native_spec`, YAML — the ``win-native``
sibling of ``tui-pty``'s smoke spec):

- ``launch`` — start the exe (or, in terminal-hosted mode, the exe inside a
  real terminal host — see below), find its real top-level window, and size
  it deterministically via ``MoveWindow``.
- ``key: <name>`` / ``click: {x, y, button}`` — real input at *screen*
  pixel coordinates relative to the window's client origin, via
  :meth:`WinCalls.send_key`/:meth:`WinCalls.send_click`.
- ``wait: {ms}`` — a plain deterministic pause.
- ``capture`` — an explicit ``PrintWindow`` evidence snapshot, attached to
  this step's own result (pass or fail) as ``capture_b64``.
- ``expect_menu: {items, exact}`` — ``GetMenu`` must return a real native
  menu (not ``NULL``) whose item labels contain (or, with ``exact: true``,
  exactly equal) *items* — the vimcode#1199/#1228 regression check.
- ``expect_hit: {x, y, ht}`` — ``WM_NCHITTEST`` at ``(x, y)`` must equal
  *ht* (one of the named ``HT*`` hit-test codes, e.g. ``HTCLOSE``).
- ``expect_a11y: {role, name}`` — the UI Automation tree must contain a
  visible element matching *role* (exact, case-insensitive) and *name*
  (substring, case-insensitive) right now.
- ``expect_a11y_within: {role, name, timeout_ms}`` — the same match, but
  polled repeatedly until it appears or *timeout_ms* elapses.
- ``expect_closed: {timeout_ms}`` — the window must actually stop existing
  (``IsWindow`` re-polled until false) within *timeout_ms*. This is the
  "Close actually closes the window" check (quadraui#1228) — per #2096, a
  click is confirmed closed by *observing the window gone afterward*, never
  by the mere fact that the click was sent without an exception.

**Terminal-hosted mode** (added 2026-09-30, vimcode#1634-#1636: three bugs
that all pass under a raw ConPTY — ``tui-pty``'s own tier — yet reproduce
for an operator in a real terminal *window*, because the remaining cause
lives in the terminal emulator layer, which only a window-level driver can
see). ``mode: terminal`` plus ``terminal_app: windows-terminal|conhost``
launches the TUI binary inside a real Windows Terminal or legacy conhost
window rather than probing the exe's own top-level window directly, and adds
three more steps:

- ``expect_idle_stable: {ms, interval_ms}`` — ``PrintWindow`` the terminal
  window repeatedly every *interval_ms* (default 100) across a full *ms*
  window (default 5000) and fail if any two consecutive captures differ —
  the vimcode#1634 idle-flicker oracle: a genuinely idle terminal produces
  byte-identical repaints; a ~1 Hz flicker does not.
- ``expect_menu_latency: {x, y, button, role, name, max_ms}`` — real-click
  (default ``button: right``) at ``(x, y)``, then time until a matching UI
  Automation element (default ``role: MenuItem``) appears, failing if it
  never does within *max_ms* — the vimcode#1635 right-click-menu-latency
  oracle.
- ``expect_panel_switch: {x, y, role, name, timeout_ms}`` — real-click at
  ``(x, y)`` (an activity-bar icon), then assert a matching UI Automation
  element (the switched-to panel) appears within *timeout_ms* — the
  vimcode#1636 dead-activity-bar-click oracle.

Both of the latter two perform their own triggering click as part of the
step — the latency/switch being measured starts at that exact ``SendInput``
call, not at some earlier unrelated step, since a separately-timed
``click`` step would leave an unbounded, unmeasured gap between the input
and the start of the timing window.

**Locked/absent session precheck (#3510).** On dell64, vimcode#1629's real-
Windows check could not verify its visual criteria because the session was
locked (``GetForegroundWindow() == NULL``, ``LogonUI`` running in session 1);
vimcode#1558/#1561/#1622 record the same. A locked desktop is an
environment condition, not an app bug, so before :meth:`NativeRunner.run`
launches anything it calls :meth:`WinCalls.session_available` — real
checks: ``OpenInputDesktop`` succeeds, ``WTSGetActiveConsoleSessionId``
reports an active console session, and no ``LogonUI.exe`` is running in
that session. When unavailable, the run returns a single
``status="unavailable"`` result — never a ``"fail"`` — and no step (not
even ``launch``) runs. :func:`coord.bugbash.run_bugbash` skips such a lane
rather than exploring it, and :mod:`coord.release_gate` reports it as
blocking-but-unavailable rather than failed.
"""

from __future__ import annotations

import base64
import ntpath
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import yaml


class WinNativeSpecError(Exception):
    """Raised for a malformed native spec: invalid YAML, a missing/empty
    ``steps:`` list, an unknown step ``type``, a missing required field, an
    unrecognized button/hit-test-code/mode/terminal_app, or a ``mode:
    terminal`` spec missing ``terminal_app:``."""


class WinNativeRuntimeError(Exception):
    """Raised when the native driver itself can't run: not on Windows, a
    missing optional dependency (the ``win-native`` extra), the launched
    process/window never appearing, or a step referencing a window before
    any ``launch`` step ran."""


# ── native spec model ───────────────────────────────────────────────────────

_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "launch": (),
    "key": ("key",),
    "click": ("x", "y"),
    "wait": ("ms",),
    "capture": (),
    "expect_menu": ("items",),
    "expect_hit": ("x", "y", "ht"),
    "expect_a11y": ("role", "name"),
    "expect_a11y_within": ("role", "name"),
    "expect_closed": (),
    "expect_idle_stable": (),
    "expect_menu_latency": ("x", "y"),
    "expect_panel_switch": ("x", "y", "role", "name"),
}

_VALID_BUTTONS = ("left", "right", "middle")
_VALID_MODES = ("window", "terminal")
_VALID_TERMINAL_APPS = ("windows-terminal", "conhost")

# Named `WM_NCHITTEST` return codes a spec author can reference by name
# rather than a raw integer — the actual Win32 `HT*` constants, both
# directions of this mapping live in :data:`_HT_CODES` below (shared with
# :class:`Win32Calls`'s real translation).
_HT_CODES: dict[str, int] = {
    "HTERROR": -2, "HTTRANSPARENT": -1, "HTNOWHERE": 0, "HTCLIENT": 1,
    "HTCAPTION": 2, "HTSYSMENU": 3, "HTGROWBOX": 4, "HTSIZE": 4, "HTMENU": 5,
    "HTHSCROLL": 6, "HTVSCROLL": 7, "HTMINBUTTON": 8, "HTMAXBUTTON": 9,
    "HTLEFT": 10, "HTRIGHT": 11, "HTTOP": 12, "HTTOPLEFT": 13,
    "HTTOPRIGHT": 14, "HTBOTTOM": 15, "HTBOTTOMLEFT": 16, "HTBOTTOMRIGHT": 17,
    "HTBORDER": 18, "HTREDUCE": 8, "HTZOOM": 9, "HTSIZEFIRST": 10,
    "HTSIZELAST": 17, "HTOBJECT": 19, "HTCLOSE": 20, "HTHELP": 21,
}
_HT_CODES_BY_VALUE: dict[int, str] = {v: k for k, v in reversed(list(_HT_CODES.items()))}


@dataclass(frozen=True)
class NativeStep:
    """One parsed step of a win-native spec. Mirrors
    :class:`coord.tui_pty_driver.SmokeStep`'s "every unused field keeps its
    default" shape — callers never have to branch on ``kind`` before reading
    one."""

    kind: str
    index: int
    id: str = ""
    key: str = ""
    x: int = 0
    y: int = 0
    button: str = ""
    ms: int = 0
    interval_ms: int = 100
    timeout_ms: int = 5000
    max_ms: int = 2000
    ht: str = ""
    role: str = ""
    name: str = ""
    items: tuple[str, ...] = ()
    exact: bool = False

    @property
    def step_id(self) -> str:
        return self.id or f"{self.index:03d} {self.kind}"


@dataclass(frozen=True)
class NativeSpec:
    name: str
    width: int
    height: int
    mode: str
    terminal_app: str
    steps: tuple[NativeStep, ...]


def _int_default(value, default: int) -> int:
    """``int(value)``, falling back to *default* only when *value* is
    absent (``None``) — an explicit literal ``0`` in the YAML is honored
    rather than silently treated as "absent" (mirrors
    :func:`coord.tui_pty_driver._int_default`)."""
    return default if value is None else int(value)


def parse_native_spec(yaml_text: str) -> NativeSpec:
    """Parse a win-native spec YAML document into a :class:`NativeSpec`.

    Top level: ``name:`` (optional), ``width:``/``height:`` (optional,
    default 1024x768 — the ``MoveWindow`` target size), ``mode:``
    (``window`` (default) or ``terminal``), ``terminal_app:`` (required
    when ``mode: terminal`` — ``windows-terminal`` or ``conhost``),
    ``steps:`` — a non-empty list of mappings each carrying a ``type:``
    from :data:`_REQUIRED_FIELDS`.

    Raises :class:`WinNativeSpecError` — never returns a partially-parsed
    spec — for: invalid YAML, a non-mapping document, a missing/empty/
    non-list ``steps:``, a step that isn't a mapping, an unknown ``type:``,
    a step missing one of its type's required fields, an unrecognized
    ``button:``/``ht:``/``mode:``/``terminal_app:``, an ``expect_menu``
    with an empty ``items:`` list, or ``mode: terminal`` with no
    ``terminal_app:`` given.
    """
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise WinNativeSpecError(f"native spec is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise WinNativeSpecError("native spec must be a YAML mapping at the top level")

    mode = str(raw.get("mode", "window") or "window")
    if mode not in _VALID_MODES:
        raise WinNativeSpecError(
            f"unrecognized mode {mode!r} — expected one of {', '.join(_VALID_MODES)}"
        )
    terminal_app = str(raw.get("terminal_app", "") or "")
    if mode == "terminal":
        if not terminal_app:
            raise WinNativeSpecError(
                "mode: terminal requires 'terminal_app:' (windows-terminal or conhost)"
            )
        if terminal_app not in _VALID_TERMINAL_APPS:
            raise WinNativeSpecError(
                f"unrecognized terminal_app {terminal_app!r} — expected one "
                f"of {', '.join(_VALID_TERMINAL_APPS)}"
            )

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise WinNativeSpecError("native spec must have a non-empty 'steps:' list")

    steps: list[NativeStep] = []
    for i, entry in enumerate(steps_raw):
        if not isinstance(entry, dict):
            raise WinNativeSpecError(f"steps[{i}] must be a mapping")
        kind = entry.get("type")
        if kind not in _REQUIRED_FIELDS:
            raise WinNativeSpecError(
                f"steps[{i}]: unknown step type {kind!r} — expected one of "
                f"{', '.join(sorted(_REQUIRED_FIELDS))}"
            )
        missing = [f for f in _REQUIRED_FIELDS[kind] if entry.get(f) in (None, "")]
        if missing:
            raise WinNativeSpecError(
                f"steps[{i}] (type={kind!r}) is missing required field(s): "
                f"{', '.join(missing)}"
            )

        button = str(entry.get("button", "") or "")
        if button and button not in _VALID_BUTTONS:
            raise WinNativeSpecError(
                f"steps[{i}]: unrecognized button {button!r} — expected one "
                f"of {', '.join(_VALID_BUTTONS)}"
            )

        ht = str(entry.get("ht", "") or "")
        if kind == "expect_hit" and ht not in _HT_CODES:
            raise WinNativeSpecError(
                f"steps[{i}]: unrecognized hit-test code {ht!r} — expected "
                f"one of {', '.join(sorted(_HT_CODES))}"
            )

        items_raw = entry.get("items")
        if kind == "expect_menu":
            if not isinstance(items_raw, list) or not items_raw:
                raise WinNativeSpecError(
                    f"steps[{i}] (type='expect_menu') must have a non-empty "
                    f"'items:' list"
                )

        steps.append(NativeStep(
            kind=kind,
            index=i,
            id=str(entry.get("id", "") or ""),
            key=str(entry.get("key", "") or ""),
            x=_int_default(entry.get("x"), 0),
            y=_int_default(entry.get("y"), 0),
            button=button,
            ms=_int_default(entry.get("ms"), 0),
            interval_ms=_int_default(entry.get("interval_ms"), 100),
            timeout_ms=_int_default(entry.get("timeout_ms"), 5000),
            max_ms=_int_default(entry.get("max_ms"), 2000),
            ht=ht,
            role=str(entry.get("role", "") or ""),
            name=str(entry.get("name", "") or ""),
            items=tuple(str(i) for i in items_raw) if isinstance(items_raw, list) else (),
            exact=bool(entry.get("exact", False)),
        ))

    return NativeSpec(
        name=str(raw.get("name", "") or ""),
        width=int(raw.get("width", 1024) or 1024),
        height=int(raw.get("height", 768) or 768),
        mode=mode,
        terminal_app=terminal_app,
        steps=tuple(steps),
    )


# ── the injectable OS-call seam ─────────────────────────────────────────────

class WinCalls(Protocol):
    """The minimal set of real-OS operations :class:`NativeRunner` drives a
    native app through — implemented for real by :class:`Win32Calls`
    (Windows-only), and by a scripted fake in
    ``tests/test_win_native_driver.py`` so the spec-to-Win32 *translation*
    logic is testable on any platform.

    ``launch``/``launch_in_terminal`` return the PID of the process THIS
    call started; :meth:`kill` must accept only that same PID back — never
    an image name — so teardown can never take down a process it didn't
    itself launch (see the module docstring's safety note).
    """

    def launch(self, command: str, cwd: str) -> int: ...

    def launch_in_terminal(self, command: str, cwd: str, terminal_app: str) -> int: ...

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        """Poll for *pid*'s (or one of its descendant processes') real
        top-level window, returning its handle once found. Raises
        :class:`WinNativeRuntimeError` if none appears within *timeout_s* —
        this is the "launch" step's own confirmation that the process
        didn't just start, but actually produced a window (#2096).

        *pid* is not always the real app's own pid: ``subprocess.Popen(...,
        shell=True)`` on Windows spawns ``cmd.exe`` as the immediate child
        and returns *its* pid, while the real app (e.g. ``vimcode.exe``) is
        a grandchild with a different pid that ``cmd.exe`` itself never
        owns a window for (#3542). Implementations must search *pid*'s
        whole descendant-process tree, not just *pid* itself."""
        ...

    def move_window(self, hwnd: int, x: int, y: int, width: int, height: int) -> None: ...

    def is_window_alive(self, hwnd: int) -> bool: ...

    def get_menu_items(self, hwnd: int) -> list[str] | None:
        """``GetMenu``'s item labels, or ``None`` when the window has no
        native menu attached at all (a ``NULL`` HMENU)."""
        ...

    def hit_test(self, hwnd: int, x: int, y: int) -> str:
        """The named ``HT*`` result of ``WM_NCHITTEST`` at screen point
        ``(x, y)``."""
        ...

    def send_click(self, hwnd: int, x: int, y: int, button: str) -> None: ...

    def send_key(self, hwnd: int, key: str) -> None: ...

    def uia_elements(self, hwnd: int) -> list[dict]:
        """Every element in the window's UI Automation tree right now, each
        as ``{"role": str, "name": str, "visible": bool}``."""
        ...

    def capture(self, hwnd: int) -> bytes:
        """A ``PrintWindow`` capture of *hwnd* right now (works even when
        covered by another window) — raises :class:`WinNativeRuntimeError`
        on failure rather than returning empty bytes, since a capture step
        exists specifically to produce evidence and has nothing to report
        if it can't."""
        ...

    def kill(self, pid: int) -> None: ...

    def session_available(self) -> tuple[bool, str]:
        """``(True, "")`` when an unlocked interactive Windows session is
        present for this driver to launch into; ``(False, reason)`` when it
        is locked or absent (#3510) — checked by :meth:`NativeRunner.run`
        BEFORE any step (including ``launch``) runs, so a locked/absent
        session is reported as ``status="unavailable"`` rather than a failed
        step. Never raises — a probe failure here is itself an
        "unavailable" verdict, not a crash."""
        ...


def _find_a11y_match(elements: list[dict], role: str, name: str) -> dict | None:
    """The first *elements* entry whose ``role`` matches exactly
    (case-insensitive) and ``name`` matches as a substring
    (case-insensitive), and which is not explicitly marked invisible — or
    ``None`` if nothing matches. An empty *name* matches any name."""
    role_l = role.lower()
    name_l = name.lower()
    for el in elements:
        if not isinstance(el, dict):
            continue
        if el.get("visible") is False:
            continue
        if str(el.get("role", "")).lower() != role_l:
            continue
        if name_l and name_l not in str(el.get("name", "")).lower():
            continue
        return el
    return None


def _summarize_elements(elements: list[dict]) -> str:
    return ", ".join(
        f"{el.get('role', '?')}:{el.get('name', '')!r}"
        for el in elements if isinstance(el, dict)
    ) or "(empty tree)"


# ── the spec-step executor ──────────────────────────────────────────────────

class NativeRunner:
    """Drives one :class:`NativeSpec` against an injected :class:`WinCalls`,
    producing coord's normalized ``{"id", "status", "message"}`` verdict
    list (plus ``capture_b64`` on failing steps when a capture could be
    taken) — the same shape :func:`coord.tui_pty_driver.run_smoke_spec`
    already produces.
    """

    def __init__(
        self, calls: WinCalls, command: str, cwd: str, *, deadline: float | None = None,
    ) -> None:
        self._calls = calls
        self._command = command
        self._cwd = cwd
        self._deadline = deadline
        self._spec: NativeSpec | None = None
        self._pid: int | None = None
        self._hwnd: int | None = None
        # Set by a handler that already has the exact failing capture in
        # hand (e.g. `expect_idle_stable`'s differing frame) so
        # `_attach_capture_if_possible` doesn't take a second, less
        # representative capture after the fact.
        self._pending_failure_capture: bytes | None = None

    def run(self, spec: NativeSpec) -> list[dict]:
        """Run *spec*, first checking :meth:`WinCalls.session_available`
        (#3510). A locked or absent interactive session is an environment
        condition, not an app bug: when unavailable, this returns a single
        ``status="unavailable"`` entry and runs NO step at all (not even
        ``launch``) — never folding it into an ordinary ``"fail"``."""
        self._spec = spec
        available, reason = self._calls.session_available()
        if not available:
            return [{
                "id": "session",
                "status": "unavailable",
                "message": reason or "no interactive Windows session is available",
            }]
        results: list[dict] = []
        try:
            for step in spec.steps:
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    results.append({
                        "id": step.step_id, "status": "fail",
                        "message": "aborted: win-native driver-level timeout exceeded",
                    })
                    continue
                results.append(self._run_step(step))
        finally:
            self._teardown()
        return results

    def _run_step(self, step: NativeStep) -> dict:
        handlers = {
            "launch": self._do_launch,
            "key": self._do_key,
            "click": self._do_click,
            "wait": self._do_wait,
            "capture": self._do_capture,
            "expect_menu": self._do_expect_menu,
            "expect_hit": self._do_expect_hit,
            "expect_a11y": self._do_expect_a11y,
            "expect_a11y_within": self._do_expect_a11y_within,
            "expect_closed": self._do_expect_closed,
            "expect_idle_stable": self._do_expect_idle_stable,
            "expect_menu_latency": self._do_expect_menu_latency,
            "expect_panel_switch": self._do_expect_panel_switch,
        }
        entry: dict = {"id": step.step_id, "status": "pass", "message": ""}
        try:
            extra = handlers[step.kind](step)
            if extra:
                entry.update(extra)
        except (WinNativeSpecError, WinNativeRuntimeError, AssertionError) as e:
            entry["status"] = "fail"
            entry["message"] = str(e)
            self._attach_capture_if_possible(entry)
        return entry

    def _attach_capture_if_possible(self, entry: dict) -> None:
        """Best-effort ``PrintWindow`` evidence attached to *entry* — never
        raises, and never masks the real failure reason in ``message`` if
        the capture itself can't be taken."""
        image = self._pending_failure_capture
        self._pending_failure_capture = None
        if image is None and self._hwnd is not None:
            try:
                image = self._calls.capture(self._hwnd)
            except Exception as e:  # noqa: BLE001 — evidence is best-effort
                entry["capture_error"] = str(e)
                return
        if image:
            entry["capture_b64"] = base64.b64encode(image).decode("ascii")

    def _require_hwnd(self) -> int:
        if self._hwnd is None:
            raise WinNativeRuntimeError(
                "no window — spec has no 'launch' step before this one"
            )
        return self._hwnd

    # -- action steps --

    def _do_launch(self, step: NativeStep) -> dict | None:
        spec = self._spec
        assert spec is not None
        if spec.mode == "terminal":
            pid = self._calls.launch_in_terminal(self._command, self._cwd, spec.terminal_app)
        else:
            pid = self._calls.launch(self._command, self._cwd)
        self._pid = pid
        timeout_s = (step.timeout_ms or 10000) / 1000
        hwnd = self._calls.find_top_window(pid, timeout_s)
        self._hwnd = hwnd
        self._calls.move_window(hwnd, 0, 0, spec.width, spec.height)
        return None

    def _do_key(self, step: NativeStep) -> None:
        self._calls.send_key(self._require_hwnd(), step.key)

    def _do_click(self, step: NativeStep) -> None:
        self._calls.send_click(self._require_hwnd(), step.x, step.y, step.button or "left")

    def _do_wait(self, step: NativeStep) -> None:
        time.sleep(step.ms / 1000)

    def _do_capture(self, step: NativeStep) -> dict:
        image = self._calls.capture(self._require_hwnd())
        return {"capture_b64": base64.b64encode(image).decode("ascii")}

    # -- assertion steps --

    def _do_expect_menu(self, step: NativeStep) -> None:
        items = self._calls.get_menu_items(self._require_hwnd())
        if items is None:
            raise AssertionError(
                "GetMenu returned no native menu (NULL) — window has no "
                "menu bar attached"
            )
        if step.exact:
            if list(items) != list(step.items):
                raise AssertionError(
                    f"expected menu items {list(step.items)!r} exactly, got {items!r}"
                )
        else:
            missing = [i for i in step.items if i not in items]
            if missing:
                raise AssertionError(
                    f"expected menu to contain {missing!r}; actual menu "
                    f"items: {items!r}"
                )

    def _do_expect_hit(self, step: NativeStep) -> None:
        actual = self._calls.hit_test(self._require_hwnd(), step.x, step.y)
        if actual != step.ht:
            raise AssertionError(
                f"WM_NCHITTEST at ({step.x},{step.y}) expected {step.ht}, got {actual}"
            )

    def _do_expect_a11y(self, step: NativeStep) -> None:
        elements = self._calls.uia_elements(self._require_hwnd())
        if _find_a11y_match(elements, step.role, step.name) is None:
            raise AssertionError(
                f"no UI Automation element found with role={step.role!r} "
                f"name={step.name!r}; tree had: {_summarize_elements(elements)}"
            )

    def _do_expect_a11y_within(self, step: NativeStep) -> dict:
        hwnd = self._require_hwnd()
        start = time.monotonic()
        deadline = start + step.timeout_ms / 1000
        while True:
            elements = self._calls.uia_elements(hwnd)
            if _find_a11y_match(elements, step.role, step.name) is not None:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {"message": f"appeared after {elapsed_ms}ms"}
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"expected role={step.role!r} name={step.name!r} within "
                    f"{step.timeout_ms}ms; never appeared. tree had: "
                    f"{_summarize_elements(elements)}"
                )
            time.sleep(0.02)

    def _do_expect_closed(self, step: NativeStep) -> None:
        """#2096: confirmed by re-polling `IsWindow` until it actually
        reports gone — never by the mere absence of an exception from an
        earlier click step."""
        hwnd = self._require_hwnd()
        deadline = time.monotonic() + (step.timeout_ms or 5000) / 1000
        while True:
            if not self._calls.is_window_alive(hwnd):
                return
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"window still alive {step.timeout_ms}ms after "
                    f"expect_closed — it did not actually close"
                )
            time.sleep(0.02)

    def _do_expect_idle_stable(self, step: NativeStep) -> None:
        """vimcode#1634's idle-flicker oracle: repeated ``PrintWindow``
        captures across a real-time window must be byte-identical. Only
        answerable by actually taking the captures and comparing them
        afterward (#2096) — nothing here can raise mid-wait, so the mere
        absence of an exception during the loop proves nothing on its own;
        the comparison after each new capture is the actual check."""
        hwnd = self._require_hwnd()
        interval_s = (step.interval_ms or 100) / 1000
        deadline = time.monotonic() + (step.ms or 5000) / 1000
        prev = self._calls.capture(hwnd)
        elapsed_captures = 1
        while time.monotonic() < deadline:
            time.sleep(interval_s)
            current = self._calls.capture(hwnd)
            elapsed_captures += 1
            if current != prev:
                self._pending_failure_capture = current
                raise AssertionError(
                    f"capture #{elapsed_captures} differs from the previous "
                    f"one taken {step.interval_ms}ms earlier while the app "
                    f"should have been idle (flicker)"
                )
            prev = current

    def _do_expect_menu_latency(self, step: NativeStep) -> dict:
        """vimcode#1635's right-click-menu-latency oracle: the timing
        window starts at THIS step's own click, not an earlier one, so
        there's no unmeasured gap between the real input and the start of
        the clock."""
        hwnd = self._require_hwnd()
        role = step.role or "MenuItem"
        start = time.monotonic()
        self._calls.send_click(hwnd, step.x, step.y, step.button or "right")
        deadline = start + (step.max_ms or 2000) / 1000
        while True:
            elements = self._calls.uia_elements(hwnd)
            if _find_a11y_match(elements, role, step.name) is not None:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {"message": f"menu appeared {elapsed_ms}ms after right-click"}
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"no menu (role={role!r}) appeared within {step.max_ms}ms "
                    f"of clicking ({step.x},{step.y})"
                )
            time.sleep(0.01)

    def _do_expect_panel_switch(self, step: NativeStep) -> dict:
        """vimcode#1636's dead-activity-bar-click oracle — same
        click-starts-the-clock reasoning as :meth:`_do_expect_menu_latency`."""
        hwnd = self._require_hwnd()
        start = time.monotonic()
        self._calls.send_click(hwnd, step.x, step.y, step.button or "left")
        deadline = start + (step.timeout_ms or 5000) / 1000
        while True:
            elements = self._calls.uia_elements(hwnd)
            if _find_a11y_match(elements, step.role, step.name) is not None:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {"message": f"panel appeared {elapsed_ms}ms after activity-bar click"}
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"activity-bar click at ({step.x},{step.y}) never "
                    f"switched to role={step.role!r} name={step.name!r} "
                    f"within {step.timeout_ms}ms"
                )
            time.sleep(0.02)

    # -- teardown --

    def _teardown(self) -> None:
        """Kills only the PID this run itself launched (see the module
        docstring's safety note) — never by image name, and never if
        `launch` never ran (`self._pid` stays `None`)."""
        if self._pid is not None:
            try:
                self._calls.kill(self._pid)
            except Exception:  # noqa: BLE001 — teardown must not mask the real result
                pass


# ── real Win32 implementation (Windows-only) ────────────────────────────────

_NAMED_VKEYS: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "backspace": 0x08, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "delete": 0x2E, "insert": 0x2D,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def _vkey_for(key: str) -> tuple[int, bool]:
    """``(virtual_key_code, needs_shift)`` for one spec ``key:`` name.
    Raises :class:`WinNativeSpecError` for anything unrecognized."""
    lowered = key.lower()
    if lowered in _NAMED_VKEYS:
        return _NAMED_VKEYS[lowered], False
    if lowered.startswith("ctrl+") and len(lowered) == 6:
        return ord(lowered[5].upper()), False
    if len(key) == 1:
        needs_shift = key.isalpha() and key.isupper()
        return ord(key.upper()), needs_shift
    raise WinNativeSpecError(f"unrecognized key {key!r}")


def _is_unc_path(path: str) -> bool:
    """True for a UNC path (``\\\\server\\share\\...``) — the shape
    :func:`coord.win_native_bridge.translate_to_windows_path` returns for a
    WSL-hosted repo (``\\\\wsl.localhost\\<distro>\\...``, #3543)."""
    return path.startswith("\\\\")


def _popen_command_and_cwd(command: str, cwd: str) -> tuple[str, str | None]:
    """Resolve the ``command``/``cwd`` pair to actually hand
    ``subprocess.Popen(..., shell=True)`` (#3543).

    ``shell=True`` on Windows always runs *command* via ``cmd.exe /c`` —
    and cmd.exe's own startup categorically refuses a UNC current
    directory: confirmed directly (``cmd.exe /c "cd \\\\wsl.localhost\\...
    && dir"`` prints "CMD does not support UNC paths as current
    directories.") and **silently** falls back to ``%windir%``
    (``C:\\Windows\\System32``) instead of raising — this is cmd.exe's own
    initialization check, not a ``CreateProcess``/``lpCurrentDirectory``
    limitation (that accepts a UNC path fine, which is exactly why the
    failure is silent: nothing downstream of ``Popen`` ever sees an error,
    every relative path in *command* just silently resolves against the
    wrong directory instead).

    For a UNC *cwd* this folds cmd.exe's own standard UNC workaround —
    ``pushd`` (maps an unused drive letter to the UNC path, cds into it) —
    into *command* itself, and returns ``cwd=None`` so ``Popen`` never
    hands cmd.exe a UNC starting directory it cannot use in the first
    place. A non-UNC (drive-letter) *cwd* is returned unchanged — the
    common case, where ``Popen``'s own ``cwd=`` already works correctly.
    """
    if cwd and _is_unc_path(cwd):
        return f'pushd "{cwd}" && {command}', None
    return command, (cwd or None)


# ── local-filesystem launch staging (#3617) ────────────────────────────────
#
# See the module docstring's own "#3617" section for the why. Split into a
# pure planning step (`_plan_staging`, Windows-path string math only, via
# `ntpath` — never touches a real filesystem, so it's testable with
# fabricated paths on any host OS) and a filesystem-executing step
# (`_execute_staging`, every real I/O call injectable and defaulting to the
# real `os`/`shutil` one) — the same "injectable OS-call seam" shape
# `WinCalls`/`Win32Calls` already use one level up, just for this one
# narrower concern.

#: Where staged sessions live, relative to `%LOCALAPPDATA%\Temp\` — real
#: local NTFS on every Windows host, never a UNC path.
_STAGING_ROOT_DIRNAME = "coord-app-drive"

#: The fleet's own convention (coordinator.yml's win-native `routes:`) for
#: a route's sample/settings working files — copied opportunistically
#: whenever present under `cwd`, regardless of whether the launch command
#: itself names it via a `cd` prefix.
_STAGING_FIXTURE_DIRNAME = ".smoke"

#: `%LOCALAPPDATA%` is only ever read for this one purpose.
_LOCALAPPDATA_ENV = "LOCALAPPDATA"

#: A command containing any of these (beyond the one recognized `cd <dir>
#: && ` prefix `_strip_cd_prefix` already peels off) is an opaque shell
#: pipeline this driver will not guess at rewriting — `_plan_staging`
#: leaves it, and its UNC `cwd`, completely alone (falling back to
#: `_popen_command_and_cwd`'s own `pushd` wrap).
_SHELL_METACHARACTERS = ("&&", "&", "|", ">", "<", ";")

_CD_PREFIX_RE = re.compile(r'^cd\s+"?([^"&]+?)"?\s*&&\s*', re.IGNORECASE)


def _strip_cd_prefix(command: str) -> tuple[str, str]:
    """``("", command)`` unless *command* starts with a plain ``cd <dir>
    && `` prefix (case-insensitive, optionally quoted) — the fleet's own
    convention for entering a route's fixture/working directory before
    launching (e.g. ``cd .smoke && ../target/release/vimcode.exe
    sample.txt``) — in which case returns ``(dir, remainder)``."""
    m = _CD_PREFIX_RE.match(command)
    if not m:
        return "", command
    return m.group(1), command[m.end():]


def _looks_shell_composed(command: str) -> bool:
    return any(token in command for token in _SHELL_METACHARACTERS)


def _leading_token(command: str) -> tuple[str, str]:
    """*command*'s leading whitespace-delimited token (optionally
    double-quoted, to allow an embedded space) and the unparsed remainder
    — e.g. ``'"My App.exe" sample.txt'`` -> ``('My App.exe',
    'sample.txt')``. ``("", "")`` for an empty/all-whitespace *command*."""
    stripped = command.strip()
    if not stripped:
        return "", ""
    if stripped[0] == '"':
        end = stripped.find('"', 1)
        if end != -1:
            return stripped[1:end], stripped[end + 1:].strip()
    parts = stripped.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


@dataclass(frozen=True)
class _StagingPlan:
    """What :func:`_plan_staging` decided — ``staged=False`` means "leave
    *command*/*cwd* exactly as given", the caller's cue to fall back to
    :func:`_popen_command_and_cwd`'s own UNC ``pushd`` wrap unchanged."""

    staged: bool
    cwd: str = ""
    source_exe: str = ""
    dest_exe: str = ""
    #: ``(source_dir, dest_dir)`` pairs to copy wholesale when the source
    #: exists — never an error when one doesn't (an optional fixture dir
    #: nobody provided this time).
    fixture_copies: tuple[tuple[str, str], ...] = ()


def _plan_staging(command: str, cwd: str, *, session_root: str) -> _StagingPlan:
    """Decide whether/how to stage *command*'s exe (plus its fixture
    dir(s)) from *cwd* into *session_root* instead of launching straight
    off *cwd* (#3617).

    Only engages when *cwd* is a UNC path (:func:`_is_unc_path`) — a
    same-host (non-WSL) ``win-native`` agent's ``cwd`` is already local,
    nothing to stage. Recognizes exactly two *command* shapes: a bare
    leading executable token (optionally quoted) with trailing arguments,
    and that same shape preceded by one ``cd <fixture-dir> && ``. Declines
    (``staged=False``) for anything else containing a shell metacharacter
    (:data:`_SHELL_METACHARACTERS` — a second ``&&``, a pipe, a
    redirection, ...) this driver cannot safely rewrite with confidence,
    an already-absolute exe token (nothing of *cwd*'s own tree to stage
    for it), or an exe whose path relative to ``cwd``/``cd_dir`` climbs
    ABOVE ``cwd`` itself without a ``cd_dir`` to cancel it back out
    (staged at the same offset from ``session_root``, it would escape
    this session's own directory into the shared parent every other
    session's own root also lives under).

    *command* itself is returned unchanged in the resulting plan — only
    ``cwd`` moves (to ``session_root``, or ``session_root/<fixture-dir>``
    when a ``cd`` prefix was recognized) — because the exe is copied to
    the SAME path, relative to *session_root*, that it already held
    relative to *cwd* (:func:`ntpath.normpath` applied to ``cd_dir`` +
    the exe token), so every relative reference in *command* — the ``cd``
    itself, a ``../`` the exe token carries, a trailing argument resolved
    against the post-``cd`` directory — keeps resolving correctly against
    the new, local root exactly as it did against the old, UNC one.
    """
    if not _is_unc_path(cwd):
        return _StagingPlan(staged=False)
    cd_dir, remainder = _strip_cd_prefix(command)
    if _looks_shell_composed(remainder):
        return _StagingPlan(staged=False)
    exe_token, _rest = _leading_token(remainder)
    if not exe_token or ntpath.isabs(exe_token):
        return _StagingPlan(staged=False)

    exe_rel = ntpath.normpath(ntpath.join(cd_dir, exe_token)) if cd_dir else ntpath.normpath(exe_token)
    if exe_rel == ".." or exe_rel.startswith(f"..{ntpath.sep}"):
        # The exe's path, relative to *cwd* (after any `cd_dir`), climbs
        # ABOVE `cwd` itself (e.g. a bare `../target/release/vimcode.exe`
        # with no `cd` to cancel it out) — staged at the same relative
        # offset from `session_root`, it would land OUTSIDE this
        # session's own directory, in the shared parent every concurrent
        # session's staging root lives under: a leak `kill`'s own
        # (session-scoped) delete would never reach, and a collision with
        # another session's identically-named escape. Left alone rather
        # than risked — the `cd .smoke && ../target/...` shape above
        # cancels its own `..` back inside `cwd` and is unaffected by
        # this guard.
        return _StagingPlan(staged=False)
    new_cwd = ntpath.join(session_root, cd_dir) if cd_dir else session_root

    fixture_dirnames: list[str] = []
    for name in (cd_dir, _STAGING_FIXTURE_DIRNAME):
        if name and name not in fixture_dirnames:
            fixture_dirnames.append(name)
    fixture_copies = tuple(
        (ntpath.join(cwd, name), ntpath.join(session_root, name))
        for name in fixture_dirnames
    )

    return _StagingPlan(
        staged=True,
        cwd=new_cwd,
        source_exe=ntpath.join(cwd, exe_rel),
        dest_exe=ntpath.join(session_root, exe_rel),
        fixture_copies=fixture_copies,
    )


def _execute_staging(
    plan: _StagingPlan,
    *,
    isfile=os.path.isfile,
    isdir=os.path.isdir,
    makedirs=os.makedirs,
    dirname=os.path.dirname,
    copy_file=shutil.copy2,
    copy_tree=shutil.copytree,
) -> None:
    """The filesystem side of a staging *plan* (#3617) — split from
    :func:`_plan_staging` so the decision logic there stays testable with
    fabricated Windows-style path strings alone; every kwarg here
    defaults to the real ``os``/``shutil`` call (what :class:`Win32Calls`
    actually runs) and is injectable for a test using plain local paths.
    A no-op for a ``staged=False`` plan.

    Raises :class:`WinNativeRuntimeError` when the resolved exe doesn't
    exist — a copy step that silently skipped it would hand back a
    session dir with no exe in it, surfacing as a confusing failure much
    later at ``launch`` itself rather than here, where the real cause is
    actually known (#2096: a gate must be able to fail with an answer
    that actually says why).
    """
    if not plan.staged:
        return
    if not isfile(plan.source_exe):
        raise WinNativeRuntimeError(
            f"win-native launch staging (#3617): exe not found at "
            f"{plan.source_exe!r} — nothing to copy onto the local "
            "Windows filesystem"
        )
    makedirs(dirname(plan.dest_exe), exist_ok=True)
    copy_file(plan.source_exe, plan.dest_exe)
    for source_dir, dest_dir in plan.fixture_copies:
        if isdir(source_dir):
            copy_tree(source_dir, dest_dir, dirs_exist_ok=True)


def _remove_staged_dir(path: str) -> None:
    """Best-effort recursive delete — never raises, mirroring every other
    teardown step in this module (#2096: cleanup must not mask whatever
    real result it's running alongside)."""
    shutil.rmtree(path, ignore_errors=True)


#: #3617: a session whose owning `Win32Calls` got killed/crashed before its
#: own `kill()` ever ran has no Windows analogue of #3583's
#: `PR_SET_PDEATHSIG` to delete its staged directory for it — swept once per
#: `launch`/`launch_in_terminal` call instead (the same "idle self-expiry is
#: the backstop, not the primary path" shape `coord.app_drive
#: .DEFAULT_IDLE_TIMEOUT` already uses), generous enough that a normal
#: `kill()`-driven delete (the primary path) is always what actually cleans
#: up a session that closed normally.
_STALE_SESSION_MAX_AGE_S = 24.0 * 3600.0


#: #3544: every kwarg :meth:`Win32Calls.launch`/:meth:`~Win32Calls.launch_in_terminal`
#: must pass to ``subprocess.Popen`` so the launched ``cmd.exe`` (and whatever
#: GUI-subsystem grandchild it execs) never inherits this *process's own*
#: stdin/stdout/stderr handles.
#:
#: Without this, a plain ``subprocess.Popen(command, shell=True, cwd=...)``
#: (no ``stdin=``/``stdout=``/``stderr=``) hands the child Python's own
#: standard handles unchanged — on Windows that's an inheritance, not a
#: dup/close-on-exec situation. When this `Win32Calls` lives inside the
#: `win-native` bridge runner (:data:`coord.win_native_bridge._BRIDGE_RUNNER_SRC`,
#: a Windows-side ``python -c ...`` whose own stdout is a pipe the WSL-side
#: ``subprocess.run(capture_output=True)`` is reading), that pipe write-end
#: handle gets duplicated into the launched GUI exe. A GUI-subsystem process
#: (``vimcode.exe``) never exits on its own — a human closes the window, or a
#: spec step does — so it holds that handle open indefinitely. A pipe's
#: read end only sees EOF once *every* write-end handle is closed; with the
#: orphaned GUI exe still holding one, the WSL-side read blocks past the
#: whole bridge script's own completion, for the full
#: ``bridge_timeout``, even though the bridge runner itself finished and
#: exited normally. ``Get-Process`` then shows the launched exe still
#: running, orphaned, after the bridge has already timed out and raised.
#:
#: Redirecting all three standard streams to ``DEVNULL`` severs that
#: inheritance: the child gets its own private, already-closed-on-the-
#: parent-side handles, never a dup of this process's own pipe — nothing in
#: this driver ever reads a launched app's stdout/stderr (the whole point
#: of `win-native` is observing the real OS/window state, not console
#: text), so there is no output to lose.
#:
#: **Mechanism caveat (review finding on #3544, unresolved).** CPython's
#: own Windows ``subprocess._execute_child`` computes ``bInheritHandles``
#: as ``int(not close_fds)``, and ``close_fds`` defaults to ``True``
#: (Python 3.7+) and is never overridden anywhere in this module — so by
#: that code path alone, a bare ``Popen(command, shell=True, cwd=...)``
#: with no ``stdin=``/``stdout=``/``stderr=`` should *already* pass
#: ``bInheritHandles=False`` to ``CreateProcess``, meaning CPython's own
#: handle-inheritance bookkeeping is probably *not* the actual leak
#: mechanism. The hang itself is real and reproduced (``Get-Process``
#: showing the launched exe still running minutes after a timed-out bridge
#: call — see #3544's repro), but the likelier remaining culprit is
#: something outside CPython's control for this specific topology: the
#: bridge runner is itself started from WSL via ``wsl.exe``/interop, not a
#: plain native parent process, and WSL interop's own console/job-object
#: handling for the Windows-side process tree it creates can share or
#: re-parent handles differently than a same-OS parent/child pair would.
#:
#: This redirect is still the right thing to do regardless of which exact
#: mechanism turns out to be responsible: explicit ``DEVNULL`` handles are
#: a private pair nothing downstream can keep open, so it closes off every
#: plausible inheritance path at once rather than betting on one theory of
#: the leak being correct. Treat it as the best available fix, **not** a
#: hardware-confirmed root-cause diagnosis — it has not been re-run against
#: the issue's own two repro scripts on a real WSL<->Windows pairing
#: (dell64). ``tests/test_win_native_driver.py``'s
#: ``TestLaunchPipeInheritanceRealSubprocess`` is the closest reproduction
#: available without that hardware: a real (unmocked) OS pipe + subprocess
#: tree showing a long-lived grandchild can hold a parent's un-redirected
#: stdout pipe open past the parent's own exit, and that the real
#: ``Win32Calls.launch`` no longer does so once its grandchild's handles
#: are redirected.
#:
#: ``launch_in_terminal`` already passes ``creationflags=CREATE_NEW_CONSOLE``
#: for its spawned process, which per Windows' own docs already gives the
#: child its own console instead of sharing/inheriting the parent's — so
#: applying this redirect there too is defensive-in-depth, not evidence
#: that call site was equally exposed to the leak this fix targets.
_NO_HANDLE_INHERITANCE: dict = {
    "stdin": subprocess.DEVNULL,
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
}


class Win32Calls:
    """The real :class:`WinCalls` implementation — ``ctypes`` for window
    management, input injection, menu/hit-test probing and ``PrintWindow``;
    the optional ``comtypes``-based UI Automation client (the ``win-native``
    extra) for the accessibility tree.

    Windows-only: raises :class:`WinNativeRuntimeError` at construction on
    any other platform, mirroring
    :class:`coord.tui_pty_driver.WindowsConPtyChild`'s own platform guard.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise WinNativeRuntimeError(
                "Win32Calls requires Windows — the win-native driver only "
                "runs on a real Windows host (e.g. dell64)"
            )
        import ctypes  # noqa: PLC0415
        import ctypes.wintypes  # noqa: PLC0415

        self._ctypes = ctypes
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        #: #3617: pid -> the local session directory `launch`/
        #: `launch_in_terminal` staged for it, so `kill` can delete it once
        #: that pid has actually been signalled.
        self._staged_session_dirs: dict[int, str] = {}

    # -- process lifecycle --

    def launch(self, command: str, cwd: str) -> int:
        # #3617: the UNC check is done BEFORE ever touching `self` — the
        # overwhelming common (non-UNC) case must stay exactly as cheap
        # (and as `self`-independent — see
        # `TestLaunchPipeInheritanceRealSubprocess`'s own unbound
        # `Win32Calls.launch(None, ...)` call) as it was pre-#3617.
        staged_dir = None
        if _is_unc_path(cwd):
            command, cwd, staged_dir = self._stage_if_needed(command, cwd)
        full_command, popen_cwd = _popen_command_and_cwd(command, cwd)
        proc = subprocess.Popen(
            full_command, shell=True, cwd=popen_cwd, **_NO_HANDLE_INHERITANCE,
        )
        if staged_dir is not None:
            self._staged_session_dirs[proc.pid] = staged_dir
        return proc.pid

    def launch_in_terminal(self, command: str, cwd: str, terminal_app: str) -> int:
        staged_dir = None
        if _is_unc_path(cwd):
            command, cwd, staged_dir = self._stage_if_needed(command, cwd)
        create_new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        if terminal_app == "windows-terminal":
            full_command = f"wt.exe {command}"
        else:
            full_command = command
        full_command, popen_cwd = _popen_command_and_cwd(full_command, cwd)
        proc = subprocess.Popen(
            full_command, shell=True, cwd=popen_cwd, creationflags=create_new_console,
            **_NO_HANDLE_INHERITANCE,
        )
        if staged_dir is not None:
            self._staged_session_dirs[proc.pid] = staged_dir
        return proc.pid

    def kill(self, pid: int) -> None:
        # By PID only — see the module docstring's safety note. No
        # image-name-based lookup exists anywhere in this class.
        PROCESS_TERMINATE = 0x0001
        handle = self._kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                self._kernel32.TerminateProcess(handle, 0)
            finally:
                self._kernel32.CloseHandle(handle)
        # #3617: the normal (non-abnormal-exit) cleanup path for a staged
        # session directory — see `_sweep_stale_sessions` for the backstop.
        staged_dir = self._staged_session_dirs.pop(pid, None)
        if staged_dir is not None:
            _remove_staged_dir(staged_dir)

    # -- local-filesystem launch staging (#3617) --

    def _staging_root(self) -> str:
        """``%LOCALAPPDATA%\\Temp\\coord-app-drive`` — real local NTFS on
        every Windows host, never a UNC path. Raises
        :class:`WinNativeRuntimeError` when ``%LOCALAPPDATA%`` isn't set
        in this process's own environment (never hardcoded/guessed — the
        issue's own requirement); :meth:`_stage_if_needed` treats that as
        "best-effort staging unavailable" and falls back to the pre-#3617
        ``pushd`` wrap rather than failing the whole launch over it."""
        local_app_data = os.environ.get(_LOCALAPPDATA_ENV)
        if not local_app_data:
            raise WinNativeRuntimeError(
                f"win-native launch staging (#3617) needs %{_LOCALAPPDATA_ENV}% "
                "to pick a per-session directory on the local Windows "
                "filesystem, but it is not set in this process's environment"
            )
        return os.path.join(local_app_data, "Temp", _STAGING_ROOT_DIRNAME)

    def _sweep_stale_sessions(self) -> None:
        """Best-effort GC backstop for an abandoned staged session (#3617,
        see the module docstring) — deletes any direct child of
        :meth:`_staging_root` whose mtime is older than
        :data:`_STALE_SESSION_MAX_AGE_S`. Never raises: a missing/unreadable
        root, or an entry that disappears mid-sweep (another process
        already cleaned it up), is not an error here."""
        try:
            root = self._staging_root()
            entries = os.listdir(root)
        except (OSError, WinNativeRuntimeError):
            return
        now = time.time()
        for name in entries:
            path = os.path.join(root, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age > _STALE_SESSION_MAX_AGE_S:
                _remove_staged_dir(path)

    def _stage_if_needed(self, command: str, cwd: str) -> tuple[str, str, str | None]:
        """Returns ``(command, cwd, staged_dir)`` — *command*/*cwd*
        rewritten per :func:`_plan_staging` (with the staging actually
        performed) when it decided to, or *command*/*cwd* UNCHANGED (and
        ``staged_dir=None``) in every skip case: a non-UNC ``cwd``, an
        unparseable/opaque *command*, or ``%LOCALAPPDATA%`` itself being
        unavailable right now. Staging is a performance optimization, not
        a correctness requirement — a caller that can't stage still gets
        a working (if UNC-slow) launch via :func:`_popen_command_and_cwd`'s
        own ``pushd`` wrap, rather than this call failing outright."""
        try:
            root = self._staging_root()
        except WinNativeRuntimeError:
            return command, cwd, None
        session_root = os.path.join(root, uuid.uuid4().hex[:12])
        plan = _plan_staging(command, cwd, session_root=session_root)
        if not plan.staged:
            return command, cwd, None
        self._sweep_stale_sessions()
        _execute_staging(plan)
        return command, plan.cwd, session_root

    # -- session precheck (#3510) --

    def session_available(self) -> tuple[bool, str]:
        """Real check: ``OpenInputDesktop`` succeeds (an interactive
        desktop exists to open at all), ``WTSGetActiveConsoleSessionId``
        reports a real console session, and no ``LogonUI.exe`` is running
        in that session (the lock-screen host process). Any one of these
        failing means the host's desktop is locked or absent.

        Defensive by design: this gates every ``win-native`` run (#3510), so
        a probe that raises (e.g. a lookup error on a function that doesn't
        live where expected — #3521) must report "unavailable, here's why"
        rather than take the whole lane down with an uncaught exception."""
        try:
            return self._session_available_unchecked()
        except Exception as exc:  # noqa: BLE001 - must never crash the lane
            return False, f"session_available probe failed: {exc}"

    def _session_available_unchecked(self) -> tuple[bool, str]:
        ctypes = self._ctypes
        DESKTOP_READOBJECTS = 0x0001
        hdesk = self._user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not hdesk:
            return False, (
                "OpenInputDesktop failed — no interactive input desktop is "
                "available on this session (locked or non-interactive)"
            )
        self._user32.CloseDesktop(hdesk)

        # WTSGetActiveConsoleSessionId is exported by kernel32, not
        # wtsapi32 (#3521) — it's the one WTS* function that lives there;
        # the rest of the WTS family (WTSQuerySessionInformation etc.) is
        # genuinely in wtsapi32, which is the likely source of the mix-up.
        INVALID_SESSION_ID = 0xFFFFFFFF
        session_id = self._kernel32.WTSGetActiveConsoleSessionId()
        if session_id == INVALID_SESSION_ID:
            return False, (
                "WTSGetActiveConsoleSessionId reports no active console "
                "session on this host"
            )

        if self._logonui_running_in_session(session_id):
            return False, (
                f"LogonUI.exe is running in session {session_id} — the "
                "desktop is locked"
            )
        return True, ""

    def _logonui_running_in_session(self, session_id: int) -> bool:
        """``True`` iff ``LogonUI.exe`` (the Windows lock-screen host
        process) is running in *session_id* — walked via
        :meth:`_snapshot_processes`, the same mechanism Task Manager itself
        uses, rather than anything that could be confused by a differently-
        named process (the module docstring's "kill only the PID this
        driver itself launched" safety note applies equally here: this is
        read-only enumeration, never a kill)."""
        ctypes = self._ctypes
        for pid, _ppid, name in self._snapshot_processes():
            if name.lower() == "logonui.exe":
                proc_session = ctypes.wintypes.DWORD()
                self._kernel32.ProcessIdToSessionId(pid, ctypes.byref(proc_session))
                if proc_session.value == session_id:
                    return True
        return False

    def _snapshot_processes(self) -> list[tuple[int, int, str]]:
        """Every currently running process as ``(pid, parent_pid,
        exe_name)``, via a single ``CreateToolhelp32Snapshot`` walk — the
        same mechanism Task Manager itself uses. Read-only enumeration,
        shared by :meth:`_logonui_running_in_session` (#3510) and
        :meth:`_descendant_pids` (#3542); never used to kill (see the
        module docstring's safety note)."""
        ctypes = self._ctypes
        kernel32 = self._kernel32
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == -1:
            return []
        out: list[tuple[int, int, str]] = []
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return []
            while True:
                name = entry.szExeFile.decode("mbcs", errors="ignore")
                out.append((entry.th32ProcessID, entry.th32ParentProcessID, name))
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return out

    def _descendant_pids(self, root_pid: int) -> set[int]:
        """*root_pid* plus every process it transitively spawned (child,
        grandchild, ...) — a fresh :meth:`_snapshot_processes` walk each
        call, since the real app may not have started yet the first time
        this is polled.

        #3542: ``launch``/``launch_in_terminal`` don't always return the
        real app's own pid — ``subprocess.Popen(..., shell=True)`` returns
        ``cmd.exe``'s pid, with the real app (``vimcode.exe``) a grandchild
        that ``cmd.exe`` itself never owns a window for. Searching the
        whole descendant tree finds the real app's window regardless of
        how many shell/console hosts sit between the returned pid and it.
        """
        children: dict[int, list[int]] = {}
        for pid, ppid, _name in self._snapshot_processes():
            children.setdefault(ppid, []).append(pid)
        result = {root_pid}
        frontier = [root_pid]
        while frontier:
            current = frontier.pop()
            for child in children.get(current, ()):
                if child not in result:
                    result.add(child)
                    frontier.append(child)
        return result

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        ctypes = self._ctypes
        deadline = time.monotonic() + timeout_s
        found: list[int] = []
        candidate_pids: set[int] = {pid}
        # `WINFUNCTYPE` (stdcall) only exists in ctypes on real Windows —
        # real runs always go through it (Win32Calls.__init__ guards
        # off-Windows construction), but falling back to `CFUNCTYPE` off-
        # Windows is what lets this method's descendant-walk logic be
        # exercised by a scripted fake on Linux/macOS too (#3542), the same
        # "unit-testable on any platform" seam the module docstring
        # describes for the rest of this class.
        win_functype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

        @win_functype(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_proc(hwnd, _lparam):
            owner_pid = ctypes.wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value in candidate_pids and self._user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        while time.monotonic() < deadline:
            candidate_pids = self._descendant_pids(pid)
            found.clear()
            self._user32.EnumWindows(_enum_proc, 0)
            if found:
                return found[0]
            time.sleep(0.1)
        raise WinNativeRuntimeError(
            f"no visible top-level window appeared for pid={pid} or any of "
            f"its child processes within {timeout_s}s"
        )

    def is_window_alive(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(hwnd))

    def move_window(self, hwnd: int, x: int, y: int, width: int, height: int) -> None:
        self._user32.MoveWindow(hwnd, x, y, width, height, True)

    # -- menu / hit-test probes --

    def get_menu_items(self, hwnd: int) -> list[str] | None:
        hmenu = self._user32.GetMenu(hwnd)
        if not hmenu:
            return None
        count = self._user32.GetMenuItemCount(hmenu)
        MF_BYPOSITION = 0x00000400
        items = []
        buf = self._ctypes.create_unicode_buffer(256)
        for i in range(max(count, 0)):
            self._user32.GetMenuStringW(hmenu, i, buf, 256, MF_BYPOSITION)
            items.append(buf.value)
        return items

    def hit_test(self, hwnd: int, x: int, y: int) -> str:
        WM_NCHITTEST = 0x0084
        lparam = (y << 16) | (x & 0xFFFF)
        result = self._user32.SendMessageW(hwnd, WM_NCHITTEST, 0, lparam)
        # ctypes returns an unsigned value for negative hit-test codes
        # (HTERROR=-2, HTTRANSPARENT=-1) — re-interpret as signed 32-bit.
        if result > 0x7FFFFFFF:
            result -= 0x100000000
        return _HT_CODES_BY_VALUE.get(result, f"HT_UNKNOWN({result})")

    # -- input injection --

    def send_click(self, hwnd: int, x: int, y: int, button: str) -> None:
        rect = self._ctypes.wintypes.RECT()
        self._user32.GetWindowRect(hwnd, self._ctypes.byref(rect))
        screen_x, screen_y = rect.left + x, rect.top + y
        self._user32.SetCursorPos(screen_x, screen_y)
        down, up = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }[button]
        self._user32.mouse_event(down, 0, 0, 0, 0)
        self._user32.mouse_event(up, 0, 0, 0, 0)

    def send_key(self, hwnd: int, key: str) -> None:
        self._user32.SetForegroundWindow(hwnd)
        vk, needs_shift = _vkey_for(key)
        KEYEVENTF_KEYUP = 0x0002
        if key.lower().startswith("ctrl+"):
            self._user32.keybd_event(0x11, 0, 0, 0)  # VK_CONTROL down
        if needs_shift:
            self._user32.keybd_event(0x10, 0, 0, 0)  # VK_SHIFT down
        self._user32.keybd_event(vk, 0, 0, 0)
        self._user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        if needs_shift:
            self._user32.keybd_event(0x10, 0, KEYEVENTF_KEYUP, 0)
        if key.lower().startswith("ctrl+"):
            self._user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)

    # -- UI Automation --

    def uia_elements(self, hwnd: int) -> list[dict]:
        client = _import_uia()
        automation = client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            clsctx=1,  # CLSCTX_INPROC_SERVER
        )
        root = automation.ElementFromHandle(hwnd)
        walker = automation.ControlViewWalker
        elements: list[dict] = []

        def _walk(element) -> None:
            try:
                elements.append({
                    "role": element.LocalizedControlType or "",
                    "name": element.CurrentName or "",
                    "visible": not bool(element.CurrentIsOffscreen),
                })
            except Exception:  # noqa: BLE001 — a dead/stale element node
                return
            child = walker.GetFirstChildElement(element)
            while child is not None:
                _walk(child)
                child = walker.GetNextSiblingElement(child)

        _walk(root)
        return elements

    # -- capture --

    def capture(self, hwnd: int) -> bytes:
        ctypes = self._ctypes
        gdi32 = ctypes.windll.gdi32
        rect = ctypes.wintypes.RECT()
        self._user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise WinNativeRuntimeError(
                f"cannot capture hwnd={hwnd} — window rect is {width}x{height}"
            )

        hdc_window = self._user32.GetWindowDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        gdi32.SelectObject(hdc_mem, hbitmap)
        PW_RENDERFULLCONTENT = 0x00000002
        ok = self._user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        try:
            if not ok:
                raise WinNativeRuntimeError(f"PrintWindow failed for hwnd={hwnd}")
            return _bitmap_to_bmp_bytes(ctypes, gdi32, hdc_mem, hbitmap, width, height)
        finally:
            gdi32.DeleteObject(hbitmap)
            gdi32.DeleteDC(hdc_mem)
            self._user32.ReleaseDC(hwnd, hdc_window)


def _bitmap_to_bmp_bytes(ctypes, gdi32, hdc_mem, hbitmap, width: int, height: int) -> bytes:
    """``GetDIBits`` the captured bitmap into raw 24-bit BGR pixel data,
    wrapped in a minimal, self-contained ``.bmp`` file (no extra imaging
    library needed) — sufficient as a failing-step evidence attachment."""
    import struct

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    row_bytes = ((width * 3 + 3) // 4) * 4
    image_size = row_bytes * height
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = height
    header.biPlanes = 1
    header.biBitCount = 24
    header.biCompression = 0  # BI_RGB
    header.biSizeImage = image_size

    buf = (ctypes.c_ubyte * image_size)()
    gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buf, ctypes.byref(header), 0)

    bmp_header = struct.pack("<2sIHHI", b"BM", 14 + ctypes.sizeof(header) + image_size, 0, 0, 14 + ctypes.sizeof(header))
    return bmp_header + bytes(header) + bytes(buf)


def _import_uia():
    """The optional ``comtypes`` UI Automation client (the ``win-native``
    extra) — guarded the same way
    :func:`coord.tui_pty_driver._import_pyte`/``WindowsConPtyChild`` guard
    their own optional dependencies, so a missing package names the extra
    to install rather than surfacing a bare ``ModuleNotFoundError``."""
    try:
        import comtypes.client as client  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise WinNativeRuntimeError(
            "win-native needs the 'win-native' extra, which is not "
            "installed (missing 'comtypes').\n"
            "  Install it with:  pip install 'code-coordinator[win-native]'"
        ) from exc
    return client


# ── top-level entry point ───────────────────────────────────────────────────

class WinNativeSession:
    """Persistent win-native ``coord app-drive`` session (#3590) — mirrors
    :class:`coord.tui_pty_driver.TuiPtySession`'s "sanctioned driver, one
    verb per call" shape: drives the exact same :class:`WinCalls` real-OS
    implementation :class:`NativeRunner`'s own ``launch``/``key``/``click``/
    ``capture`` step handlers use (same PID-only ``kill``, never by image
    name), just invoked on demand rather than against one fixed step list.

    Callers (:mod:`coord.app_drive`'s daemon dispatch) MUST confirm
    :meth:`WinCalls.session_available` themselves BEFORE constructing this
    — exactly like :meth:`NativeRunner.run` only reaches its own per-step
    handlers after that same check; this class assumes an
    already-confirmed-available session.
    """

    def __init__(
        self, launch_command: str, cwd: str, *, width: int = 1024, height: int = 768,
        calls: WinCalls | None = None, timeout_s: float = 10.0,
    ) -> None:
        self._calls: WinCalls = calls if calls is not None else Win32Calls()
        self._pid = self._calls.launch(launch_command, cwd)
        self._hwnd = self._calls.find_top_window(self._pid, timeout_s)
        self._calls.move_window(self._hwnd, 0, 0, width, height)

    @property
    def pid(self) -> int:
        """The pid :meth:`WinCalls.launch` returned (#3590) — read by
        :mod:`coord.app_drive_daemon` for its ready-file's ``app_pid`` so
        :func:`coord.app_drive.close_session` can re-observe/re-signal the
        process this session itself launched, not just the daemon.

        See :meth:`_descendant_pids`'s own docstring (#3542): this is
        ``cmd.exe``'s pid, not the real app's — ``Win32Calls.launch``'s
        ``shell=True`` makes the real app a grandchild this pid never
        owns a window for. Killing this pid alone can leave that
        grandchild running; tracked separately from #3590, same class as
        the mac-native/gtk-native shell-vs-app gap."""
        return self._pid

    def send_key(self, key: str) -> None:
        self._calls.send_key(self._hwnd, key)

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        self._calls.send_click(self._hwnd, x, y, button)

    def capture(self) -> bytes:
        return self._calls.capture(self._hwnd)

    def probe(self, name: str, **kwargs: Any) -> Any:
        if name == "uia_elements":
            return self._calls.uia_elements(self._hwnd)
        if name == "is_window_alive":
            return self._calls.is_window_alive(self._hwnd)
        if name == "get_menu_items":
            return self._calls.get_menu_items(self._hwnd)
        if name == "hit_test":
            return self._calls.hit_test(self._hwnd, int(kwargs.get("x", 0)), int(kwargs.get("y", 0)))
        if name == "find_a11y":
            elements = self._calls.uia_elements(self._hwnd)
            match = _find_a11y_match(elements, kwargs.get("role", ""), kwargs.get("name", ""))
            return {
                "found": match is not None, "element": match,
                "tree": _summarize_elements(elements),
            }
        raise ValueError(
            f"unknown probe {name!r} — expected one of: uia_elements, "
            "is_window_alive, get_menu_items, hit_test, find_a11y"
        )

    def is_alive(self) -> bool:
        return self._calls.is_window_alive(self._hwnd)

    def close(self) -> None:
        """Kills only the PID this session itself launched (see the module
        docstring's safety note)."""
        try:
            self._calls.kill(self._pid)
        except Exception:  # noqa: BLE001 — teardown must not raise
            pass


def run_native_spec(
    spec_text: str, *, launch_command: str, cwd: str,
    calls: WinCalls | None = None, timeout: float | None = None,
) -> list[dict]:
    """Parse *spec_text* and run it against *calls* (a real
    :class:`Win32Calls` by default) launching *launch_command* in *cwd* —
    the top-level entry point
    :func:`coord.acceptance_drivers._run_win_native` calls.

    *timeout*, when given, is an overall wall-clock budget in seconds for
    the whole spec — mirrors :func:`coord.tui_pty_driver.run_smoke_spec`'s
    own ``timeout``: each step already carries its own bounded per-step
    budget, but their sum can still exceed it, in which case every
    remaining step fails explicitly rather than the run truncating or
    blocking past it.

    Raises :class:`WinNativeSpecError` for a malformed spec. Runtime
    failures (the process never launches, a window never appears, an
    assertion fails) do NOT raise — they're folded into the returned list
    as a ``status="fail"`` entry, the same "partial results, not a crash"
    contract :func:`coord.tui_pty_driver.run_smoke_spec` already gives.

    When *calls* reports a locked or absent interactive session
    (:meth:`WinCalls.session_available`, #3510), the returned list is a
    single ``status="unavailable"`` entry and no step runs — a distinct
    verdict from ``"fail"``, since a locked desktop is an environment
    condition, not an app bug.
    """
    spec = parse_native_spec(spec_text)
    resolved_calls = calls if calls is not None else Win32Calls()
    deadline = time.monotonic() + timeout if timeout is not None else None
    runner = NativeRunner(resolved_calls, launch_command, cwd, deadline=deadline)
    return runner.run(spec)
